from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import stat
import sys
import uuid
from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

import requests

# Import FastMCP before exposing the trusted snapshot parent on ``sys.path``.
# The local generation package is also named ``mcp`` on disk, while FastMCP
# depends on the separately installed MCP SDK's top-level ``mcp`` package.
# Loading the SDK first prevents direct execution from shadowing ``mcp.types``
# with the trusted generation sources.
try:
    from fastmcp import FastMCP
except ModuleNotFoundError as exc:  # pragma: no cover - dependency is required in prod
    if exc.name != "fastmcp":
        raise
    FastMCP = None  # type: ignore[assignment]

if __package__ in {None, ""}:
    # ``python -I /attested/snapshot/mcp/server.py`` intentionally removes the
    # script directory from sys.path. Re-add only this exact attested sibling
    # directory so the dependency-free generation-control CLI can use the same
    # module without trusting cwd or PYTHONPATH.
    _ATTESTED_MCP_ROOT = Path(__file__).resolve(strict=True).parent
    sys.path.insert(0, str(_ATTESTED_MCP_ROOT))
    # ``review_client`` depends on the separately attested, read-only
    # ``review`` sibling copied into the same trusted runtime.  Bind only that
    # exact snapshot parent; never consult cwd or PYTHONPATH.
    sys.path.insert(0, str(_ATTESTED_MCP_ROOT.parent))

try:
    from .advisor_client import advisor_report_get
except ImportError:  # pragma: no cover - direct module execution
    from advisor_client import advisor_report_get

try:
    from .proof_context import parse_blueprint
except ImportError:  # pragma: no cover - direct module execution
    from proof_context import parse_blueprint

try:
    from .review_client import (
        context_handoff_get as _adapter_context_handoff_get,
        context_handoff_preflight as _adapter_context_handoff_preflight,
        context_handoff_prepare as _adapter_context_handoff_prepare,
        context_handoff_status as _adapter_context_handoff_status,
        generation_yield_prepare as _adapter_generation_yield_prepare,
        reasoning_phase_preflight as _adapter_reasoning_phase_preflight,
        review_due_status as _adapter_review_due_status,
        route_review_close as _adapter_route_review_close,
        route_review_prepare as _adapter_route_review_prepare,
        route_review_status as _adapter_route_review_status,
        route_review_targeted_verification_commit as _adapter_targeted_verification_commit,
        route_review_targeted_verification_prepare as _adapter_targeted_verification_prepare,
        route_review_wait as _adapter_route_review_wait,
        route_cycle_close as _adapter_route_cycle_close,
    )
except ImportError:  # pragma: no cover - direct module execution
    from review_client import (  # type: ignore[no-redef]
        context_handoff_get as _adapter_context_handoff_get,
        context_handoff_preflight as _adapter_context_handoff_preflight,
        context_handoff_prepare as _adapter_context_handoff_prepare,
        context_handoff_status as _adapter_context_handoff_status,
        generation_yield_prepare as _adapter_generation_yield_prepare,
        reasoning_phase_preflight as _adapter_reasoning_phase_preflight,
        review_due_status as _adapter_review_due_status,
        route_review_close as _adapter_route_review_close,
        route_review_prepare as _adapter_route_review_prepare,
        route_review_status as _adapter_route_review_status,
        route_review_targeted_verification_commit as _adapter_targeted_verification_commit,
        route_review_targeted_verification_prepare as _adapter_targeted_verification_prepare,
        route_review_wait as _adapter_route_review_wait,
        route_cycle_close as _adapter_route_cycle_close,
    )

try:
    from ...review.contracts import (
        PROGRESS_KINDS,
        REVIEW_ID_RE,
        SHA256_RE,
        apply_effective_verdict,
        build_targeted_verification_ticket,
        canonical_json_bytes,
        handoff_id as trusted_handoff_id,
        handoff_sha256,
        validate_context_handoff,
        validate_review_report,
        validate_targeted_verification_ticket,
    )
    from ...review.critic import build_review_request
except ImportError:  # pragma: no cover - trusted snapshot/direct execution
    from review.contracts import (
        PROGRESS_KINDS,
        REVIEW_ID_RE,
        SHA256_RE,
        apply_effective_verdict,
        build_targeted_verification_ticket,
        canonical_json_bytes,
        handoff_id as trusted_handoff_id,
        handoff_sha256,
        validate_context_handoff,
        validate_review_report,
        validate_targeted_verification_ticket,
    )
    from review.critic import build_review_request

try:
    from .verification_client import (
        expected_attestation,
        proof_digest,
        validate_service_response,
        verify_targeted_claim_service,
        verify_blueprint_file,
    )
except ImportError:  # pragma: no cover - direct `python mcp/server.py` execution
    from verification_client import (  # type: ignore[no-redef]
        expected_attestation,
        proof_digest,
        validate_service_response,
        verify_targeted_claim_service,
        verify_blueprint_file,
    )

_SOURCE_REPO_ROOT = Path(__file__).resolve().parents[1]
# The example runner launches this module from a read-only trusted snapshot
# outside the model-writable workspace. In that mode, business data still lives
# under the explicitly bound generation root.
REPO_ROOT = Path(os.getenv("RETHLAS_GENERATION_ROOT", str(_SOURCE_REPO_ROOT))).resolve(
    strict=True
)
MEMORY_ROOT = REPO_ROOT / "memory"
RESULTS_ROOT = REPO_ROOT / "results"
DATA_ROOT = REPO_ROOT / "data"
GENERATION_CONTROL_ROOT = REPO_ROOT.parent / ".generation_control"
# The receipt directory is a trust boundary: generation Codex receives write
# access to ``REPO_ROOT`` and must never be able to redirect receipts into that
# workspace or a generally writable temporary directory.
RECEIPTS_ROOT = REPO_ROOT.parent / ".verification_receipts"

MATLAS_SEARCH_URL = os.getenv(
    "MATLAS_URL",
    "https://matlas.ai/api/search",
)
LEGACY_ARXIV_THEOREM_URL = os.getenv(
    "LEGACY_ARXIV_THEOREM_URL",
    "https://leansearch.net/thm/search",
)
THEOREM_SEARCH_TASK = (
    "Given a math statement, retrieve useful references, such as theorems, "
    "lemmas, and definitions, that are useful for solving the given problem."
)
MAX_EXTERNAL_QUERY_UTF8_BYTES = 8_192
MAX_EXTERNAL_SUCCESS_UTF8_BYTES = 32_768
MAX_EXTERNAL_RAW_RESPONSE_BYTES = 262_144


class _ExternalResponseTooLarge(ValueError):
    pass


class _ExternalResponseCloseError(requests.RequestException):
    pass


def _normalize_external_query(query: Any) -> str:
    if not isinstance(query, str):
        raise ValueError("query must be a string")
    normalized = query.strip()
    if not normalized:
        raise ValueError("query must be non-empty")
    try:
        encoded = normalized.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError("query must be valid UTF-8") from error
    if len(encoded) > MAX_EXTERNAL_QUERY_UTF8_BYTES:
        raise ValueError("query exceeds external retrieval byte limit")
    return normalized


def _validate_external_result_count(num_results: Any) -> int:
    if (
        isinstance(num_results, bool)
        or not isinstance(num_results, int)
        or not 1 <= num_results <= 200
    ):
        raise ValueError("num_results must be an integer between 1 and 200")
    return num_results


def _external_fields_are_utf8(item: Mapping[str, str], fields: Iterable[str]) -> bool:
    try:
        for field in fields:
            item[field].encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _read_bounded_external_json(response: requests.Response) -> Any:
    headers = getattr(response, "headers", {})
    content_length = headers.get("Content-Length") if hasattr(headers, "get") else None
    if isinstance(content_length, str) and content_length.isdigit():
        if int(content_length) > MAX_EXTERNAL_RAW_RESPONSE_BYTES:
            raise _ExternalResponseTooLarge

    iter_content = getattr(response, "iter_content", None)
    if not callable(iter_content):
        return response.json()

    chunks: List[bytes] = []
    total = 0
    for chunk in iter_content(chunk_size=16_384):
        if not chunk:
            continue
        if not isinstance(chunk, bytes):
            raise ValueError("external response chunk must be bytes")
        total += len(chunk)
        if total > MAX_EXTERNAL_RAW_RESPONSE_BYTES:
            raise _ExternalResponseTooLarge
        chunks.append(chunk)
    return json.loads(b"".join(chunks))


VERIFY_PROOF_URL = os.getenv(
    "VERIFY_PROOF_URL",
    "http://127.0.0.1:8091/verify",
)
VERIFY_TARGETED_CLAIM_URL = os.getenv(
    "VERIFY_TARGETED_CLAIM_URL",
    "http://127.0.0.1:8091/verify-targeted-claim",
)

DEFAULT_MEMORY_SEARCH_MAX_CHARS = 20_000
MAX_OMITTED_IDS = 100
BM25_NEAR_TIE_DECIMALS = 6
MAX_MEMORY_BATCH_RECORDS = 32
MAX_MEMORY_BATCH_UTF8_BYTES = 131_072
MEMORY_BATCH_SCHEMA = "rethlas_memory_batch_v2"
MAX_MEMORY_BATCH_FILE_BYTES = 262_144
GENERATION_CONTROL_SCHEMA = "rethlas_generation_control_v1"
GENERATION_WAIT_STATES = frozenset(
    {"waiting_cost_gate", "waiting_owner_advisor_decision"}
)
MAX_GENERATION_CONTROL_REASON_BYTES = 4_096
MAX_GENERATION_CONTROL_EVIDENCE_IDS = 16
MAX_GENERATION_CONTROL_FILE_BYTES = 32_768
_GENERATION_CONTROL_EVIDENCE_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_GENERATION_CONTROL_INSTANCE_RE = re.compile(r"[0-9a-f]{32}")
_CONTENT_ADDRESSED_BATCH_ID_RE = re.compile(r"^batch_[0-9a-f]{64}$")
_REVIEW_MEMORY_SCHEMA = "rethlas_official_route_review_memory_v1"
_TARGETED_MEMORY_SCHEMA = "rethlas_targeted_verification_memory_v1"
_REVIEW_MODEL_ENV = "RETHLAS_REVIEW_EXPECTED_MODEL"
_REVIEW_EFFORT_ENV = "RETHLAS_REVIEW_EXPECTED_REASONING_EFFORT"
_REVIEW_POLICY_SHA_ENV = "RETHLAS_REVIEW_POLICY_SHA256"
_REVIEW_RUN_ENV = "RETHLAS_EXPECTED_HOTJOIN_RUN_ID"
_REVIEW_PROGRESS_KIND_FIELD = "review_progress_kind"
_ACTIVE_ROUTE_COMMITMENT_SCHEMA = "rethlas_active_route_commitment_v1"
_ROUTE_TRANSITION_STATE_SCHEMA = "rethlas_route_transition_state_v1"
_ROUTE_TRANSITION_RECEIPT_SCHEMA = "rethlas_route_transition_publication_receipt_v1"
_REVIEW_FRONTIER_CHANNELS = frozenset(
    {
        "immediate_conclusions",
        "toy_examples",
        "counterexamples",
        "big_decisions",
        "subgoals",
        "proof_steps",
        "failed_paths",
        "verification_reports",
        "branch_states",
    }
)
_HANDOFF_REQUIRED_ID_ENV = "RETHLAS_CONTEXT_HANDOFF_REQUIRED_ID"
_HANDOFF_REQUIRED_SHA_ENV = "RETHLAS_CONTEXT_HANDOFF_REQUIRED_SHA256"
_HANDOFF_THREAD_EPOCH_ENV = "RETHLAS_CONTEXT_THREAD_EPOCH"

CHANNEL_FILES: Dict[str, str] = {
    "immediate_conclusions": "immediate_conclusions.jsonl",
    "toy_examples": "toy_examples.jsonl",
    "counterexamples": "counterexamples.jsonl",
    "big_decisions": "big_decisions.jsonl",
    "subgoals": "subgoals.jsonl",
    "proof_steps": "proof_steps.jsonl",
    "failed_paths": "failed_paths.jsonl",
    "verification_reports": "verification_reports.jsonl",
    "route_reviews": "route_reviews.jsonl",
    "targeted_verifications": "targeted_verifications.jsonl",
    "branch_states": "branch_states.jsonl",
    "events": "events.jsonl",
}
_CONTROL_ONLY_MEMORY_CHANNELS = frozenset({"route_reviews", "targeted_verifications"})


def _is_route_transition_projection(channel: Any, record: Any) -> bool:
    state = record.get("state") if isinstance(record, dict) else None
    return bool(
        channel == "branch_states"
        and isinstance(state, dict)
        and state.get("schema_version") == _ROUTE_TRANSITION_STATE_SCHEMA
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sanitize_problem_component(raw: str) -> str:
    cleaned = re.sub(r"\s+", "_", raw.strip())
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", cleaned)
    cleaned = re.sub(r"_+", "_", cleaned).strip("._")
    return cleaned


def sanitize_problem_id(raw: str) -> str:
    """Return a safe problem id while preserving relative path components."""
    normalized = raw.strip().replace("\\", "/")
    parts: List[str] = []
    for part in normalized.split("/"):
        stripped = part.strip()
        if stripped in {"", "."}:
            continue
        if stripped == "..":
            raise ValueError("problem_id must not contain '..' path components")
        cleaned = _sanitize_problem_component(stripped)
        if cleaned:
            parts.append(cleaned)
    return "/".join(parts) or "problem"


_VERIFIED_PROBLEM_COMPONENT_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9-])?$"
)


def validate_verified_problem_id(raw: str) -> str:
    """Validate a publication id without lossy normalization."""
    if not isinstance(raw, str) or not raw or raw != raw.strip() or "\\" in raw:
        raise ValueError("problem_id must be a non-empty normalized relative path")
    parts = raw.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(
            "problem_id must not contain empty, '.', or '..' path components"
        )
    if any(_VERIFIED_PROBLEM_COMPONENT_RE.fullmatch(part) is None for part in parts):
        raise ValueError(
            "problem_id components must use ASCII letters, digits, '.', '_', or '-' "
            "and must begin with an alphanumeric character and end with an "
            "alphanumeric character or '-'"
        )
    return raw


def build_problem_id(source: str, identifier: str) -> str:
    return sanitize_problem_id(f"{source}_{identifier}")


def _resolve_path(path_str: str) -> Path:
    candidate = Path(path_str)
    if not candidate.is_absolute():
        candidate = REPO_ROOT / candidate
    return candidate.resolve()


def _memory_root_path() -> Path:
    root = Path(os.path.abspath(os.fspath(MEMORY_ROOT)))
    if root == Path(root.anchor):
        raise ValueError("memory root must not be a filesystem root")
    return root


def _problem_dir(problem_id: str) -> Path:
    sanitized_problem_id = sanitize_problem_id(problem_id)
    return _memory_root_path().joinpath(*sanitized_problem_id.split("/"))


def _channel_path(problem_id: str, channel: str) -> Path:
    if channel not in CHANNEL_FILES:
        allowed = ", ".join(sorted(CHANNEL_FILES))
        raise ValueError(f"Unknown channel '{channel}'. Allowed channels: {allowed}")
    return _problem_dir(problem_id) / CHANNEL_FILES[channel]


def _batch_checkpoint_dir(problem_id: str) -> Path:
    return _problem_dir(problem_id) / ".phase_checkpoints"


def _strict_json_loads(raw: str, *, label: str) -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError(f"{label} contains non-finite JSON constant {value}")

    def reject_duplicate_keys(pairs: List[tuple[str, Any]]) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"{label} contains duplicate JSON key {key!r}")
            result[key] = value
        return result

    try:
        return json.loads(
            raw,
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_constant,
        )
    except (json.JSONDecodeError, UnicodeError, ValueError) as exc:
        raise ValueError(f"{label} is not valid strict JSON: {exc}") from exc


def _memory_path_parts(path: Path) -> tuple[str, ...]:
    root = _memory_root_path()
    target = Path(os.path.abspath(os.fspath(path)))
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise ValueError("memory path resolves outside memory root") from exc
    parts = relative.parts
    if any(part in {"", ".", ".."} or "/" in part or "\0" in part for part in parts):
        raise ValueError("memory path contains an invalid component")
    return parts


def _directory_open_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _file_open_flags(flags: int) -> int:
    return flags | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _validate_directory_metadata(
    metadata: os.stat_result,
    *,
    label: str,
    require_owner: bool,
) -> None:
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_nlink < 1:
        raise ValueError(f"{label} is not a durable directory")
    if require_owner:
        if metadata.st_uid != os.geteuid():
            raise ValueError(f"{label} is not owned by the MCP process")
        if metadata.st_mode & 0o022:
            raise ValueError(f"{label} is writable by group or other users")


def _validate_regular_metadata(
    metadata: os.stat_result,
    *,
    label: str,
    expected_nlink: int,
) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"{label} is not a regular file")
    if metadata.st_uid != os.geteuid():
        raise ValueError(f"{label} is not owned by the MCP process")
    if metadata.st_nlink != expected_nlink:
        raise ValueError(f"{label} has an unsafe hard-link count")
    if metadata.st_mode & 0o022:
        raise ValueError(f"{label} is writable by group or other users")


def _fsync_directory_fd(descriptor: int, path: Path) -> None:
    del path  # Kept as a deterministic durability trace label for tests/audits.
    os.fsync(descriptor)


def _open_absolute_directory_nofollow(path: Path) -> int:
    absolute = Path(os.path.abspath(os.fspath(path)))
    descriptor = os.open(absolute.anchor, _directory_open_flags())
    current = Path(absolute.anchor)
    try:
        for component in absolute.parts[1:]:
            label = f"memory ancestor {current / component}"
            before = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
            _validate_directory_metadata(
                before,
                label=label,
                require_owner=False,
            )
            child = os.open(component, _directory_open_flags(), dir_fd=descriptor)
            try:
                opened = os.fstat(child)
                after = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
                _validate_directory_metadata(
                    opened,
                    label=label,
                    require_owner=False,
                )
                if not _same_inode(before, opened) or not _same_inode(opened, after):
                    raise ValueError(f"{label} changed while it was opened")
            except BaseException:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
            current /= component
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_child_directory_at(
    parent_descriptor: int,
    parent_path: Path,
    name: str,
    *,
    create: bool,
) -> int:
    if name in {"", ".", ".."} or "/" in name or "\0" in name:
        raise ValueError("memory directory has an invalid component")
    child_path = parent_path / name
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent_descriptor)
        except FileExistsError:
            pass
        # Always fsync: this also closes a prior post-mkdir durability gap.
        _fsync_directory_fd(parent_descriptor, parent_path)
    before = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    _validate_directory_metadata(
        before,
        label=f"memory directory {child_path}",
        require_owner=True,
    )
    child = os.open(name, _directory_open_flags(), dir_fd=parent_descriptor)
    try:
        opened = os.fstat(child)
        after = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        _validate_directory_metadata(
            opened,
            label=f"memory directory {child_path}",
            require_owner=True,
        )
        if not _same_inode(before, opened) or not _same_inode(opened, after):
            raise ValueError(f"memory directory {child_path} changed while opened")
    except BaseException:
        os.close(child)
        raise
    return child


def _open_memory_root(*, create: bool) -> int:
    root = _memory_root_path()
    # The configured parent is the trusted anchor.  Resolve only that anchor so
    # platform aliases such as macOS /var -> /private/var remain usable; the
    # MEMORY_ROOT entry itself and every component below it are never followed.
    parent_path = root.parent.resolve(strict=True)
    parent = _open_absolute_directory_nofollow(parent_path)
    try:
        _validate_directory_metadata(
            os.fstat(parent),
            label=f"memory root parent {parent_path}",
            require_owner=False,
        )
        return _open_child_directory_at(
            parent,
            parent_path,
            root.name,
            create=create,
        )
    finally:
        os.close(parent)


def _open_memory_directory(path: Path, *, create: bool) -> int:
    parts = _memory_path_parts(path)
    descriptor = _open_memory_root(create=create)
    current = _memory_root_path()
    try:
        for component in parts:
            child = _open_child_directory_at(
                descriptor,
                current,
                component,
                create=create,
            )
            os.close(descriptor)
            descriptor = child
            current /= component
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_memory_parent(path: Path, *, create: bool) -> tuple[int, str]:
    parts = _memory_path_parts(path)
    if not parts:
        raise ValueError("memory file path must be below memory root")
    parent = _memory_root_path().joinpath(*parts[:-1])
    return _open_memory_directory(parent, create=create), parts[-1]


def _fsync_directory(path: Path) -> None:
    descriptor = _open_memory_directory(path, create=False)
    try:
        _fsync_directory_fd(descriptor, path)
    finally:
        os.close(descriptor)


def _ensure_memory_directory_durable(path: Path) -> None:
    """Create a fenced memory directory and durably record every component."""

    descriptor = _open_memory_directory(path, create=True)
    os.close(descriptor)


def _verify_open_regular_at(
    parent_descriptor: int,
    name: str,
    descriptor: int,
    *,
    label: str,
    expected_nlink: int = 1,
) -> os.stat_result:
    opened = os.fstat(descriptor)
    entry = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    _validate_regular_metadata(
        opened,
        label=label,
        expected_nlink=expected_nlink,
    )
    _validate_regular_metadata(
        entry,
        label=label,
        expected_nlink=expected_nlink,
    )
    if not _same_inode(opened, entry):
        raise ValueError(f"{label} changed after it was opened")
    return opened


def _verify_open_regular_allowed_nlinks(
    parent_descriptor: int,
    name: str,
    descriptor: int,
    *,
    label: str,
    allowed_nlinks: frozenset[int],
) -> os.stat_result:
    opened = os.fstat(descriptor)
    entry = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    if opened.st_nlink not in allowed_nlinks or entry.st_nlink not in allowed_nlinks:
        raise ValueError(f"{label} has an unsafe hard-link count")
    _validate_regular_metadata(
        opened,
        label=label,
        expected_nlink=opened.st_nlink,
    )
    _validate_regular_metadata(
        entry,
        label=label,
        expected_nlink=entry.st_nlink,
    )
    if not _same_inode(opened, entry):
        raise ValueError(f"{label} changed after it was opened")
    return opened


def _open_existing_regular_at(
    parent_descriptor: int,
    name: str,
    flags: int,
    *,
    label: str,
) -> int:
    before = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    _validate_regular_metadata(before, label=label, expected_nlink=1)
    descriptor = os.open(
        name,
        _file_open_flags(flags),
        dir_fd=parent_descriptor,
    )
    try:
        opened = _verify_open_regular_at(
            parent_descriptor,
            name,
            descriptor,
            label=label,
        )
        if not _same_inode(before, opened):
            raise ValueError(f"{label} changed while it was opened")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _open_existing_regular_allowed_nlinks(
    parent_descriptor: int,
    name: str,
    flags: int,
    *,
    label: str,
    allowed_nlinks: frozenset[int],
) -> int:
    before = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    if before.st_nlink not in allowed_nlinks:
        raise ValueError(f"{label} has an unsafe hard-link count")
    _validate_regular_metadata(
        before,
        label=label,
        expected_nlink=before.st_nlink,
    )
    descriptor = os.open(
        name,
        _file_open_flags(flags),
        dir_fd=parent_descriptor,
    )
    try:
        opened = _verify_open_regular_allowed_nlinks(
            parent_descriptor,
            name,
            descriptor,
            label=label,
            allowed_nlinks=allowed_nlinks,
        )
        if not _same_inode(before, opened):
            raise ValueError(f"{label} changed while it was opened")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _open_new_regular_at(
    parent_descriptor: int,
    name: str,
    flags: int,
    *,
    label: str,
) -> int:
    descriptor = os.open(
        name,
        _file_open_flags(flags | os.O_CREAT | os.O_EXCL),
        0o600,
        dir_fd=parent_descriptor,
    )
    try:
        _verify_open_regular_at(
            parent_descriptor,
            name,
            descriptor,
            label=label,
        )
    except BaseException:
        try:
            entry = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if _same_inode(entry, os.fstat(descriptor)):
                os.unlink(name, dir_fd=parent_descriptor)
                _fsync_directory_fd(parent_descriptor, Path("."))
        os.close(descriptor)
        raise
    return descriptor


def _write_all(descriptor: int, encoded: bytes) -> None:
    offset = 0
    while offset < len(encoded):
        written = os.write(descriptor, encoded[offset:])
        if written <= 0:
            raise OSError("short write while persisting memory")
        offset += written


def _read_memory_bytes(
    path: Path,
    *,
    max_bytes: int | None = None,
    allow_missing: bool = False,
) -> bytes | None:
    try:
        parent, name = _open_memory_parent(path, create=False)
    except FileNotFoundError:
        if allow_missing:
            return None
        raise
    try:
        try:
            descriptor = _open_existing_regular_at(
                parent,
                name,
                os.O_RDONLY,
                label=f"memory file {path}",
            )
        except FileNotFoundError:
            if allow_missing:
                return None
            raise
        try:
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(descriptor, 65_536)
                if not chunk:
                    break
                total += len(chunk)
                if max_bytes is not None and total > max_bytes:
                    raise ValueError(f"memory file exceeds its size limit: {path}")
                chunks.append(chunk)
            _verify_open_regular_at(
                parent,
                name,
                descriptor,
                label=f"memory file {path}",
            )
            return b"".join(chunks)
        finally:
            os.close(descriptor)
    finally:
        os.close(parent)


def _iter_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    try:
        parent, name = _open_memory_parent(path, create=False)
    except FileNotFoundError:
        return
    try:
        try:
            descriptor = _open_existing_regular_at(
                parent,
                name,
                os.O_RDONLY,
                label=f"memory channel {path}",
            )
        except FileNotFoundError:
            return
        try:
            with os.fdopen(
                descriptor,
                "r",
                encoding="utf-8",
                closefd=False,
            ) as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(payload, dict):
                        yield payload
            _verify_open_regular_at(
                parent,
                name,
                descriptor,
                label=f"memory channel {path}",
            )
        finally:
            os.close(descriptor)
    finally:
        os.close(parent)


def _append_jsonl(path: Path, payload: Dict[str, Any]) -> None:
    encoded = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
    parent, name = _open_memory_parent(path, create=False)
    try:
        descriptor = _open_existing_regular_at(
            parent,
            name,
            os.O_WRONLY | os.O_APPEND,
            label=f"memory channel {path}",
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            _verify_open_regular_at(
                parent,
                name,
                descriptor,
                label=f"memory channel {path}",
            )
            _write_all(descriptor, encoded)
            os.fsync(descriptor)
            _verify_open_regular_at(
                parent,
                name,
                descriptor,
                label=f"memory channel {path}",
            )
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
    finally:
        os.close(parent)


def _unlink_temporary_durable_at(
    parent_descriptor: int,
    parent_path: Path,
    name: str,
    descriptor: int | None,
) -> None:
    try:
        entry = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return
    _validate_regular_metadata(
        entry,
        label=f"memory temporary {parent_path / name}",
        expected_nlink=entry.st_nlink,
    )
    if entry.st_nlink not in {1, 2}:
        raise ValueError("memory temporary has an unsafe hard-link count")
    if descriptor is not None and not _same_inode(entry, os.fstat(descriptor)):
        raise ValueError("memory temporary changed before cleanup")
    os.unlink(name, dir_fd=parent_descriptor)
    _fsync_directory_fd(parent_descriptor, parent_path)


def _unlink_temporary_durable(path: Path) -> None:
    parent, name = _open_memory_parent(path, create=False)
    try:
        _unlink_temporary_durable_at(parent, path.parent, name, None)
    finally:
        os.close(parent)


def _write_atomic_json_replace(path: Path, payload: Dict[str, Any]) -> None:
    """Atomically replace fenced mutable JSON without exposing truncation."""

    encoded = (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    parent, name = _open_memory_parent(path, create=False)
    temporary_name = f".{name}.{uuid.uuid4().hex}.tmp"
    descriptor: int | None = None
    try:
        descriptor = _open_new_regular_at(
            parent,
            temporary_name,
            os.O_WRONLY,
            label=f"memory metadata temporary {path.parent / temporary_name}",
        )
        _write_all(descriptor, encoded)
        os.fsync(descriptor)
        _verify_open_regular_at(
            parent,
            temporary_name,
            descriptor,
            label=f"memory metadata temporary {path.parent / temporary_name}",
        )
        try:
            existing = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            _validate_regular_metadata(
                existing,
                label=f"memory metadata {path}",
                expected_nlink=1,
            )
        os.replace(
            temporary_name,
            name,
            src_dir_fd=parent,
            dst_dir_fd=parent,
        )
        _verify_open_regular_at(
            parent,
            name,
            descriptor,
            label=f"memory metadata {path}",
        )
        _fsync_directory_fd(parent, path.parent)
    finally:
        try:
            if descriptor is not None:
                _unlink_temporary_durable_at(
                    parent,
                    path.parent,
                    temporary_name,
                    descriptor,
                )
        finally:
            if descriptor is not None:
                os.close(descriptor)
            os.close(parent)


def _open_durable_lock_file(path: Path) -> int:
    parent, name = _open_memory_parent(path, create=False)
    try:
        try:
            descriptor = _open_new_regular_at(
                parent,
                name,
                os.O_RDWR,
                label=f"memory lock {path}",
            )
        except FileExistsError:
            descriptor = _open_existing_regular_at(
                parent,
                name,
                os.O_RDWR,
                label=f"memory lock {path}",
            )
            created = False
        else:
            created = True
        try:
            if created:
                os.fsync(descriptor)
            _verify_open_regular_at(
                parent,
                name,
                descriptor,
                label=f"memory lock {path}",
            )
            _fsync_directory_fd(parent, path.parent)
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor
    finally:
        os.close(parent)


def _verify_open_memory_file(path: Path, descriptor: int, *, label: str) -> None:
    parent, name = _open_memory_parent(path, create=False)
    try:
        _verify_open_regular_at(
            parent,
            name,
            descriptor,
            label=label,
        )
    finally:
        os.close(parent)


def _ensure_empty_file_durable(path: Path) -> None:
    parent, name = _open_memory_parent(path, create=False)
    try:
        try:
            descriptor = _open_new_regular_at(
                parent,
                name,
                os.O_WRONLY,
                label=f"memory channel {path}",
            )
        except FileExistsError:
            descriptor = _open_existing_regular_at(
                parent,
                name,
                os.O_RDONLY,
                label=f"memory channel {path}",
            )
            created = False
        else:
            created = True
        try:
            if created:
                os.fsync(descriptor)
            _verify_open_regular_at(
                parent,
                name,
                descriptor,
                label=f"memory channel {path}",
            )
            _fsync_directory_fd(parent, path.parent)
        finally:
            os.close(descriptor)
    finally:
        os.close(parent)


def _recover_checkpoint_orphan_at(
    parent_descriptor: int,
    parent_path: Path,
    name: str,
    descriptor: int,
    current_temporary_name: str | None,
) -> bool:
    """Recover one SIGKILL cut between checkpoint fsync and temp unlink.

    The caller holds an exclusive flock on ``descriptor``.  Recovery is
    deliberately narrower than general hard-link cleanup: the final and the
    one constrained same-inode temp entry must be the only two links to the
    checkpoint inode. Different-inode stale temps are preserved. The caller
    must fully authenticate the checkpoint payload before invoking recovery.
    """

    final_metadata = _verify_open_regular_allowed_nlinks(
        parent_descriptor,
        name,
        descriptor,
        label=f"memory checkpoint {parent_path / name}",
        allowed_nlinks=frozenset({1, 2}),
    )
    if final_metadata.st_nlink == 1:
        return False

    temporary_pattern = re.compile(rf"^\.{re.escape(name)}\.[0-9a-f]{{32}}\.tmp$")
    same_inode_candidates: List[str] = []
    for candidate in sorted(os.listdir(parent_descriptor)):
        if candidate == current_temporary_name or not temporary_pattern.fullmatch(
            candidate
        ):
            continue
        try:
            candidate_metadata = os.stat(
                candidate,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            continue
        if not _same_inode(final_metadata, candidate_metadata):
            continue
        temporary = _open_existing_regular_allowed_nlinks(
            parent_descriptor,
            candidate,
            os.O_RDONLY,
            label=f"memory checkpoint orphan {parent_path / candidate}",
            allowed_nlinks=frozenset({2}),
        )
        try:
            if not _same_inode(final_metadata, os.fstat(temporary)):
                raise ValueError("memory checkpoint orphan changed while opened")
        finally:
            os.close(temporary)
        same_inode_candidates.append(candidate)

    if len(same_inode_candidates) != 1:
        raise ValueError("memory checkpoint has no unique same-inode orphan")
    temporary_name = same_inode_candidates[0]

    _unlink_temporary_durable_at(
        parent_descriptor,
        parent_path,
        temporary_name,
        descriptor,
    )
    _verify_open_regular_at(
        parent_descriptor,
        name,
        descriptor,
        label=f"memory checkpoint {parent_path / name}",
    )
    return True


def _publish_atomic_json_once(
    path: Path,
    payload: Dict[str, Any],
    *,
    problem_id: str,
    expected_normalized_items: List[Dict[str, Any]],
) -> bool:
    """Durably publish one immutable fenced JSON value without replacement."""

    encoded = (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    if len(encoded) > MAX_MEMORY_BATCH_FILE_BYTES:
        raise ValueError("encoded batch checkpoint exceeds its file-size limit")

    _ensure_memory_directory_durable(path.parent)
    parent, name = _open_memory_parent(path, create=False)
    temporary_name = f".{name}.{uuid.uuid4().hex}.tmp"
    descriptor: int | None = None
    published = False
    temporary_locked = False
    try:
        descriptor = _open_new_regular_at(
            parent,
            temporary_name,
            os.O_WRONLY,
            label=f"memory checkpoint temporary {path.parent / temporary_name}",
        )
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        temporary_locked = True
        _write_all(descriptor, encoded)
        os.fsync(descriptor)
        _verify_open_regular_at(
            parent,
            temporary_name,
            descriptor,
            label=f"memory checkpoint temporary {path.parent / temporary_name}",
        )
        try:
            os.link(
                temporary_name,
                name,
                src_dir_fd=parent,
                dst_dir_fd=parent,
                follow_symlinks=False,
            )
        except FileExistsError:
            winner = _open_existing_regular_allowed_nlinks(
                parent,
                name,
                os.O_RDONLY,
                label=f"memory checkpoint {path}",
                allowed_nlinks=frozenset({1, 2}),
            )
            try:
                fcntl.flock(winner, fcntl.LOCK_EX)
                _validate_open_memory_batch_checkpoint_at(
                    problem_id,
                    path,
                    parent,
                    name,
                    winner,
                    current_temporary_name=temporary_name,
                    expected_normalized_items=expected_normalized_items,
                )
            finally:
                try:
                    fcntl.flock(winner, fcntl.LOCK_UN)
                finally:
                    os.close(winner)
        else:
            published = True
            temporary = _verify_open_regular_at(
                parent,
                temporary_name,
                descriptor,
                label=f"memory checkpoint temporary {path.parent / temporary_name}",
                expected_nlink=2,
            )
            checkpoint = os.stat(name, dir_fd=parent, follow_symlinks=False)
            _validate_regular_metadata(
                checkpoint,
                label=f"memory checkpoint {path}",
                expected_nlink=2,
            )
            if not _same_inode(temporary, checkpoint):
                raise ValueError("published memory checkpoint changed during linking")
        _fsync_directory_fd(parent, path.parent)
    finally:
        try:
            if descriptor is not None:
                _unlink_temporary_durable_at(
                    parent,
                    path.parent,
                    temporary_name,
                    descriptor,
                )
            if published and descriptor is not None:
                _verify_open_regular_at(
                    parent,
                    name,
                    descriptor,
                    label=f"memory checkpoint {path}",
                )
        finally:
            if descriptor is not None:
                try:
                    if temporary_locked:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)
            os.close(parent)
    return published


def _batch_id_for_items(problem_id: str, encoded_items: bytes) -> str:
    material = (
        MEMORY_BATCH_SCHEMA.encode("utf-8")
        + b"\0"
        + problem_id.encode("utf-8")
        + b"\0"
        + encoded_items
    )
    return f"batch_{hashlib.sha256(material).hexdigest()}"


def _validate_canonical_utc_timestamp(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a canonical UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be a canonical UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError(f"{label} must be a canonical UTC timestamp")
    canonical = parsed.astimezone(timezone.utc).isoformat()
    if value != canonical:
        raise ValueError(f"{label} must be a canonical UTC timestamp")
    return value


def _memory_batch_checkpoint_sha256(payload: Dict[str, Any]) -> str:
    unsigned = dict(payload)
    unsigned.pop("checkpoint_sha256", None)
    canonical = (
        json.dumps(
            unsigned,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _batch_record_id(batch_id: str, index: int) -> str:
    digest = hashlib.sha256(f"{batch_id}\0record\0{index}".encode("utf-8")).hexdigest()
    return f"mem_{digest}"


def _batch_event_id(batch_id: str) -> str:
    digest = hashlib.sha256(f"{batch_id}\0event".encode("utf-8")).hexdigest()
    return f"event_{digest}"


def _checkpoint_normalized_items(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        {
            "channel": record["channel"],
            "record": record["record"],
            "active": record["active"],
            "supersedes": record["supersedes"],
        }
        for record in payload["records"]
    ]


def _read_open_memory_batch_bytes(descriptor: int, path: Path) -> bytes:
    """Read one already-fenced checkpoint descriptor with a hard size bound."""

    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: List[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, 65_536)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_MEMORY_BATCH_FILE_BYTES:
            raise ValueError(f"memory batch checkpoint exceeds its size limit: {path}")
        chunks.append(chunk)
    encoded = b"".join(chunks)
    if not encoded:
        raise ValueError(f"memory batch checkpoint has an invalid size: {path}")
    return encoded


def _validate_memory_batch_payload(
    problem_id: str,
    path: Path,
    encoded: bytes,
    *,
    require_content_addressed: bool,
) -> Dict[str, Any]:
    """Purely validate checkpoint bytes, including full-SHA self-authentication."""

    try:
        raw = encoded.decode("utf-8")
    except UnicodeError as exc:
        raise ValueError(f"cannot read memory batch checkpoint {path}: {exc}") from exc
    payload = _strict_json_loads(raw, label=f"memory batch checkpoint {path}")
    if not isinstance(payload, dict) or set(payload) != {
        "schema",
        "batch_id",
        "checkpoint_sha256",
        "timestamp_utc",
        "records",
        "event",
    }:
        raise ValueError(f"memory batch checkpoint has an invalid envelope: {path}")
    batch_id = payload.get("batch_id")
    records = payload.get("records")
    event = payload.get("event")
    checkpoint_sha256 = payload.get("checkpoint_sha256")
    if (
        payload.get("schema") != MEMORY_BATCH_SCHEMA
        or not isinstance(batch_id, str)
        or path.name != f"{batch_id}.json"
        or not isinstance(checkpoint_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", checkpoint_sha256) is None
        or not isinstance(records, list)
        or not 1 <= len(records) <= MAX_MEMORY_BATCH_RECORDS
        or not isinstance(event, dict)
    ):
        raise ValueError(f"memory batch checkpoint has invalid bindings: {path}")
    _validate_canonical_utc_timestamp(
        payload.get("timestamp_utc"),
        label=f"memory batch checkpoint timestamp {path}",
    )
    is_content_addressed = bool(_CONTENT_ADDRESSED_BATCH_ID_RE.fullmatch(batch_id))
    if require_content_addressed and not is_content_addressed:
        raise ValueError(
            "memory batch checkpoint requires a full-SHA "
            f"content-addressed batch: {path}"
        )

    record_keys = {
        "record_id",
        "timestamp_utc",
        "channel",
        "active",
        "supersedes",
        "batch_id",
        "record",
    }
    appended_records: List[Dict[str, str]] = []
    for record in records:
        if not isinstance(record, dict):
            raise ValueError(f"memory batch checkpoint has a non-object record: {path}")
        if is_content_addressed and set(record) != record_keys:
            raise ValueError(
                f"content-addressed memory batch checkpoint is not hash-bound: {path}"
            )
        channel = record.get("channel")
        record_id = record.get("record_id")
        if not isinstance(channel, str):
            raise ValueError(f"memory batch checkpoint has an invalid channel: {path}")
        _channel_path(problem_id, channel)
        if (
            not isinstance(record_id, str)
            or not record_id
            or record.get("batch_id") != batch_id
            or record.get("timestamp_utc") != payload["timestamp_utc"]
            or not isinstance(record.get("active"), bool)
            or not isinstance(record.get("supersedes"), list)
            or not isinstance(record.get("record"), dict)
        ):
            raise ValueError(f"memory batch checkpoint has an invalid record: {path}")
        appended_records.append({"record_id": record_id, "channel": channel})
    event_keys = {
        "record_id",
        "timestamp_utc",
        "event_type",
        "batch_id",
        "active",
        "supersedes",
        "appended_records",
    }
    if (
        (
            is_content_addressed
            and (
                set(event) != event_keys
                or not isinstance(event.get("record_id"), str)
                or not event.get("record_id")
            )
        )
        or event.get("event_type") != "memory_append_batch"
        or event.get("batch_id") != batch_id
        or event.get("timestamp_utc") != payload["timestamp_utc"]
        or event.get("active") is not True
        or event.get("supersedes") != []
        or event.get("appended_records") != appended_records
    ):
        raise ValueError(f"memory batch checkpoint has an invalid event: {path}")

    if is_content_addressed:
        for record in records:
            try:
                normalized_supersedes = _validate_supersedes(record["supersedes"])
            except ValueError as exc:
                raise ValueError(
                    "content-addressed memory batch checkpoint is not hash-bound: "
                    f"{path}"
                ) from exc
            if normalized_supersedes != record["supersedes"]:
                raise ValueError(
                    "content-addressed memory batch checkpoint is not hash-bound: "
                    f"{path}"
                )
        normalized_items = _checkpoint_normalized_items(payload)
        encoded_items = json.dumps(
            normalized_items,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        expected_batch_id = _batch_id_for_items(
            sanitize_problem_id(problem_id), encoded_items
        )
        expected_record_ids = [
            _batch_record_id(batch_id, index) for index in range(len(records))
        ]
        canonical = (
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
        if (
            batch_id != expected_batch_id
            or [record["record_id"] for record in records] != expected_record_ids
            or event.get("record_id") != _batch_event_id(batch_id)
            or checkpoint_sha256 != _memory_batch_checkpoint_sha256(payload)
            or encoded != canonical
        ):
            raise ValueError(
                f"content-addressed memory batch checkpoint is not hash-bound: {path}"
            )
    return payload


def _validate_open_memory_batch_checkpoint_at(
    problem_id: str,
    path: Path,
    parent_descriptor: int,
    name: str,
    descriptor: int,
    *,
    current_temporary_name: str | None = None,
    expected_normalized_items: List[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    """Validate a locked checkpoint and recover only its authenticated orphan."""

    label = f"memory checkpoint {path}"
    before = _verify_open_regular_allowed_nlinks(
        parent_descriptor,
        name,
        descriptor,
        label=label,
        allowed_nlinks=frozenset({1, 2}),
    )
    try:
        encoded = _read_open_memory_batch_bytes(descriptor, path)
    except OSError as exc:
        raise ValueError(f"cannot read memory batch checkpoint {path}: {exc}") from exc
    after = _verify_open_regular_allowed_nlinks(
        parent_descriptor,
        name,
        descriptor,
        label=label,
        allowed_nlinks=frozenset({1, 2}),
    )
    if before.st_nlink != after.st_nlink:
        raise ValueError(f"memory checkpoint changed while it was read: {path}")

    needs_recovery = after.st_nlink == 2
    payload = _validate_memory_batch_payload(
        problem_id,
        path,
        encoded,
        require_content_addressed=True,
    )
    if (
        expected_normalized_items is not None
        and _checkpoint_normalized_items(payload) != expected_normalized_items
    ):
        raise ValueError(
            "content-addressed memory batch checkpoint collides with different items"
        )
    if needs_recovery:
        _recover_checkpoint_orphan_at(
            parent_descriptor,
            path.parent,
            name,
            descriptor,
            current_temporary_name,
        )
    _verify_open_regular_at(
        parent_descriptor,
        name,
        descriptor,
        label=label,
    )
    return payload


def _validate_memory_batch_checkpoint(
    problem_id: str,
    path: Path,
) -> Dict[str, Any]:
    parent, name = _open_memory_parent(path, create=False)
    try:
        descriptor = _open_existing_regular_allowed_nlinks(
            parent,
            name,
            os.O_RDONLY,
            label=f"memory checkpoint {path}",
            allowed_nlinks=frozenset({1, 2}),
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            return _validate_open_memory_batch_checkpoint_at(
                problem_id,
                path,
                parent,
                name,
                descriptor,
            )
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
    finally:
        os.close(parent)


def _iter_memory_batch_checkpoints(problem_id: str) -> Iterable[Dict[str, Any]]:
    checkpoint_dir = _batch_checkpoint_dir(problem_id)
    try:
        descriptor = _open_memory_directory(checkpoint_dir, create=False)
    except FileNotFoundError:
        return
    try:
        names = sorted(
            name
            for name in os.listdir(descriptor)
            if name.startswith("batch_") and name.endswith(".json")
        )
        _validate_directory_metadata(
            os.fstat(descriptor),
            label=f"memory checkpoint directory {checkpoint_dir}",
            require_owner=True,
        )
    finally:
        os.close(descriptor)
    for name in names:
        path = checkpoint_dir / name
        yield _validate_memory_batch_checkpoint(problem_id, path)


def _new_record_id(prefix: str = "mem") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _validate_supersedes(supersedes: Optional[List[str]]) -> List[str]:
    if supersedes is None:
        return []
    if not isinstance(supersedes, list):
        raise ValueError("supersedes must be a JSON array of record ids")

    normalized: List[str] = []
    seen = set()
    for record_id in supersedes:
        if not isinstance(record_id, str) or not record_id.strip():
            raise ValueError("supersedes entries must be non-empty record id strings")
        cleaned = record_id.strip()
        if cleaned not in seen:
            normalized.append(cleaned)
            seen.add(cleaned)
    return normalized


def _read_supersedes(raw: Any) -> List[str]:
    """Read legacy supersedes metadata without rejecting the whole memory file."""
    if isinstance(raw, str):
        candidates = [raw]
    elif isinstance(raw, list):
        candidates = raw
    else:
        candidates = []

    normalized: List[str] = []
    seen = set()
    for candidate in candidates:
        if not isinstance(candidate, str) or not candidate.strip():
            continue
        cleaned = candidate.strip()
        if cleaned not in seen:
            normalized.append(cleaned)
            seen.add(cleaned)
    return normalized


def _legacy_record_id(channel: str, ordinal: int, item: Dict[str, Any]) -> str:
    canonical = json.dumps(
        item,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    material = f"{channel}\0{ordinal}\0{canonical}".encode("utf-8")
    return f"legacy_{hashlib.sha256(material).hexdigest()[:24]}"


def _load_memory_entries(problem_id: str) -> Dict[str, List[Dict[str, Any]]]:
    raw_items_by_channel: Dict[str, List[Dict[str, Any]]] = {
        channel: list(_iter_jsonl(_channel_path(problem_id, channel)))
        for channel in CHANNEL_FILES
    }
    for checkpoint in _iter_memory_batch_checkpoints(problem_id):
        for item in checkpoint["records"]:
            raw_items_by_channel[item["channel"]].append(item)
        raw_items_by_channel["events"].append(checkpoint["event"])

    entries_by_channel: Dict[str, List[Dict[str, Any]]] = {}
    global_ordinal = 0
    for channel in CHANNEL_FILES:
        entries: List[Dict[str, Any]] = []
        for ordinal, raw_item in enumerate(raw_items_by_channel[channel]):
            raw_record_id = raw_item.get("record_id")
            if isinstance(raw_record_id, str) and raw_record_id.strip():
                record_id = raw_record_id.strip()
            else:
                record_id = _legacy_record_id(channel, ordinal, raw_item)

            raw_active = raw_item.get("active", True)
            declared_active = raw_active if isinstance(raw_active, bool) else True
            supersedes = _read_supersedes(raw_item.get("supersedes"))
            entries.append(
                {
                    "record_id": record_id,
                    "declared_active": declared_active,
                    "supersedes": supersedes,
                    "item": raw_item,
                    "channel": channel,
                    "ordinal": ordinal,
                    "global_ordinal": global_ordinal,
                }
            )
            global_ordinal += 1
        entries_by_channel[channel] = entries

    superseded_by: Dict[str, List[str]] = {}
    for entries in entries_by_channel.values():
        for entry in entries:
            for superseded_id in entry["supersedes"]:
                superseders = superseded_by.setdefault(superseded_id, [])
                if entry["record_id"] not in superseders:
                    superseders.append(entry["record_id"])

    for entries in entries_by_channel.values():
        for entry in entries:
            entry["superseded_by"] = superseded_by.get(entry["record_id"], [])
            entry["effective_active"] = (
                entry["declared_active"] and not entry["superseded_by"]
            )
    return entries_by_channel


def _timestamp_rank(item: Dict[str, Any]) -> float:
    raw_timestamp = item.get("timestamp_utc")
    if not isinstance(raw_timestamp, str) or not raw_timestamp.strip():
        return float("-inf")
    try:
        parsed = datetime.fromisoformat(raw_timestamp.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        return float("-inf")


def _candidate_rank(candidate: Dict[str, Any], newest_first: bool) -> tuple:
    score = candidate["score"]
    if newest_first:
        # Relevance remains primary. Rounding only groups numerically negligible
        # BM25 differences so recency can settle exact or near ties.
        return (
            round(score, BM25_NEAR_TIE_DECIMALS),
            candidate["timestamp_rank"],
            candidate["global_ordinal"],
            score,
        )
    return (
        score,
        -candidate["timestamp_rank"],
        -candidate["global_ordinal"],
    )


def _compact_json_chars(value: Any) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _tokenize_bm25(text: str) -> List[str]:
    return re.findall(r"[A-Za-z0-9_]+", text.lower())


def _bm25_score_documents(
    query: str,
    documents: List[List[str]],
    *,
    k1: float = 1.5,
    b: float = 0.75,
) -> List[float]:
    query_tokens = _tokenize_bm25(query)
    if not query_tokens or not documents:
        return [0.0 for _ in documents]

    query_term_counts = Counter(query_tokens)
    document_frequencies: Counter[str] = Counter()
    document_term_counts = [Counter(document) for document in documents]
    document_lengths = [len(document) for document in documents]
    avg_doc_length = (
        sum(document_lengths) / len(document_lengths) if document_lengths else 0.0
    )
    total_documents = len(documents)

    for document in documents:
        for token in set(document):
            document_frequencies[token] += 1

    scores: List[float] = []
    for doc_counts, doc_length in zip(document_term_counts, document_lengths):
        score = 0.0
        norm = (
            k1 * (1.0 - b + b * (doc_length / avg_doc_length))
            if avg_doc_length > 0
            else k1
        )
        for token, query_tf in query_term_counts.items():
            term_frequency = doc_counts.get(token, 0)
            if term_frequency <= 0:
                continue
            document_frequency = document_frequencies.get(token, 0)
            idf = math.log(
                1.0
                + (
                    (total_documents - document_frequency + 0.5)
                    / (document_frequency + 0.5)
                )
            )
            numerator = term_frequency * (k1 + 1.0)
            denominator = term_frequency + norm
            score += query_tf * idf * (numerator / denominator)
        scores.append(score)

    return scores


def search_matlas_theorems(
    query: str,
    num_results: int = 10,
    endpoint: str = MATLAS_SEARCH_URL,
    timeout_seconds: int = 30,
) -> Dict[str, Any]:
    normalized_query = _normalize_external_query(query)
    num_results = _validate_external_result_count(num_results)

    # Matlas 0.1 requires at least ten upstream results. Keep the public MCP
    # surface useful for smaller bounded requests by truncating locally.
    upstream_count = max(10, num_results)
    payload = {
        "query": normalized_query,
        "num_results": upstream_count,
    }
    envelope: Dict[str, Any] = {
        "schema_version": "rethlas_external_retrieval_v1",
        "provider": "matlas_official_v0_1",
        "provider_protocol": "matlas_openapi_0_1_0",
        "endpoint": endpoint,
        "query": normalized_query,
        "requested_count": num_results,
        "count": 0,
        "results": [],
        "scope": "published_mathematical_statements",
        "mathematical_evidence_authority": False,
        "fallback_used": False,
    }
    try:
        response = requests.post(
            endpoint,
            json=payload,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "rethlas/1.0",
            },
            timeout=timeout_seconds,
            stream=True,
        )
        primary_error_pending = True
        try:
            response.raise_for_status()
            data = _read_bounded_external_json(response)
            primary_error_pending = False
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as error:
                    if not primary_error_pending:
                        raise _ExternalResponseCloseError from error
    except _ExternalResponseTooLarge:
        return {**envelope, "retrieval_status": "error", "error": "response_too_large"}
    except _ExternalResponseCloseError:
        return {
            **envelope,
            "retrieval_status": "error",
            "error": "network response_close_failed",
        }
    except requests.HTTPError as error:
        status = error.response.status_code if error.response is not None else "unknown"
        return {
            **envelope,
            "retrieval_status": "error",
            "error": f"http {status}",
        }
    except ValueError:
        return {
            **envelope,
            "retrieval_status": "error",
            "error": "invalid_json",
        }
    except requests.RequestException as error:
        return {
            **envelope,
            "retrieval_status": "error",
            "error": f"network {type(error).__name__}",
        }
    if not isinstance(data, list):
        return {
            **envelope,
            "retrieval_status": "error",
            "error": f"invalid_response_type:{type(data).__name__}",
        }

    required_string_fields = (
        "entity_name",
        "doi",
        "title",
        "authors",
        "journal",
        "year",
        "statement",
        "candidate_id",
    )
    normalized: List[Dict[str, str]] = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            return {
                **envelope,
                "retrieval_status": "error",
                "error": f"invalid_result:{index}:not_object",
            }
        source_type = item.get("type")
        if not isinstance(source_type, str) or source_type not in {"book", "paper"}:
            return {
                **envelope,
                "retrieval_status": "error",
                "error": f"invalid_result:{index}:type",
            }
        if any(
            not isinstance(item.get(field), str) for field in required_string_fields
        ):
            return {
                **envelope,
                "retrieval_status": "error",
                "error": f"invalid_result:{index}:fields",
            }
        if not _external_fields_are_utf8(item, required_string_fields):
            return {
                **envelope,
                "retrieval_status": "error",
                "error": f"invalid_result:{index}:encoding",
            }
        normalized.append(
            {
                "type": source_type,
                **{field: item[field] for field in required_string_fields},
            }
        )

    normalized = normalized[:num_results]

    result = {
        **envelope,
        "count": len(normalized),
        "results": normalized,
        "retrieval_status": "ok",
    }
    if len(canonical_json_bytes(result)) > MAX_EXTERNAL_SUCCESS_UTF8_BYTES:
        return {**envelope, "retrieval_status": "error", "error": "response_too_large"}
    return result


def search_arxiv_theorems(
    query: str,
    num_results: int = 10,
    endpoint: str = LEGACY_ARXIV_THEOREM_URL,
    timeout_seconds: int = 30,
) -> Dict[str, Any]:
    """Query Danus's historical arXiv theorem service without implicit fallback."""

    normalized_query = _normalize_external_query(query)
    num_results = _validate_external_result_count(num_results)

    envelope: Dict[str, Any] = {
        "schema_version": "rethlas_external_retrieval_v1",
        "provider": "danus_legacy_arxiv_theorem_v1",
        "provider_protocol": "danus_legacy_arxiv_theorem_search_v1",
        "endpoint": endpoint,
        "query": normalized_query,
        "requested_count": num_results,
        "count": 0,
        "results": [],
        "scope": "arxiv_theorem_snippets",
        "mathematical_evidence_authority": False,
        "fallback_used": False,
    }
    try:
        response = requests.post(
            endpoint,
            json={
                "query": normalized_query,
                "task": THEOREM_SEARCH_TASK,
                "num_results": num_results,
            },
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "rethlas/1.0",
            },
            timeout=timeout_seconds,
            stream=True,
        )
        primary_error_pending = True
        try:
            response.raise_for_status()
            data = _read_bounded_external_json(response)
            primary_error_pending = False
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as error:
                    if not primary_error_pending:
                        raise _ExternalResponseCloseError from error
    except _ExternalResponseTooLarge:
        return {**envelope, "retrieval_status": "error", "error": "response_too_large"}
    except _ExternalResponseCloseError:
        return {
            **envelope,
            "retrieval_status": "error",
            "error": "network response_close_failed",
        }
    except requests.HTTPError as error:
        status = error.response.status_code if error.response is not None else "unknown"
        return {**envelope, "retrieval_status": "error", "error": f"http {status}"}
    except ValueError:
        return {**envelope, "retrieval_status": "error", "error": "invalid_json"}
    except requests.RequestException as error:
        return {
            **envelope,
            "retrieval_status": "error",
            "error": f"network {type(error).__name__}",
        }
    if not isinstance(data, list):
        return {
            **envelope,
            "retrieval_status": "error",
            "error": f"invalid_response_type:{type(data).__name__}",
        }

    required_fields = ("title", "theorem", "arxiv_id", "theorem_id")
    normalized: List[Dict[str, str]] = []
    for index, item in enumerate(data):
        if not isinstance(item, dict) or any(
            not isinstance(item.get(field), str) for field in required_fields
        ):
            return {
                **envelope,
                "retrieval_status": "error",
                "error": f"invalid_result:{index}",
            }
        if not _external_fields_are_utf8(item, required_fields):
            return {
                **envelope,
                "retrieval_status": "error",
                "error": f"invalid_result:{index}:encoding",
            }
        normalized.append({field: item[field] for field in required_fields})

    normalized = normalized[:num_results]
    result = {
        **envelope,
        "count": len(normalized),
        "results": normalized,
        "retrieval_status": "ok",
    }
    if len(canonical_json_bytes(result)) > MAX_EXTERNAL_SUCCESS_UTF8_BYTES:
        return {**envelope, "retrieval_status": "error", "error": "response_too_large"}
    return result


def verify_proof_service(
    statement: str,
    proof: str,
    endpoint: str = VERIFY_PROOF_URL,
    timeout_seconds: int = 3600,
) -> Dict[str, Any]:
    if not isinstance(statement, str) or not statement.strip():
        raise ValueError("statement must be non-empty")
    if not isinstance(proof, str):
        raise ValueError("proof must be markdown text")
    if not proof.strip():
        raise ValueError("proof markdown must be non-empty")

    payload = {
        "statement": statement,
        "proof": proof,
    }

    request_kwargs: Dict[str, Any] = {
        "json": payload,
        "timeout": timeout_seconds,
    }
    api_token = os.getenv("VERIFY_API_TOKEN")
    if api_token:
        request_kwargs["headers"] = {"Authorization": f"Bearer {api_token}"}
    response = requests.post(endpoint, **request_kwargs)
    response.raise_for_status()

    try:
        body = response.json()
    except ValueError as exc:
        raise ValueError("verification service returned non-JSON response") from exc

    expected_ids, expected_context_digest = expected_attestation(
        proof=proof,
        statement=statement,
    )
    expected_manifest = parse_blueprint(proof, target_statement=statement)
    return validate_service_response(
        body,
        expected_proof_digest=proof_digest(proof),
        expected_checked_item_ids=expected_ids,
        expected_context_digest=expected_context_digest,
        expected_manifest=expected_manifest,
    )


def _trusted_problem_statement(problem_id: str) -> tuple[str, str]:
    sanitized_problem_id = validate_verified_problem_id(problem_id)
    expected_problem_id = os.getenv("RETHLAS_EXPECTED_PROBLEM_ID")
    if expected_problem_id and sanitized_problem_id != expected_problem_id:
        raise ValueError("problem_id does not match the runner-bound problem")

    data_root = DATA_ROOT.resolve()
    source_path = DATA_ROOT / f"{sanitized_problem_id}.md"
    resolved_source = source_path.resolve(strict=True)
    if not resolved_source.is_relative_to(data_root) or source_path.is_symlink():
        raise ValueError("problem source must be a regular file inside data root")
    descriptor = os.open(
        source_path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("problem source must be a regular file")
        if metadata.st_size > 400_000:
            raise ValueError("problem source is too large")
        raw_statement = os.read(descriptor, 400_001)
        if len(raw_statement) > 400_000:
            raise ValueError("problem source is too large")
    finally:
        os.close(descriptor)
    try:
        statement = raw_statement.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("problem source must be valid UTF-8") from exc
    if not statement.strip():
        raise ValueError("problem source must be non-empty")
    if len(statement) > 100_000:
        raise ValueError("problem source exceeds verifier statement limit")
    statement_digest = hashlib.sha256(raw_statement).hexdigest()
    expected_digest = os.getenv("RETHLAS_EXPECTED_STATEMENT_SHA256")
    if expected_digest and statement_digest != expected_digest:
        raise ValueError("problem source changed after the runner bound its digest")
    return statement, statement_digest


def verify_blueprint_service(
    problem_id: str,
    endpoint: str = VERIFY_PROOF_URL,
    timeout_seconds: int = 3600,
) -> Dict[str, Any]:
    """Verify a draft against its trusted data file and publish with a receipt."""

    sanitized_problem_id = validate_verified_problem_id(problem_id)
    phase = _reasoning_phase_preflight("verify_blueprint_service")
    verification_deadline_utc = (
        (
            datetime.now(timezone.utc) + timedelta(seconds=min(timeout_seconds, 3600))
        ).isoformat()
        if phase is None
        else phase["hard_stop_at_utc"]
    )
    deadline = datetime.fromisoformat(
        _validate_canonical_utc_timestamp(
            verification_deadline_utc,
            label="whole-blueprint verification hard stop",
        )
    )
    remaining_seconds = (deadline - datetime.now(timezone.utc)).total_seconds()
    if remaining_seconds <= 0:
        raise ValueError("whole-blueprint verification hard stop has expired")
    transport_timeout = min(timeout_seconds, max(1, int(remaining_seconds) + 5))
    statement, _statement_digest = _trusted_problem_statement(sanitized_problem_id)
    results_root = Path(os.path.abspath(os.fspath(RESULTS_ROOT)))
    result_dir = results_root.joinpath(*sanitized_problem_id.split("/"))
    receipts_root = RECEIPTS_ROOT.resolve()
    if receipts_root.is_relative_to(REPO_ROOT.resolve()):
        raise ValueError(
            "trusted receipt root must be outside the generation workspace"
        )
    receipt_path = (receipts_root / f"{sanitized_problem_id}.json").resolve()
    if not receipt_path.is_relative_to(receipts_root):
        raise ValueError("problem_id resolves outside receipt root")
    return verify_blueprint_file(
        statement=statement,
        draft_path=result_dir / "blueprint.md",
        verified_path=result_dir / "blueprint_verified.md",
        endpoint=endpoint,
        verification_deadline_utc=verification_deadline_utc,
        timeout_seconds=transport_timeout,
        api_token=os.getenv("VERIFY_API_TOKEN") or None,
        receipt_path=receipt_path,
        problem_id=sanitized_problem_id,
        blueprint_root=results_root,
    )


def memory_init(
    problem_id: str,
    meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    sanitized_problem_id = sanitize_problem_id(problem_id)
    problem_dir = _problem_dir(sanitized_problem_id)
    _ensure_memory_directory_durable(problem_dir)

    lock_descriptor = _open_durable_lock_file(problem_dir / ".meta.lock")
    try:
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
    except BaseException:
        os.close(lock_descriptor)
        raise
    try:
        _verify_open_memory_file(
            problem_dir / ".meta.lock",
            lock_descriptor,
            label=f"memory lock {problem_dir / '.meta.lock'}",
        )
        created_files: Dict[str, str] = {}
        for channel, filename in CHANNEL_FILES.items():
            channel_path = problem_dir / filename
            _ensure_empty_file_durable(channel_path)
            created_files[channel] = str(channel_path)

        meta_path = problem_dir / "meta.json"
        existing_meta: Dict[str, Any] = {}
        encoded_meta = _read_memory_bytes(meta_path, allow_missing=True)
        if encoded_meta is not None:
            try:
                raw_meta = encoded_meta.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError("memory metadata must be valid UTF-8") from exc
            loaded = _strict_json_loads(raw_meta, label=f"memory metadata {meta_path}")
            if not isinstance(loaded, dict):
                raise ValueError("memory metadata must be a JSON object")
            existing_meta = loaded

        merged_meta: Dict[str, Any] = {
            "problem_id": sanitized_problem_id,
            "created_at_utc": existing_meta.get("created_at_utc", _utc_now()),
            "updated_at_utc": _utc_now(),
        }
        merged_meta.update(existing_meta)
        if meta:
            merged_meta.update(meta)

        _write_atomic_json_replace(meta_path, merged_meta)
    finally:
        try:
            _verify_open_memory_file(
                problem_dir / ".meta.lock",
                lock_descriptor,
                label=f"memory lock {problem_dir / '.meta.lock'}",
            )
        finally:
            try:
                fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
            finally:
                os.close(lock_descriptor)

    return {
        "problem_id": sanitized_problem_id,
        "memory_dir": str(problem_dir),
        "meta_path": str(meta_path),
        "channels": created_files,
    }


def memory_append(
    problem_id: str,
    channel: str,
    record: Dict[str, Any],
    active: bool = True,
    supersedes: Optional[List[str]] = None,
    return_mode: str = "metadata",
) -> Dict[str, Any]:
    """Append one fact and return a compact receipt unless full mode is requested.

    ``supersedes`` is append-only metadata: referenced records are treated as
    inactive by searches, while legacy records without lifecycle metadata remain
    active by default.
    """
    if channel in _CONTROL_ONLY_MEMORY_CHANNELS or _is_route_transition_projection(
        channel, record
    ):
        raise ValueError(f"{channel} is reserved for trusted control publication")
    if not isinstance(record, dict):
        raise ValueError("record must be a JSON object")
    if not isinstance(active, bool):
        raise ValueError("active must be a boolean")
    if not isinstance(return_mode, str) or return_mode not in {"metadata", "full"}:
        raise ValueError("return_mode must be either 'metadata' or 'full'")

    normalized_supersedes = _validate_supersedes(supersedes)
    target = _channel_path(problem_id, channel)

    memory_init(problem_id)

    record_id = _new_record_id()
    entry = {
        "record_id": record_id,
        "timestamp_utc": _utc_now(),
        "channel": channel,
        "active": active,
        "supersedes": normalized_supersedes,
        "record": record,
    }
    _append_jsonl(target, entry)

    if channel != "events":
        event_entry = {
            "record_id": _new_record_id("event"),
            "timestamp_utc": _utc_now(),
            "event_type": "memory_append",
            "channel": channel,
            "active": True,
            "supersedes": [],
            "appended_record_id": record_id,
        }
        _append_jsonl(_channel_path(problem_id, "events"), event_entry)

    response = {
        "status": "ok",
        "problem_id": sanitize_problem_id(problem_id),
        "record_id": record_id,
        "timestamp_utc": entry["timestamp_utc"],
        "channel": channel,
        "path": str(target),
        "active": active,
        "supersedes": normalized_supersedes,
    }
    if return_mode == "full":
        response["entry"] = entry
    return response


def memory_append_batch(
    problem_id: str,
    items: List[Dict[str, Any]],
    *,
    _trusted_control_publication: bool = False,
) -> Dict[str, Any]:
    """Append a bounded write-behind checkpoint in one MCP round trip.

    Every item is validated and JSON-encoded before the first file write. The
    complete logical channel update and its event are then published as one
    immutable, content-addressed sidecar with an atomic no-clobber link, so a
    reader observes either none or all of the checkpoint. An exact retry returns
    the original receipt, including after a post-publication durability error.
    The response intentionally omits record bodies so a checkpoint is not
    echoed back into the model context.
    """

    if not isinstance(items, list) or not items:
        raise ValueError("items must be a non-empty JSON array")
    if len(items) > MAX_MEMORY_BATCH_RECORDS:
        raise ValueError(
            f"items must contain at most {MAX_MEMORY_BATCH_RECORDS} records"
        )

    allowed_keys = {"channel", "record", "active", "supersedes"}
    normalized_items: List[Dict[str, Any]] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"items[{index}] must be a JSON object")
        unknown_keys = set(item) - allowed_keys
        if unknown_keys:
            unknown = ", ".join(sorted(str(key) for key in unknown_keys))
            raise ValueError(f"items[{index}] has unknown fields: {unknown}")
        channel = item.get("channel")
        record = item.get("record")
        active = item.get("active", True)
        if not isinstance(channel, str):
            raise ValueError(f"items[{index}].channel must be a string")
        _channel_path(problem_id, channel)
        if (
            channel in _CONTROL_ONLY_MEMORY_CHANNELS
            or _is_route_transition_projection(channel, record)
        ) and not _trusted_control_publication:
            raise ValueError(f"{channel} is reserved for trusted control publication")
        if not isinstance(record, dict):
            raise ValueError(f"items[{index}].record must be a JSON object")
        if not isinstance(active, bool):
            raise ValueError(f"items[{index}].active must be a boolean")
        supersedes = _validate_supersedes(item.get("supersedes"))
        normalized_items.append(
            {
                "channel": channel,
                "record": record,
                "active": active,
                "supersedes": supersedes,
            }
        )

    try:
        encoded = json.dumps(
            normalized_items,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError(f"items must be strict JSON data: {exc}") from exc
    if len(encoded) > MAX_MEMORY_BATCH_UTF8_BYTES:
        raise ValueError(
            "encoded items exceed the "
            f"{MAX_MEMORY_BATCH_UTF8_BYTES}-byte checkpoint limit"
        )

    sanitized_problem_id = sanitize_problem_id(problem_id)
    batch_id = _batch_id_for_items(sanitized_problem_id, encoded)
    timestamp = _utc_now()
    entries: List[Dict[str, Any]] = []
    for index, item in enumerate(normalized_items):
        record_id = _batch_record_id(batch_id, index)
        entry = {
            "record_id": record_id,
            "timestamp_utc": timestamp,
            "channel": item["channel"],
            "active": item["active"],
            "supersedes": item["supersedes"],
            "batch_id": batch_id,
            "record": item["record"],
        }
        entries.append(entry)

    event = {
        "record_id": _batch_event_id(batch_id),
        "timestamp_utc": timestamp,
        "event_type": "memory_append_batch",
        "batch_id": batch_id,
        "active": True,
        "supersedes": [],
        "appended_records": [
            {"record_id": entry["record_id"], "channel": entry["channel"]}
            for entry in entries
        ],
    }
    checkpoint_path = _batch_checkpoint_dir(sanitized_problem_id) / f"{batch_id}.json"
    candidate = {
        "schema": MEMORY_BATCH_SCHEMA,
        "batch_id": batch_id,
        "timestamp_utc": timestamp,
        "records": entries,
        "event": event,
    }
    candidate["checkpoint_sha256"] = _memory_batch_checkpoint_sha256(candidate)
    _publish_atomic_json_once(
        checkpoint_path,
        candidate,
        problem_id=sanitized_problem_id,
        expected_normalized_items=normalized_items,
    )
    committed = _validate_memory_batch_checkpoint(sanitized_problem_id, checkpoint_path)
    if _checkpoint_normalized_items(committed) != normalized_items:
        raise ValueError(
            "content-addressed memory batch checkpoint collides with different items"
        )
    receipts = [
        {
            "record_id": entry["record_id"],
            "channel": entry["channel"],
            "active": entry["active"],
            "supersedes": entry["supersedes"],
        }
        for entry in committed["records"]
    ]
    return {
        "status": "ok",
        "problem_id": sanitized_problem_id,
        "batch_id": batch_id,
        "timestamp_utc": committed["timestamp_utc"],
        "count": len(receipts),
        "records": receipts,
        "checkpoint_path": str(checkpoint_path),
    }


def memory_search(
    problem_id: str,
    query: str,
    channels: Optional[List[str]] = None,
    limit_per_channel: int = 10,
    max_chars: int = DEFAULT_MEMORY_SEARCH_MAX_CHARS,
    include_inactive: bool = False,
    newest_first: bool = True,
) -> Dict[str, Any]:
    """Search active memory with an explicit whole-result character budget.

    ``complete`` is false whenever positive-scoring matches are omitted by either
    ``limit_per_channel`` or ``max_chars``. ``returned_chars`` counts compact JSON
    characters for the returned result objects; no stored item is partially cut.
    BM25 relevance is primary and recency only breaks exact or near ties.
    """
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be non-empty")
    if (
        not isinstance(limit_per_channel, int)
        or isinstance(limit_per_channel, bool)
        or limit_per_channel <= 0
    ):
        raise ValueError("limit_per_channel must be > 0")
    if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars <= 0:
        raise ValueError("max_chars must be a positive integer")
    if not isinstance(include_inactive, bool):
        raise ValueError("include_inactive must be a boolean")
    if not isinstance(newest_first, bool):
        raise ValueError("newest_first must be a boolean")

    if channels is None:
        search_channels = [name for name in CHANNEL_FILES if name != "events"]
    else:
        if not isinstance(channels, list):
            raise ValueError("channels must be a JSON array")
        search_channels = []
        for channel in channels:
            if not isinstance(channel, str):
                raise ValueError("channels entries must be strings")
            _channel_path(problem_id, channel)
            if channel not in search_channels:
                search_channels.append(channel)

    entries_by_channel = _load_memory_entries(problem_id)
    ranked_by_channel: Dict[str, List[Dict[str, Any]]] = {}
    corpus_count = 0
    for channel in search_channels:
        channel_entries = [
            entry
            for entry in entries_by_channel[channel]
            if include_inactive or entry["effective_active"]
        ]
        corpus_count += len(channel_entries)
        documents = [
            json.dumps(entry["item"], ensure_ascii=False) for entry in channel_entries
        ]
        tokenized_documents = [_tokenize_bm25(document) for document in documents]
        scores = _bm25_score_documents(query, tokenized_documents)

        candidates: List[Dict[str, Any]] = []
        for entry, score in zip(channel_entries, scores):
            if score <= 0:
                continue
            normalized_item = dict(entry["item"])
            normalized_item["record_id"] = entry["record_id"]
            normalized_item["active"] = entry["effective_active"]
            normalized_item["supersedes"] = entry["supersedes"]
            if entry["declared_active"] != entry["effective_active"]:
                normalized_item["declared_active"] = entry["declared_active"]
            if entry["superseded_by"]:
                normalized_item["superseded_by"] = entry["superseded_by"]

            result = {
                "score": score,
                "item": normalized_item,
            }
            candidates.append(
                {
                    "key": (channel, entry["ordinal"]),
                    "channel": channel,
                    "record_id": entry["record_id"],
                    "score": score,
                    "timestamp_rank": _timestamp_rank(entry["item"]),
                    "global_ordinal": entry["global_ordinal"],
                    "result": result,
                    "chars": _compact_json_chars(result),
                }
            )

        candidates.sort(
            key=lambda candidate: _candidate_rank(candidate, newest_first),
            reverse=True,
        )
        ranked_by_channel[channel] = candidates

    budget_candidates: List[Dict[str, Any]] = []
    for channel in search_channels:
        budget_candidates.extend(ranked_by_channel[channel][:limit_per_channel])
    budget_candidates.sort(
        key=lambda candidate: _candidate_rank(candidate, newest_first),
        reverse=True,
    )

    returned_keys = set()
    returned_chars = 0
    for candidate in budget_candidates:
        candidate_chars = candidate["chars"]
        if returned_chars + candidate_chars > max_chars:
            continue
        returned_keys.add(candidate["key"])
        returned_chars += candidate_chars

    all_matches: List[Dict[str, Any]] = []
    for channel in search_channels:
        all_matches.extend(ranked_by_channel[channel])
    all_matches.sort(
        key=lambda candidate: _candidate_rank(candidate, newest_first),
        reverse=True,
    )

    results_by_channel: Dict[str, Dict[str, Any]] = {}
    for channel in search_channels:
        ranked = ranked_by_channel[channel]
        returned = [
            candidate for candidate in ranked if candidate["key"] in returned_keys
        ]
        omitted = [
            candidate for candidate in ranked if candidate["key"] not in returned_keys
        ]
        results_by_channel[channel] = {
            "corpus_count": sum(
                1
                for entry in entries_by_channel[channel]
                if include_inactive or entry["effective_active"]
            ),
            "matched_count": len(ranked),
            "count": len(returned),
            "complete": not omitted,
            "truncated": bool(omitted),
            "omitted_count": len(omitted),
            "omitted_ids": [
                candidate["record_id"] for candidate in omitted[:MAX_OMITTED_IDS]
            ],
            "omitted_ids_complete": len(omitted) <= MAX_OMITTED_IDS,
            "returned_chars": sum(candidate["chars"] for candidate in returned),
            "results": [candidate["result"] for candidate in returned],
        }

    omitted_matches = [
        candidate for candidate in all_matches if candidate["key"] not in returned_keys
    ]

    return {
        "problem_id": sanitize_problem_id(problem_id),
        "query": query,
        "channels": search_channels,
        "limit_per_channel": limit_per_channel,
        "max_chars": max_chars,
        "include_inactive": include_inactive,
        "newest_first": newest_first,
        "corpus_count": corpus_count,
        "matched_count": len(all_matches),
        "count": len(returned_keys),
        "complete": not omitted_matches,
        "truncated": bool(omitted_matches),
        "omitted_count": len(omitted_matches),
        "omitted_ids": [
            candidate["record_id"] for candidate in omitted_matches[:MAX_OMITTED_IDS]
        ],
        "omitted_ids_complete": len(omitted_matches) <= MAX_OMITTED_IDS,
        "returned_chars": returned_chars,
        "results_by_channel": results_by_channel,
    }


def _required_review_env(name: str, *, label: str) -> str:
    value = os.getenv(name, "")
    if not value or value != value.strip() or "\x00" in value:
        raise ValueError(f"runner did not bind {label}")
    return value


def _trusted_blueprint(problem_id: str) -> tuple[str | None, str | None]:
    """Read the exact bounded draft bytes without accepting an excerpt."""

    results_root = RESULTS_ROOT.resolve()
    candidate = results_root.joinpath(*problem_id.split("/"), "blueprint.md")
    try:
        metadata = candidate.lstat()
    except FileNotFoundError:
        return None, None
    if not stat.S_ISREG(metadata.st_mode) or candidate.is_symlink():
        raise ValueError("review blueprint must be a regular non-symlink file")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_relative_to(results_root) or resolved != candidate.absolute():
        raise ValueError("review blueprint path crossed an untrusted link")
    descriptor = os.open(candidate, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_size > 65_536:
            raise ValueError("full review blueprint exceeds its 64 KiB bound")
        raw = os.read(descriptor, 65_537)
        if len(raw) > 65_536 or os.read(descriptor, 1):
            raise ValueError("full review blueprint exceeds its 64 KiB bound")
    finally:
        os.close(descriptor)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("review blueprint must be valid UTF-8") from exc
    return text, hashlib.sha256(raw).hexdigest()


def _trusted_blueprint_items(
    blueprint_text: str | None, statement_text: str
) -> List[Dict[str, str]]:
    """Derive exact verifier claim commitments from the authoritative draft."""

    if blueprint_text is None:
        return []
    try:
        manifest = parse_blueprint(blueprint_text)
    except ValueError:
        # Completely unstructured legacy drafts need a target solely to obtain
        # their one deterministic synthetic item. Structured malformed input
        # remains fail-closed in the second parse as well.
        manifest = parse_blueprint(blueprint_text, target_statement=statement_text)
    return [
        {
            "label": item.label,
            "item_id": item.item_id,
            "claim_sha256": item.digest,
        }
        for item in manifest.items
    ]


def _trusted_checkpoint_records(
    problem_id: str, *, cutoff_utc: str | None = None
) -> Dict[str, Dict[str, Any]]:
    """Return the active checkpoint projection now or at one exact UTC cutoff."""

    cutoff = (
        None
        if cutoff_utc is None
        else datetime.fromisoformat(
            _validate_canonical_utc_timestamp(
                cutoff_utc, label="checkpoint projection cutoff"
            )
        )
    )
    all_records: Dict[str, Dict[str, Any]] = {}
    superseded: set[str] = set()
    for checkpoint in _iter_memory_batch_checkpoints(problem_id):
        for raw in checkpoint["records"]:
            if (
                cutoff is not None
                and datetime.fromisoformat(
                    _validate_canonical_utc_timestamp(
                        raw["timestamp_utc"], label="checkpoint record timestamp"
                    )
                )
                > cutoff
            ):
                continue
            record_id = str(raw["record_id"])
            normalized = {
                "record_id": record_id,
                "timestamp_utc": raw["timestamp_utc"],
                "channel": raw["channel"],
                "active": raw["active"],
                "supersedes": list(raw["supersedes"]),
                "batch_id": raw["batch_id"],
                "record": deepcopy(raw["record"]),
            }
            existing = all_records.get(record_id)
            if existing is not None and existing != normalized:
                raise ValueError("durable memory record id has conflicting bodies")
            all_records[record_id] = normalized
            superseded.update(normalized["supersedes"])
    return {
        record_id: record
        for record_id, record in all_records.items()
        if record["active"] and record_id not in superseded
    }


def _trusted_mathematical_evidence_records(
    problem_id: str, *, cutoff_utc: str | None = None
) -> Dict[str, Dict[str, Any]]:
    """Exclude searchable control/advisory records from mathematical evidence."""

    return {
        record_id: record
        for record_id, record in _trusted_checkpoint_records(
            problem_id, cutoff_utc=cutoff_utc
        ).items()
        if record["channel"] in _REVIEW_FRONTIER_CHANNELS
        and not _is_active_route_commitment_record(record)
    }


def _is_active_route_commitment_record(record: Mapping[str, Any]) -> bool:
    body = record.get("record")
    state = body.get("state") if isinstance(body, dict) else None
    return bool(
        record.get("channel") == "branch_states"
        and isinstance(state, dict)
        and state.get("schema_version")
        in {_ACTIVE_ROUTE_COMMITMENT_SCHEMA, _ROUTE_TRANSITION_STATE_SCHEMA}
    )


def _bounded_route_obligations(value: Any, *, label: str) -> List[str]:
    if not isinstance(value, list) or not value or len(value) > 16:
        raise ValueError(f"{label} must be a non-empty bounded array")
    obligations: List[str] = []
    for index, item in enumerate(value):
        if (
            not isinstance(item, str)
            or not item.strip()
            or "\x00" in item
            or len(item.encode("utf-8")) > 2_048
        ):
            raise ValueError(f"{label}[{index}] is invalid")
        obligations.append(item)
    if len(set(obligations)) != len(obligations):
        raise ValueError(f"{label} contains duplicate obligations")
    return obligations


def _trusted_route_commitment_manifest(
    problem_id: str, *, due_at_utc: str, current_review_id: str | None = None
) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Resolve one active route and at most one pre-due evidenced fallback."""

    due_at = datetime.fromisoformat(
        _validate_canonical_utc_timestamp(
            due_at_utc, label="active route commitment due_at_utc"
        )
    )
    current_records = _trusted_checkpoint_records(problem_id)
    for record in current_records.values():
        if record["channel"] != "branch_states":
            continue
        timestamp = datetime.fromisoformat(
            _validate_canonical_utc_timestamp(
                record["timestamp_utc"], label="active route commitment timestamp"
            )
        )
        if timestamp <= due_at:
            continue
        body = record["record"]
        state = body.get("state") if isinstance(body, dict) else None
        schema = state.get("schema_version") if isinstance(state, dict) else None
        if schema == _ACTIVE_ROUTE_COMMITMENT_SCHEMA or (
            schema == _ROUTE_TRANSITION_STATE_SCHEMA
            and (
                current_review_id is None or state.get("review_id") != current_review_id
            )
        ):
            raise ValueError("route commitment changed after the exact review due time")
    all_records = _trusted_checkpoint_records(problem_id, cutoff_utc=due_at_utc)
    mathematical_records = _trusted_mathematical_evidence_records(
        problem_id, cutoff_utc=due_at_utc
    )
    latest: Dict[str, tuple[datetime, str, str, Dict[str, Any]]] = {}
    for record in all_records.values():
        if record["channel"] != "branch_states":
            continue
        timestamp = datetime.fromisoformat(
            _validate_canonical_utc_timestamp(
                record["timestamp_utc"], label="active route commitment timestamp"
            )
        )
        body = record["record"]
        state = body.get("state") if isinstance(body, dict) else None
        schema = state.get("schema_version") if isinstance(state, dict) else None
        if timestamp > due_at:  # pragma: no cover - cutoff projection invariant
            raise ValueError("route commitment escaped its cutoff projection")
        if schema not in {
            _ACTIVE_ROUTE_COMMITMENT_SCHEMA,
            _ROUTE_TRANSITION_STATE_SCHEMA,
        }:
            continue
        status = state.get("status")
        if schema == _ACTIVE_ROUTE_COMMITMENT_SCHEMA:
            state_keys = {
                "schema_version",
                "route_id",
                "status",
                "core_bridge",
                "obligations",
            } | ({"evidence_record_ids"} if status == "fallback" else set())
        else:
            state_keys = {
                "schema_version",
                "review_id",
                "request_sha256",
                "snapshot_sha256",
                "route_id",
                "status",
                "core_bridge",
                "obligations",
                "source_commitment_sha256",
            }
        if set(body) != {"branch_id", "state"} or set(state) != state_keys:
            raise ValueError("active route commitment has an unsupported shape")
        route_id = state.get("route_id")
        core_bridge = state.get("core_bridge")
        if (
            not isinstance(route_id, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}", route_id) is None
            or body.get("branch_id") != route_id
            or status
            not in (
                {"active", "inactive", "fallback"}
                if schema == _ACTIVE_ROUTE_COMMITMENT_SCHEMA
                else {"active", "inactive", "frozen"}
            )
            or not isinstance(core_bridge, str)
            or not core_bridge.strip()
            or len(core_bridge.encode("utf-8")) > 8_192
        ):
            raise ValueError("active route commitment binding is invalid")
        if schema == _ROUTE_TRANSITION_STATE_SCHEMA and (
            not isinstance(state.get("review_id"), str)
            or REVIEW_ID_RE.fullmatch(state["review_id"]) is None
            or not isinstance(state.get("request_sha256"), str)
            or SHA256_RE.fullmatch(state["request_sha256"]) is None
            or not isinstance(state.get("snapshot_sha256"), str)
            or SHA256_RE.fullmatch(state["snapshot_sha256"]) is None
            or not isinstance(state.get("source_commitment_sha256"), str)
            or SHA256_RE.fullmatch(state["source_commitment_sha256"]) is None
        ):
            raise ValueError("route transition state binding is invalid")
        obligations = _bounded_route_obligations(
            state.get("obligations"), label="route commitment obligations"
        )
        evidence_ids: List[str] = []
        if status == "fallback":
            evidence_ids = _bounded_unique_record_ids(
                state.get("evidence_record_ids"),
                label="fallback route evidence_record_ids",
                maximum=16,
            )
            if not evidence_ids:
                raise ValueError("fallback route commitment requires evidence")
            invalid_evidence = [
                record_id
                for record_id in evidence_ids
                if record_id not in mathematical_records
                or datetime.fromisoformat(
                    _validate_canonical_utc_timestamp(
                        mathematical_records[record_id]["timestamp_utc"],
                        label="fallback evidence timestamp",
                    )
                )
                > due_at
            ]
            if invalid_evidence:
                raise ValueError(
                    "fallback route evidence is inactive, non-mathematical, or post-due"
                )
        ordering = (timestamp, str(record["batch_id"]), str(record["record_id"]))
        existing = latest.get(route_id)
        commitment = {
            "route_id": route_id,
            "status": str(state["status"]),
            "core_bridge": core_bridge,
            "obligations": obligations,
            "record_id": str(record["record_id"]),
            "batch_id": str(record["batch_id"]),
            "timestamp_utc": str(record["timestamp_utc"]),
            "evidence_record_ids": evidence_ids,
        }
        if existing is None or ordering[:3] > existing[:3]:
            latest[route_id] = (*ordering, commitment)
    active = [entry[3] for entry in latest.values() if entry[3]["status"] == "active"]
    if len(active) != 1:
        raise ValueError(
            "first review requires exactly one pre-boundary active route commitment"
        )
    fallbacks = [
        entry[3] for entry in latest.values() if entry[3]["status"] == "fallback"
    ]
    if len(fallbacks) > 1:
        raise ValueError("review permits at most one pre-boundary fallback route")
    if fallbacks and fallbacks[0]["route_id"] == active[0]["route_id"]:
        raise ValueError("fallback route must differ from the active route")
    active_seed = {
        "route_id": active[0]["route_id"],
        "core_bridge": active[0]["core_bridge"],
        "obligations": list(active[0]["obligations"]),
        "commitment_record_id": active[0]["record_id"],
        "commitment_batch_id": active[0]["batch_id"],
        "commitment_timestamp_utc": active[0]["timestamp_utc"],
    }
    normalized_active = {
        **active_seed,
        "commitment_sha256": hashlib.sha256(
            canonical_json_bytes(active_seed)
        ).hexdigest(),
    }
    candidates: List[Dict[str, Any]] = []
    for fallback in fallbacks:
        seed = {
            "route_id": fallback["route_id"],
            "core_bridge": fallback["core_bridge"],
            "obligations": list(fallback["obligations"]),
            "commitment_record_id": fallback["record_id"],
            "commitment_batch_id": fallback["batch_id"],
            "commitment_timestamp_utc": fallback["timestamp_utc"],
            "evidence_record_ids": list(fallback["evidence_record_ids"]),
        }
        candidates.append(
            {
                **seed,
                "commitment_sha256": hashlib.sha256(
                    canonical_json_bytes(seed)
                ).hexdigest(),
            }
        )
    return normalized_active, candidates


def _trusted_active_route_commitment(
    problem_id: str, *, due_at_utc: str
) -> Dict[str, Any]:
    active, _fallbacks = _trusted_route_commitment_manifest(
        problem_id, due_at_utc=due_at_utc
    )
    return active


def _bounded_unique_record_ids(value: Any, *, label: str, maximum: int) -> List[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"{label} must be a bounded record-id array")
    result: List[str] = []
    for index, item in enumerate(value):
        if (
            not isinstance(item, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", item) is None
        ):
            raise ValueError(f"{label}[{index}] is not a record id")
        result.append(item)
    if len(set(result)) != len(result):
        raise ValueError(f"{label} contains duplicate record ids")
    return result


def _snapshot_record(record: Mapping[str, Any]) -> Dict[str, Any]:
    body = deepcopy(record["record"])
    marker = body.get(_REVIEW_PROGRESS_KIND_FIELD)
    if marker is not None and marker not in PROGRESS_KINDS:
        raise ValueError("durable review_progress_kind is invalid")
    kind = marker if marker is not None else record["channel"]
    return {
        "record_id": record["record_id"],
        "kind": kind,
        "body": body,
        "channel": record["channel"],
        "batch_id": record["batch_id"],
        "timestamp_utc": record["timestamp_utc"],
    }


def _official_review_record(
    active_records: Mapping[str, Mapping[str, Any]], *, cycle_id: str
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    matches: List[tuple[Dict[str, Any], Dict[str, Any]]] = []
    for record in active_records.values():
        body = record["record"]
        if (
            record["channel"] == "route_reviews"
            and body.get("schema_version") == _REVIEW_MEMORY_SCHEMA
            and body.get("state") == "official_published"
            and body.get("cycle_id") == cycle_id
            and body.get("review_ordinal") == 1
        ):
            matches.append((dict(record), deepcopy(body)))
    if len(matches) != 1:
        raise ValueError(
            "minute60 requires exactly one durable official minute30 review"
        )
    return matches[0]


def _prior_official_review_record(
    active_records: Mapping[str, Mapping[str, Any]],
    *,
    cycle_id: str,
    cycle: str,
    route_id: str,
) -> tuple[Dict[str, Any], Dict[str, Any]] | None:
    """Resolve the immutable same-route predecessor across or within cycles."""

    if cycle == "minute60":
        return _official_review_record(active_records, cycle_id=cycle_id)
    if cycle != "minute30":
        raise ValueError("review cycle is invalid")
    candidates: List[tuple[datetime, str, Dict[str, Any], Dict[str, Any]]] = []
    for record in active_records.values():
        body = record["record"]
        decision = body.get("decision")
        if (
            record["channel"] != "route_reviews"
            or body.get("schema_version") != _REVIEW_MEMORY_SCHEMA
            or body.get("state") != "official_published"
            or body.get("cycle_id") == cycle_id
            or body.get("cycle") != "minute60"
            or body.get("review_ordinal") != 2
            or not isinstance(decision, dict)
            or decision.get("route_id") != route_id
        ):
            continue
        timestamp = datetime.fromisoformat(
            _validate_canonical_utc_timestamp(
                body.get("official_published_timestamp_utc"),
                label="cross-cycle prior official cutoff",
            )
        )
        candidates.append(
            (timestamp, str(record["record_id"]), dict(record), deepcopy(body))
        )
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]))
    if len(candidates) > 1 and candidates[-2][0] == candidates[-1][0]:
        raise ValueError("cross-cycle prior official review cutoff is ambiguous")
    return candidates[-1][2], candidates[-1][3]


def _prior_official_review_payload(
    record: Mapping[str, Any], body: Mapping[str, Any]
) -> Dict[str, Any]:
    prior = {
        "record_id": record["record_id"],
        "review_id": body["review_id"],
        "cycle_id": body["cycle_id"],
        "cycle": body["cycle"],
        "review_ordinal": body["review_ordinal"],
        "snapshot_sha256": body["snapshot_sha256"],
        "timestamp_utc": body["official_published_timestamp_utc"],
        "report": deepcopy(body["report"]),
        "decision": deepcopy(body["decision"]),
    }
    prior["content_sha256"] = hashlib.sha256(canonical_json_bytes(prior)).hexdigest()
    return prior


def _build_trusted_review_request(
    *,
    review_id: str,
    cycle_id: str,
    cycle: str,
    review_ordinal: int,
    due_at_utc: str,
    root_thread_id: str,
    root_turn_id: str,
    root_terminal_sha256: str,
    route_id: str,
    frontier_record_ids: List[str],
    progress_record_ids: List[str],
) -> Dict[str, Any]:
    problem_id = validate_verified_problem_id(
        _required_review_env("RETHLAS_EXPECTED_PROBLEM_ID", label="review problem id")
    )
    run_id = _required_review_env(_REVIEW_RUN_ENV, label="review run id")
    statement_text, statement_sha256 = _trusted_problem_statement(problem_id)
    if len(statement_text.encode("utf-8")) > 65_536:
        raise ValueError(
            "full authoritative problem statement exceeds 64 KiB review bound"
        )
    blueprint_text, blueprint_sha256 = _trusted_blueprint(problem_id)
    blueprint_items = _trusted_blueprint_items(blueprint_text, statement_text)
    active_records = _trusted_checkpoint_records(problem_id, cutoff_utc=due_at_utc)
    committed_route, fallback_route_candidates = _trusted_route_commitment_manifest(
        problem_id,
        due_at_utc=due_at_utc,
        current_review_id=review_id,
    )
    if committed_route["route_id"] != route_id:
        raise ValueError("review route differs from its pre-boundary commitment")
    frontier_ids = _bounded_unique_record_ids(
        frontier_record_ids, label="frontier_record_ids", maximum=64
    )
    progress_ids = _bounded_unique_record_ids(
        progress_record_ids, label="progress_record_ids", maximum=32
    )
    if not set(progress_ids) <= set(frontier_ids):
        raise ValueError("every progress id must also be a frontier id")
    missing = sorted(set(frontier_ids) - set(active_records))
    if missing:
        raise ValueError(f"review record ids are not active durable records: {missing}")
    non_evidence = sorted(
        record_id
        for record_id in frontier_ids
        if active_records[record_id]["channel"] not in _REVIEW_FRONTIER_CHANNELS
    )
    if non_evidence:
        raise ValueError(
            "review control/advisory records cannot be mathematical frontier evidence: "
            f"{non_evidence}"
        )
    frontier = [
        _snapshot_record(active_records[record_id]) for record_id in frontier_ids
    ]
    frontier_by_id = {record["record_id"]: record for record in frontier}
    progress = [deepcopy(frontier_by_id[record_id]) for record_id in progress_ids]
    for record in progress:
        if record["kind"] not in PROGRESS_KINDS:
            raise ValueError("progress id lacks a durable qualifying progress kind")

    if cycle == "minute30" and review_ordinal != 1:
        raise ValueError("minute30 review ordinal must be 1")
    if cycle == "minute60" and review_ordinal != 2:
        raise ValueError("minute60 review ordinal must be 2")
    if cycle not in {"minute30", "minute60"}:
        raise ValueError("cycle must be minute30 or minute60")
    prior_pair = _prior_official_review_record(
        active_records,
        cycle_id=cycle_id,
        cycle=cycle,
        route_id=route_id,
    )
    prior_official_review = (
        None if prior_pair is None else _prior_official_review_payload(*prior_pair)
    )

    snapshot = {
        "schema_version": "rethlas_route_review_snapshot_v2",
        "run_id": run_id,
        "problem_id": problem_id,
        "cycle_id": cycle_id,
        "cycle": cycle,
        "review_ordinal": review_ordinal,
        "due_at_utc": due_at_utc,
        "root_thread_id": root_thread_id,
        "root_turn_id": root_turn_id,
        "root_terminal_sha256": root_terminal_sha256,
        "route_id": route_id,
        "active_route": committed_route,
        "statement_sha256": statement_sha256,
        "statement_text": statement_text,
        "blueprint_sha256": blueprint_sha256,
        "blueprint_text": blueprint_text,
        "blueprint_items": blueprint_items,
        "fallback_route_candidates": fallback_route_candidates,
        "frontier_records": frontier,
        "progress_records": progress,
        "prior_official_review": prior_official_review,
    }
    return build_review_request(
        review_id=review_id,
        snapshot=snapshot,
        expected_model=_required_review_env(_REVIEW_MODEL_ENV, label="review model"),
        reasoning_effort=_required_review_env(
            _REVIEW_EFFORT_ENV, label="review effort"
        ),
        policy_sha256=_required_review_env(
            _REVIEW_POLICY_SHA_ENV, label="review policy SHA-256"
        ),
    )


def _trusted_review_frontier_ids(
    *,
    cycle_id: str,
    cycle: str,
    due_at_utc: str,
    route_id: str,
    current_review_id: str | None = None,
) -> tuple[List[str], List[str]]:
    """Select the one bounded, deterministic pre-boundary reasoning frontier."""

    due_text = _validate_canonical_utc_timestamp(
        due_at_utc, label="review frontier due_at_utc"
    )
    due_at = datetime.fromisoformat(due_text)
    problem_id = validate_verified_problem_id(
        _required_review_env("RETHLAS_EXPECTED_PROBLEM_ID", label="review problem id")
    )
    active = _trusted_checkpoint_records(problem_id, cutoff_utc=due_text)
    _active_route, fallback_candidates = _trusted_route_commitment_manifest(
        problem_id,
        due_at_utc=due_text,
        current_review_id=current_review_id,
    )
    fallback_evidence_ids = {
        record_id
        for candidate in fallback_candidates
        for record_id in candidate["evidence_record_ids"]
    }
    prior_pair = _prior_official_review_record(
        active, cycle_id=cycle_id, cycle=cycle, route_id=route_id
    )
    if prior_pair is None:
        if cycle != "minute30":
            raise ValueError("minute60 requires its official minute30 cutoff")
        progress_cutoff = due_at - timedelta(seconds=1800)
        progress_after = False
    else:
        _prior_record, prior_body = prior_pair
        progress_cutoff = datetime.fromisoformat(
            _validate_canonical_utc_timestamp(
                prior_body["official_published_timestamp_utc"],
                label="review frontier prior official cutoff",
            )
        )
        progress_after = True
    eligible: List[tuple[datetime, str, str, Dict[str, Any]]] = []
    for record in active.values():
        if record["channel"] not in _REVIEW_FRONTIER_CHANNELS:
            continue
        if _is_active_route_commitment_record(record):
            continue
        timestamp = datetime.fromisoformat(
            _validate_canonical_utc_timestamp(
                record["timestamp_utc"], label="review frontier record timestamp"
            )
        )
        in_current_window = (
            timestamp > progress_cutoff
            if progress_after
            else timestamp >= progress_cutoff
        )
        if timestamp <= due_at and (
            in_current_window or record["record_id"] in fallback_evidence_ids
        ):
            eligible.append(
                (
                    timestamp,
                    str(record["batch_id"]),
                    str(record["record_id"]),
                    _snapshot_record(record),
                )
            )
    eligible.sort(key=lambda item: item[:3])
    if len(eligible) > 64:
        raise ValueError("trusted review frontier exceeds its 64-record bound")
    frontier_ids = [item[2] for item in eligible]
    progress_ids: List[str] = []
    for timestamp, _batch_id, record_id, snapshot_record in eligible:
        if (
            (
                timestamp > progress_cutoff
                if progress_after
                else timestamp >= progress_cutoff
            )
            and record_id not in fallback_evidence_ids
            and snapshot_record["kind"] in PROGRESS_KINDS
        ):
            progress_ids.append(record_id)
    if len(progress_ids) > 32:
        raise ValueError("trusted review progress exceeds its 32-record bound")
    return frontier_ids, progress_ids


def review_frontier_status(
    *,
    cycle_id: str,
    cycle: str,
    review_ordinal: int,
) -> Dict[str, Any]:
    """Expose only the exact trusted record ids a restricted helper may review."""

    boundary = _adapter_review_due_status(
        cycle_id=cycle_id,
        cycle=cycle,
        review_ordinal=review_ordinal,
    )
    review_id = boundary["review_id"]
    active_route_id = boundary["active_route_id"]
    if active_route_id == "route:unspecified" and review_ordinal != 1:
        raise ValueError("only the first review may bind an unspecified active route")
    problem_id = validate_verified_problem_id(
        _required_review_env("RETHLAS_EXPECTED_PROBLEM_ID", label="review problem id")
    )
    committed_route, fallback_route_candidates = _trusted_route_commitment_manifest(
        problem_id,
        due_at_utc=boundary["due_at_utc"],
        current_review_id=review_id,
    )
    route_id = committed_route["route_id"]
    if active_route_id != "route:unspecified" and active_route_id != route_id:
        raise ValueError("durable active route differs from its commitment")
    frontier_ids, progress_ids = _trusted_review_frontier_ids(
        cycle_id=cycle_id,
        cycle=cycle,
        due_at_utc=boundary["due_at_utc"],
        route_id=route_id,
        current_review_id=review_id,
    )
    manifest_seed = {
        "schema_version": "rethlas_review_frontier_manifest_v1",
        "review_id": review_id,
        "cycle_id": cycle_id,
        "cycle": cycle,
        "review_ordinal": review_ordinal,
        "due_at_utc": boundary["due_at_utc"],
        "root_thread_id": boundary["root_thread_id"],
        "root_turn_id": boundary["root_turn_id"],
        "root_terminal_sha256": boundary["root_terminal_sha256"],
        "route_id": route_id,
        "active_route": committed_route,
        "fallback_route_candidates": fallback_route_candidates,
        "frontier_record_ids": frontier_ids,
        "progress_record_ids": progress_ids,
    }
    return {
        **manifest_seed,
        "schema_version": "rethlas_review_frontier_status_v1",
        "active_route_id": active_route_id,
        "manifest_sha256": hashlib.sha256(
            canonical_json_bytes(manifest_seed)
        ).hexdigest(),
    }


def _prepared_review_body(request: Mapping[str, Any]) -> Dict[str, Any]:
    snapshot = request["snapshot"]
    return {
        "schema_version": _REVIEW_MEMORY_SCHEMA,
        "record_role": "control_audit_only",
        "mathematical_evidence_authority": False,
        "state": "prepared",
        "run_id": snapshot["run_id"],
        "problem_id": snapshot["problem_id"],
        "cycle_id": snapshot["cycle_id"],
        "cycle": snapshot["cycle"],
        "review_ordinal": snapshot["review_ordinal"],
        "due_at_utc": snapshot["due_at_utc"],
        "review_id": request["review_id"],
        "route_id": snapshot["route_id"],
        "active_route": deepcopy(snapshot["active_route"]),
        "root_thread_id": snapshot["root_thread_id"],
        "root_turn_id": snapshot["root_turn_id"],
        "root_terminal_sha256": snapshot["root_terminal_sha256"],
        "statement_sha256": snapshot["statement_sha256"],
        "blueprint_sha256": snapshot["blueprint_sha256"],
        "snapshot_sha256": request["snapshot_sha256"],
        "request_sha256": request["request_sha256"],
        "expected_model": request["expected_model"],
        "reasoning_effort": request["reasoning_effort"],
        "policy_sha256": request["policy_sha256"],
        "frontier_record_ids": [
            record["record_id"] for record in snapshot["frontier_records"]
        ],
        "progress_record_ids": [
            record["record_id"] for record in snapshot["progress_records"]
        ],
        "fallback_route_candidates": deepcopy(snapshot["fallback_route_candidates"]),
        "prior_official_review": deepcopy(snapshot["prior_official_review"]),
    }


def _append_review_memory(
    *,
    problem_id: str,
    body: Mapping[str, Any],
    supersedes: List[str] | None = None,
) -> Dict[str, Any]:
    normalized_body = deepcopy(dict(body))
    receipt = memory_append_batch(
        problem_id,
        [
            {
                "channel": "route_reviews",
                "record": normalized_body,
                "supersedes": [] if supersedes is None else supersedes,
            }
        ],
        _trusted_control_publication=True,
    )
    checkpoint = _validate_memory_batch_checkpoint(
        problem_id, Path(receipt["checkpoint_path"])
    )
    record = checkpoint["records"][0]
    return {
        "schema_version": "rethlas_route_review_publication_receipt_v1",
        "problem_id": problem_id,
        "review_id": normalized_body["review_id"],
        "request_sha256": normalized_body["request_sha256"],
        "snapshot_sha256": normalized_body["snapshot_sha256"],
        "batch_id": receipt["batch_id"],
        "record_id": record["record_id"],
        "timestamp_utc": checkpoint["timestamp_utc"],
        "checkpoint_sha256": checkpoint["checkpoint_sha256"],
        "record_sha256": hashlib.sha256(
            canonical_json_bytes(normalized_body)
        ).hexdigest(),
        "publication_state": {
            "completed_pending_close": "pending",
            "official_published": "official",
        }.get(str(normalized_body.get("state")), "prepared"),
    }


def _ensure_immutable_official_cutoff(
    *, problem_id: str, publication_receipt: Mapping[str, Any]
) -> Dict[str, Any]:
    """Stamp the first official record's cutoff into every later supersession."""

    record, body = _find_review_memory(
        problem_id,
        review_id=str(publication_receipt["review_id"]),
        request_sha256=str(publication_receipt["request_sha256"]),
        snapshot_sha256=str(publication_receipt["snapshot_sha256"]),
    )
    fields = {
        "official_published_record_id",
        "official_published_timestamp_utc",
        "official_published_record_sha256",
    }
    present = fields & set(body)
    if present:
        if present != fields:
            raise ValueError("official review cutoff commitment is incomplete")
        return _publication_receipt_for_existing(problem_id, record, body)
    if body.get("state") != "official_published":
        raise ValueError("only an official review can acquire a cutoff commitment")
    stamped = deepcopy(body)
    stamped.update(
        {
            "official_published_record_id": record["record_id"],
            "official_published_timestamp_utc": record["timestamp_utc"],
            "official_published_record_sha256": hashlib.sha256(
                canonical_json_bytes(body)
            ).hexdigest(),
        }
    )
    return _append_review_memory(
        problem_id=problem_id,
        body=stamped,
        supersedes=[str(record["record_id"])],
    )


def _append_targeted_result_memory(
    *,
    problem_id: str,
    review_body: Mapping[str, Any],
    ticket: Mapping[str, Any],
    outcome_state: str,
    verification_receipt: Mapping[str, Any] | None,
    error_sha256: str | None,
) -> Dict[str, Any]:
    result_sha = (
        verification_receipt["receipt_sha256"]
        if verification_receipt is not None
        else error_sha256
    )
    if not isinstance(result_sha, str) or SHA256_RE.fullmatch(result_sha) is None:
        raise ValueError("targeted verifier result lacks one bound digest")
    body = {
        "schema_version": _TARGETED_MEMORY_SCHEMA,
        "record_role": "control_audit_only",
        "mathematical_evidence_authority": False,
        "state": "completed_pending_publication",
        "run_id": review_body["run_id"],
        "problem_id": problem_id,
        "review_id": review_body["review_id"],
        "request_sha256": review_body["request_sha256"],
        "snapshot_sha256": review_body["snapshot_sha256"],
        "ticket_id": ticket["ticket_id"],
        "ticket": deepcopy(dict(ticket)),
        "outcome": {
            "state": outcome_state,
            "verification_receipt": (
                None
                if verification_receipt is None
                else deepcopy(dict(verification_receipt))
            ),
            "error_sha256": error_sha256,
        },
        "result_sha256": result_sha,
    }
    receipt = memory_append_batch(
        problem_id,
        [{"channel": "targeted_verifications", "record": body, "supersedes": []}],
        _trusted_control_publication=True,
    )
    checkpoint = _validate_memory_batch_checkpoint(
        problem_id, Path(receipt["checkpoint_path"])
    )
    record = checkpoint["records"][0]
    return {
        "schema_version": "rethlas_targeted_verification_publication_receipt_v1",
        "problem_id": problem_id,
        "review_id": body["review_id"],
        "request_sha256": body["request_sha256"],
        "snapshot_sha256": body["snapshot_sha256"],
        "ticket_id": body["ticket_id"],
        "verifier_receipt_sha256": result_sha,
        "batch_id": receipt["batch_id"],
        "record_id": record["record_id"],
        "timestamp_utc": checkpoint["timestamp_utc"],
        "checkpoint_sha256": checkpoint["checkpoint_sha256"],
        "record_sha256": hashlib.sha256(canonical_json_bytes(body)).hexdigest(),
        "publication_state": "pending",
    }


def _find_targeted_result_memory(
    problem_id: str,
    *,
    review_id: str,
    request_sha256: str,
    snapshot_sha256: str,
) -> tuple[Dict[str, Any], Dict[str, Any]] | None:
    matches: List[tuple[Dict[str, Any], Dict[str, Any]]] = []
    for record in _trusted_checkpoint_records(problem_id).values():
        body = record["record"]
        if (
            record["channel"] == "targeted_verifications"
            and body.get("schema_version") == _TARGETED_MEMORY_SCHEMA
            and body.get("state") == "completed_pending_publication"
            and body.get("review_id") == review_id
            and body.get("request_sha256") == request_sha256
            and body.get("snapshot_sha256") == snapshot_sha256
        ):
            matches.append((dict(record), deepcopy(body)))
    if len(matches) > 1:
        raise ValueError("targeted verifier has conflicting pending result records")
    return None if not matches else matches[0]


def _targeted_publication_receipt_for_existing(
    problem_id: str, record: Mapping[str, Any], body: Mapping[str, Any]
) -> Dict[str, Any]:
    checkpoint = _validate_memory_batch_checkpoint(
        problem_id,
        _batch_checkpoint_dir(problem_id) / f"{record['batch_id']}.json",
    )
    return {
        "schema_version": "rethlas_targeted_verification_publication_receipt_v1",
        "problem_id": problem_id,
        "review_id": body["review_id"],
        "request_sha256": body["request_sha256"],
        "snapshot_sha256": body["snapshot_sha256"],
        "ticket_id": body["ticket_id"],
        "verifier_receipt_sha256": body["result_sha256"],
        "batch_id": record["batch_id"],
        "record_id": record["record_id"],
        "timestamp_utc": record["timestamp_utc"],
        "checkpoint_sha256": checkpoint["checkpoint_sha256"],
        "record_sha256": hashlib.sha256(canonical_json_bytes(body)).hexdigest(),
        "publication_state": "pending",
    }


def _review_memory_matches(
    body: Mapping[str, Any],
    *,
    review_id: str,
    request_sha256: str,
    snapshot_sha256: str,
) -> bool:
    return (
        body.get("schema_version") == _REVIEW_MEMORY_SCHEMA
        and body.get("review_id") == review_id
        and body.get("request_sha256") == request_sha256
        and body.get("snapshot_sha256") == snapshot_sha256
    )


def _find_review_memory(
    problem_id: str,
    *,
    review_id: str,
    request_sha256: str,
    snapshot_sha256: str,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    candidates: List[tuple[Dict[str, Any], Dict[str, Any]]] = []
    for record in _trusted_checkpoint_records(problem_id).values():
        body = record["record"]
        if record["channel"] == "route_reviews" and _review_memory_matches(
            body,
            review_id=review_id,
            request_sha256=request_sha256,
            snapshot_sha256=snapshot_sha256,
        ):
            candidates.append((dict(record), deepcopy(body)))
    if len(candidates) != 1:
        raise ValueError("review operation lacks one active durable memory binding")
    return candidates[0]


def _request_from_prepared_body(body: Mapping[str, Any]) -> Dict[str, Any]:
    """Rebuild only the immutable request needed to validate a returned report."""

    # The full snapshot is deliberately not echoed into route-review memory.
    # Re-resolve its exact record ids and current authoritative files. Any
    # mutation changes the digest and fails against the prepared binding.
    request = _build_trusted_review_request(
        review_id=body["review_id"],
        cycle_id=body["cycle_id"],
        cycle=body["cycle"],
        review_ordinal=body["review_ordinal"],
        due_at_utc=body["due_at_utc"],
        root_thread_id=body["root_thread_id"],
        root_turn_id=body["root_turn_id"],
        root_terminal_sha256=body["root_terminal_sha256"],
        route_id=body["route_id"],
        frontier_record_ids=list(body["frontier_record_ids"]),
        progress_record_ids=list(body["progress_record_ids"]),
    )
    if (
        request["request_sha256"] != body["request_sha256"]
        or request["snapshot_sha256"] != body["snapshot_sha256"]
    ):
        raise ValueError("prepared review inputs changed before durable completion")
    return request


def _pending_review_body(
    prepared_record: Mapping[str, Any],
    prepared_body: Mapping[str, Any],
    result: Mapping[str, Any],
) -> Dict[str, Any]:
    request = _request_from_prepared_body(prepared_body)
    execution = result.get("execution")
    if (
        not isinstance(execution, dict)
        or execution.get("state") != "completed"
        or not isinstance(execution.get("report"), dict)
        or not isinstance(result.get("decision"), dict)
    ):
        raise ValueError("pending review result lacks a strict report and decision")
    report = validate_review_report(
        execution["report"],
        review_id=request["review_id"],
        snapshot=request["snapshot"],
    )
    previous_decision = None
    prior = request["snapshot"]["prior_official_review"]
    if prior is not None:
        active = _trusted_checkpoint_records(request["snapshot"]["problem_id"])
        prior_record = active.get(prior["record_id"])
        if prior_record is None:
            raise ValueError("prior official review is no longer active")
        previous_decision = prior_record["record"].get("decision")
    expected_decision = apply_effective_verdict(
        report,
        review_id=request["review_id"],
        snapshot=request["snapshot"],
        previous_decision=(
            None
            if previous_decision is None
            else {
                key: previous_decision[key]
                for key in (
                    "route_id",
                    "effective_verdict",
                    "yellow_streak",
                    "route_frozen",
                )
            }
        ),
    )
    if result["decision"] != expected_decision:
        raise ValueError("host review decision disagrees with the pinned reducer")
    body = deepcopy(dict(prepared_body))
    body.update(
        {
            "state": "completed_pending_close",
            "prepared_record_id": prepared_record["record_id"],
            "report": report,
            "decision": expected_decision,
        }
    )
    return body


def _publication_receipt_for_existing(
    problem_id: str, record: Mapping[str, Any], body: Mapping[str, Any]
) -> Dict[str, Any]:
    checkpoint = _validate_memory_batch_checkpoint(
        problem_id,
        _batch_checkpoint_dir(problem_id) / f"{record['batch_id']}.json",
    )
    return {
        "schema_version": "rethlas_route_review_publication_receipt_v1",
        "problem_id": problem_id,
        "review_id": body["review_id"],
        "request_sha256": body["request_sha256"],
        "snapshot_sha256": body["snapshot_sha256"],
        "batch_id": record["batch_id"],
        "record_id": record["record_id"],
        "timestamp_utc": record["timestamp_utc"],
        "checkpoint_sha256": checkpoint["checkpoint_sha256"],
        "record_sha256": hashlib.sha256(canonical_json_bytes(body)).hexdigest(),
        "publication_state": {
            "completed_pending_close": "pending",
            "official_published": "official",
        }.get(str(body.get("state")), "prepared"),
    }


def _persist_pending_review(
    *,
    problem_id: str,
    record: Mapping[str, Any],
    body: Mapping[str, Any],
    result: Mapping[str, Any],
) -> Dict[str, Any]:
    if body.get("state") == "completed_pending_close":
        return _publication_receipt_for_existing(problem_id, record, body)
    if body.get("state") == "official_published":
        publication = body.get("publication_receipt")
        if not isinstance(publication, dict):
            raise ValueError("official review lost its publication receipt")
        return deepcopy(publication)
    if body.get("state") != "prepared":
        raise ValueError("review memory is not in a publishable state")
    pending_body = _pending_review_body(record, body, result)
    return _append_review_memory(
        problem_id=problem_id,
        body=pending_body,
        supersedes=[record["record_id"]],
    )


def route_review_prepare(
    *,
    review_id: str,
    cycle_id: str,
    cycle: str,
    review_ordinal: int,
    frontier_manifest_sha256: str,
    frontier_record_ids: List[str],
    progress_record_ids: List[str],
) -> Dict[str, Any]:
    """Build one official request solely from durable ids and trusted bindings."""

    boundary = _adapter_review_due_status(
        cycle_id=cycle_id,
        cycle=cycle,
        review_ordinal=review_ordinal,
    )
    if boundary["review_id"] != review_id:
        raise ValueError("review_id differs from the host-derived due identity")
    active_route_id = boundary["active_route_id"]
    if active_route_id == "route:unspecified" and review_ordinal != 1:
        raise ValueError("only the first review may bind an unspecified active route")
    problem_id = validate_verified_problem_id(
        _required_review_env("RETHLAS_EXPECTED_PROBLEM_ID", label="review problem id")
    )
    committed_route, fallback_route_candidates = _trusted_route_commitment_manifest(
        problem_id,
        due_at_utc=boundary["due_at_utc"],
        current_review_id=review_id,
    )
    route_id = committed_route["route_id"]
    if active_route_id != "route:unspecified" and active_route_id != route_id:
        raise ValueError("durable active route differs from its commitment")
    trusted_frontier_ids, trusted_progress_ids = _trusted_review_frontier_ids(
        cycle_id=cycle_id,
        cycle=cycle,
        due_at_utc=boundary["due_at_utc"],
        route_id=route_id,
        current_review_id=review_id,
    )
    manifest_seed = {
        "schema_version": "rethlas_review_frontier_manifest_v1",
        "review_id": review_id,
        "cycle_id": cycle_id,
        "cycle": cycle,
        "review_ordinal": review_ordinal,
        "due_at_utc": boundary["due_at_utc"],
        "root_thread_id": boundary["root_thread_id"],
        "root_turn_id": boundary["root_turn_id"],
        "root_terminal_sha256": boundary["root_terminal_sha256"],
        "route_id": route_id,
        "active_route": committed_route,
        "fallback_route_candidates": fallback_route_candidates,
        "frontier_record_ids": trusted_frontier_ids,
        "progress_record_ids": trusted_progress_ids,
    }
    trusted_manifest_sha256 = hashlib.sha256(
        canonical_json_bytes(manifest_seed)
    ).hexdigest()
    if (
        frontier_manifest_sha256 != trusted_manifest_sha256
        or SHA256_RE.fullmatch(frontier_manifest_sha256) is None
        or frontier_record_ids != trusted_frontier_ids
        or progress_record_ids != trusted_progress_ids
    ):
        raise ValueError(
            "review record ids must equal the trusted frontier manifest in order"
        )
    request = _build_trusted_review_request(
        review_id=review_id,
        cycle_id=cycle_id,
        cycle=cycle,
        review_ordinal=review_ordinal,
        due_at_utc=boundary["due_at_utc"],
        root_thread_id=boundary["root_thread_id"],
        root_turn_id=boundary["root_turn_id"],
        root_terminal_sha256=boundary["root_terminal_sha256"],
        route_id=route_id,
        frontier_record_ids=frontier_record_ids,
        progress_record_ids=progress_record_ids,
    )
    result = _adapter_route_review_prepare(request=request)
    prepared_body = _prepared_review_body(request)
    prepared_receipt = _append_review_memory(
        problem_id=request["snapshot"]["problem_id"], body=prepared_body
    )
    response = deepcopy(result)
    response["prepared_memory_record_id"] = prepared_receipt["record_id"]
    if result["state"] in {"completed", "completed_pending_close"}:
        record, body = _find_review_memory(
            request["snapshot"]["problem_id"],
            review_id=request["review_id"],
            request_sha256=request["request_sha256"],
            snapshot_sha256=request["snapshot_sha256"],
        )
        publication = _persist_pending_review(
            problem_id=request["snapshot"]["problem_id"],
            record=record,
            body=body,
            result=result,
        )
        response["publication_receipt"] = publication
    return response


def _bound_review_result_operation(
    *,
    operation: str,
    review_id: str,
    request_sha256: str,
    snapshot_sha256: str,
) -> Dict[str, Any]:
    problem_id = validate_verified_problem_id(
        _required_review_env("RETHLAS_EXPECTED_PROBLEM_ID", label="review problem id")
    )
    record, body = _find_review_memory(
        problem_id,
        review_id=review_id,
        request_sha256=request_sha256,
        snapshot_sha256=snapshot_sha256,
    )
    if operation == "wait":
        result = _adapter_route_review_wait(
            review_id=review_id,
            request_sha256=request_sha256,
            snapshot_sha256=snapshot_sha256,
        )
    elif operation == "status":
        result = _adapter_route_review_status(
            review_id=review_id,
            request_sha256=request_sha256,
            snapshot_sha256=snapshot_sha256,
        )
    else:  # pragma: no cover - internal call contract
        raise AssertionError("invalid review result operation")
    response = deepcopy(result)
    if result["state"] in {"completed", "completed_pending_close"}:
        publication = _persist_pending_review(
            problem_id=problem_id,
            record=record,
            body=body,
            result=result,
        )
        response["publication_receipt"] = publication
    elif result["state"] == "closed" and body.get("state") != "official_published":
        raise ValueError("host closed review before durable official memory marker")
    return response


def route_review_wait(
    *,
    review_id: str,
    request_sha256: str,
    snapshot_sha256: str,
) -> Dict[str, Any]:
    return _bound_review_result_operation(
        operation="wait",
        review_id=review_id,
        request_sha256=request_sha256,
        snapshot_sha256=snapshot_sha256,
    )


def route_review_status(
    *, review_id: str, request_sha256: str, snapshot_sha256: str
) -> Dict[str, Any]:
    return _bound_review_result_operation(
        operation="status",
        review_id=review_id,
        request_sha256=request_sha256,
        snapshot_sha256=snapshot_sha256,
    )


def _route_transition_projection_items(
    *,
    review_body: Mapping[str, Any],
    fallback: Mapping[str, Any] | None,
) -> List[Dict[str, Any]]:
    active = review_body.get("active_route")
    if not isinstance(active, dict):
        raise ValueError("official review lacks its active-route commitment")
    shared = {
        "schema_version": _ROUTE_TRANSITION_STATE_SCHEMA,
        "review_id": review_body["review_id"],
        "request_sha256": review_body["request_sha256"],
        "snapshot_sha256": review_body["snapshot_sha256"],
    }
    inactive_state = {
        **shared,
        "route_id": active["route_id"],
        "status": "frozen",
        "core_bridge": active["core_bridge"],
        "obligations": list(active["obligations"]),
        "source_commitment_sha256": active["commitment_sha256"],
    }
    items = [
        {
            "channel": "branch_states",
            "record": {"branch_id": active["route_id"], "state": inactive_state},
            "active": True,
            "supersedes": [active["commitment_record_id"]],
        },
    ]
    if fallback is not None:
        active_state = {
            **shared,
            "route_id": fallback["route_id"],
            "status": "active",
            "core_bridge": fallback["core_bridge"],
            "obligations": list(fallback["obligations"]),
            "source_commitment_sha256": fallback["commitment_sha256"],
        }
        items.append(
            {
                "channel": "branch_states",
                "record": {
                    "branch_id": fallback["route_id"],
                    "state": active_state,
                },
                "active": True,
                "supersedes": [fallback["commitment_record_id"]],
            }
        )
    return items


def _route_transition_projection_receipt(
    *,
    problem_id: str,
    review_body: Mapping[str, Any],
    fallback: Mapping[str, Any] | None,
    publish: bool,
) -> Dict[str, Any]:
    items = _route_transition_projection_items(
        review_body=review_body, fallback=fallback
    )
    normalized_problem_id = sanitize_problem_id(problem_id)
    encoded = json.dumps(
        items,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    expected_batch_id = _batch_id_for_items(normalized_problem_id, encoded)
    if publish:
        publication = memory_append_batch(
            problem_id,
            items,
            _trusted_control_publication=True,
        )
        if publication["batch_id"] != expected_batch_id:
            raise ValueError("route transition projection batch id changed")
        checkpoint_path = Path(publication["checkpoint_path"])
    else:
        checkpoint_path = (
            _batch_checkpoint_dir(normalized_problem_id) / f"{expected_batch_id}.json"
        )
        if not checkpoint_path.exists():
            raise ValueError("official route transition projection is not durable")
    checkpoint = _validate_memory_batch_checkpoint(
        normalized_problem_id, checkpoint_path
    )
    if _checkpoint_normalized_items(checkpoint) != items:
        raise ValueError("route transition projection checkpoint body mismatch")
    records = checkpoint["records"]
    active = review_body["active_route"]
    seed = {
        "schema_version": _ROUTE_TRANSITION_RECEIPT_SCHEMA,
        "problem_id": normalized_problem_id,
        "review_id": review_body["review_id"],
        "request_sha256": review_body["request_sha256"],
        "snapshot_sha256": review_body["snapshot_sha256"],
        "from_route_id": active["route_id"],
        "to_route_id": None if fallback is None else fallback["route_id"],
        "batch_id": expected_batch_id,
        "record_ids": [record["record_id"] for record in records],
        "timestamp_utc": checkpoint["timestamp_utc"],
        "checkpoint_sha256": checkpoint["checkpoint_sha256"],
        "transition_sha256": hashlib.sha256(canonical_json_bytes(items)).hexdigest(),
    }
    return {
        **seed,
        "receipt_sha256": hashlib.sha256(canonical_json_bytes(seed)).hexdigest(),
    }


def _append_route_transition_projection(
    *,
    problem_id: str,
    review_body: Mapping[str, Any],
    fallback: Mapping[str, Any] | None,
) -> Dict[str, Any]:
    return _route_transition_projection_receipt(
        problem_id=problem_id,
        review_body=review_body,
        fallback=fallback,
        publish=True,
    )


def route_review_close(
    *,
    review_id: str,
    request_sha256: str,
    snapshot_sha256: str,
) -> Dict[str, Any]:
    problem_id = validate_verified_problem_id(
        _required_review_env("RETHLAS_EXPECTED_PROBLEM_ID", label="review problem id")
    )
    record, body = _find_review_memory(
        problem_id,
        review_id=review_id,
        request_sha256=request_sha256,
        snapshot_sha256=snapshot_sha256,
    )
    decision = body.get("decision")
    if not isinstance(decision, dict):
        raise ValueError("review close lacks its durable effective decision")
    if decision.get("effective_verdict") == "red":
        transition, fallback_candidate = _committed_red_route_transition(body)
    else:
        transition = {
            "next_route_id": None,
            "fallback_evidence_record_ids": [],
        }
        fallback_candidate = None

    if body.get("state") == "completed_pending_close":
        pending_receipt = _publication_receipt_for_existing(problem_id, record, body)
        pending_ack = _adapter_route_review_close(
            review_id=review_id,
            request_sha256=request_sha256,
            snapshot_sha256=snapshot_sha256,
            publication_receipt=pending_receipt,
            route_transition_publication_receipt=None,
            **transition,
        )
        if pending_ack["state"] != "completed_pending_publication":
            raise ValueError("pending review publication was not durably acknowledged")
        official_body = deepcopy(body)
        official_body.update(
            {
                "state": "official_published",
                "pending_record_id": record["record_id"],
                "pending_publication_receipt": deepcopy(pending_receipt),
                "route_transition": deepcopy(transition),
            }
        )
        official_receipt = _append_review_memory(
            problem_id=problem_id,
            body=official_body,
            supersedes=[record["record_id"]],
        )
        official_receipt = _ensure_immutable_official_cutoff(
            problem_id=problem_id, publication_receipt=official_receipt
        )
    elif body.get("state") == "official_published":
        if body.get("route_transition") != transition:
            raise ValueError("official route transition cannot change on retry")
        official_receipt = _publication_receipt_for_existing(problem_id, record, body)
    else:
        raise ValueError("review result must be durably published before close")

    route_transition_publication_receipt = (
        _append_route_transition_projection(
            problem_id=problem_id,
            review_body=body,
            fallback=fallback_candidate,
        )
        if decision.get("effective_verdict") == "red"
        else None
    )

    result = _adapter_route_review_close(
        review_id=review_id,
        request_sha256=request_sha256,
        snapshot_sha256=snapshot_sha256,
        publication_receipt=official_receipt,
        route_transition_publication_receipt=(route_transition_publication_receipt),
        **transition,
    )
    expected_state = (
        "verification_required"
        if (
            body.get("report", {}).get("load_bearing_claim") is not None
            and decision.get("effective_verdict") in {"green", "yellow"}
        )
        else "closed"
    )
    if result["state"] != expected_state:
        raise ValueError("official review publication reached an invalid host state")
    response = deepcopy(result)
    response["official_memory_record_id"] = official_receipt["record_id"]
    return response


def _committed_red_route_transition(
    body: Mapping[str, Any],
) -> tuple[Dict[str, Any], Mapping[str, Any] | None]:
    """Resolve the sole pre-due fallback; never accept a post-review route choice."""

    candidates = body.get("fallback_route_candidates")
    if not isinstance(candidates, list) or len(candidates) > 1:
        raise ValueError("official review fallback commitment is malformed")
    fallback_candidate: Mapping[str, Any] | None = None
    next_route_id: str | None = None
    evidence_ids: List[str] = []
    if candidates:
        candidate = candidates[0]
        expected_keys = {
            "route_id",
            "core_bridge",
            "obligations",
            "commitment_record_id",
            "commitment_batch_id",
            "commitment_timestamp_utc",
            "evidence_record_ids",
            "commitment_sha256",
        }
        if not isinstance(candidate, dict) or set(candidate) != expected_keys:
            raise ValueError("official review fallback commitment is malformed")
        seed = dict(candidate)
        commitment_sha = seed.pop("commitment_sha256")
        if (
            not isinstance(commitment_sha, str)
            or SHA256_RE.fullmatch(commitment_sha) is None
            or hashlib.sha256(canonical_json_bytes(seed)).hexdigest() != commitment_sha
        ):
            raise ValueError("official review fallback commitment digest mismatch")
        next_route_id = candidate["route_id"]
        if next_route_id == body.get("route_id"):
            raise ValueError("fallback route cannot equal the frozen route")
        evidence_ids = _bounded_unique_record_ids(
            candidate["evidence_record_ids"],
            label="fallback_evidence_record_ids",
            maximum=16,
        )
        if not evidence_ids:
            raise ValueError("fallback route commitment requires evidence")
        fallback_candidate = candidate
    return (
        {
            "next_route_id": next_route_id,
            "fallback_evidence_record_ids": evidence_ids,
        },
        fallback_candidate,
    )


def _targeted_review_admissible(body: Mapping[str, Any]) -> bool:
    """Admit a live green/yellow review or an exact terminal wrong-result replay."""

    decision = body.get("decision")
    if isinstance(decision, dict) and decision.get("effective_verdict") in {
        "green",
        "yellow",
    }:
        return True
    targeted = body.get("targeted_verification")
    receipt = (
        targeted.get("verification_receipt") if isinstance(targeted, dict) else None
    )
    return bool(
        isinstance(decision, dict)
        and decision.get("effective_verdict") == "red"
        and isinstance(targeted, dict)
        and targeted.get("state") == "completed"
        and isinstance(targeted.get("ticket"), dict)
        and isinstance(receipt, dict)
        and receipt.get("verdict") == "wrong"
    )


def _targeted_ticket_from_official_review(
    body: Mapping[str, Any],
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    if body.get("state") != "official_published":
        raise ValueError("targeted verification requires an official published review")
    if not _targeted_review_admissible(body):
        raise ValueError(
            "targeted verification requires an effective green or yellow review"
        )
    request = _request_from_prepared_body(body)
    report = validate_review_report(
        body.get("report"),
        review_id=request["review_id"],
        snapshot=request["snapshot"],
    )
    ticket = build_targeted_verification_ticket(
        report,
        review_id=request["review_id"],
        snapshot=request["snapshot"],
    )
    if ticket is None:
        raise ValueError("official review did not request targeted verification")
    return request, validate_targeted_verification_ticket(ticket)


def _replace_official_review_body(
    *, problem_id: str, record: Mapping[str, Any], body: Mapping[str, Any]
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    _append_review_memory(
        problem_id=problem_id,
        body=body,
        supersedes=[str(record["record_id"])],
    )
    return _find_review_memory(
        problem_id,
        review_id=str(body["review_id"]),
        request_sha256=str(body["request_sha256"]),
        snapshot_sha256=str(body["snapshot_sha256"]),
    )


def _targeted_wrong_decision(decision: Mapping[str, Any]) -> Dict[str, Any]:
    effective = deepcopy(dict(decision))
    effective.update(
        {
            "effective_verdict": "red",
            "auto_red": True,
            "auto_red_reason": (
                "targeted verification found the official load-bearing claim wrong"
            ),
            "route_frozen": True,
            "allowed_action": "freeze_route",
        }
    )
    return effective


def _targeted_error_sha256(error: BaseException | str) -> str:
    stable = (
        error
        if isinstance(error, str)
        else (f"{type(error).__module__}.{type(error).__qualname__}")
    )
    return hashlib.sha256(stable.encode("utf-8", errors="replace")).hexdigest()


def _finalize_targeted_outcome(
    *,
    problem_id: str,
    record: Mapping[str, Any],
    body: Mapping[str, Any],
    ticket: Mapping[str, Any],
    outcome_state: str,
    verification_receipt: Mapping[str, Any] | None,
    error_sha256: str | None,
    pending_record: Mapping[str, Any] | None = None,
    pending_body: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    if pending_record is None or pending_body is None:
        pending_receipt = _append_targeted_result_memory(
            problem_id=problem_id,
            review_body=body,
            ticket=ticket,
            outcome_state=outcome_state,
            verification_receipt=verification_receipt,
            error_sha256=error_sha256,
        )
        found = _find_targeted_result_memory(
            problem_id,
            review_id=str(body["review_id"]),
            request_sha256=str(body["request_sha256"]),
            snapshot_sha256=str(body["snapshot_sha256"]),
        )
        if found is None:
            raise ValueError("targeted result publication was not durably readable")
        pending_record, pending_body = found
    else:
        pending_receipt = _targeted_publication_receipt_for_existing(
            problem_id, pending_record, pending_body
        )
    outcome = {
        "state": outcome_state,
        "verification_receipt": (
            None
            if verification_receipt is None
            else deepcopy(dict(verification_receipt))
        ),
        "error_sha256": error_sha256,
    }
    if pending_body.get("ticket") != ticket or pending_body.get("outcome") != outcome:
        raise ValueError("pending targeted result disagrees with its official ticket")
    pending_ack = _adapter_targeted_verification_commit(
        review_id=str(body["review_id"]),
        request_sha256=str(body["request_sha256"]),
        snapshot_sha256=str(body["snapshot_sha256"]),
        outcome_state=outcome_state,
        verification_receipt=verification_receipt,
        error_sha256=error_sha256,
        publication_receipt=pending_receipt,
        route_transition_publication_receipt=None,
    )
    if pending_ack["state"] != "verification_pending_publication":
        raise ValueError(
            "host rejected pending targeted result before official publication"
        )

    official_body = deepcopy(dict(body))
    official_body["targeted_verification"].update(
        {
            "state": outcome_state,
            "verification_receipt": (
                None
                if verification_receipt is None
                else deepcopy(dict(verification_receipt))
            ),
            "error_sha256": error_sha256,
            "pending_result_record_id": pending_record["record_id"],
        }
    )
    transition_receipt: Mapping[str, Any] | None = None
    if verification_receipt is not None and verification_receipt["verdict"] == "wrong":
        official_body["decision"] = _targeted_wrong_decision(body["decision"])
        transition, fallback = _committed_red_route_transition(body)
        official_body["route_transition"] = transition
    official_receipt = _append_review_memory(
        problem_id=problem_id,
        body=official_body,
        supersedes=[str(record["record_id"]), str(pending_record["record_id"])],
    )
    if verification_receipt is not None and verification_receipt["verdict"] == "wrong":
        transition_receipt = _append_route_transition_projection(
            problem_id=problem_id,
            review_body=official_body,
            fallback=fallback,
        )
    final = _adapter_targeted_verification_commit(
        review_id=str(body["review_id"]),
        request_sha256=str(body["request_sha256"]),
        snapshot_sha256=str(body["snapshot_sha256"]),
        outcome_state=outcome_state,
        verification_receipt=verification_receipt,
        error_sha256=error_sha256,
        publication_receipt=official_receipt,
        route_transition_publication_receipt=transition_receipt,
    )
    expected_state = {
        "completed": "closed",
        "operational_blocked": "operational_blocked",
        "execution_unknown": "verification_unknown",
    }[outcome_state]
    if final["state"] != expected_state:
        raise ValueError(
            "host returned an invalid targeted verification terminal state"
        )
    if (
        outcome_state == "completed"
        and final.get("decision") != official_body["decision"]
    ):
        raise ValueError(
            "host targeted-verification decision disagrees with durable state"
        )
    response = deepcopy(final)
    if verification_receipt is not None:
        response["verification_receipt"] = deepcopy(dict(verification_receipt))
    return response


def verify_review_claim(
    *, review_id: str, request_sha256: str, snapshot_sha256: str
) -> Dict[str, Any]:
    """Run at most one verifier attempt for one official load-bearing claim."""

    problem_id = validate_verified_problem_id(
        _required_review_env("RETHLAS_EXPECTED_PROBLEM_ID", label="review problem id")
    )
    record, body = _find_review_memory(
        problem_id,
        review_id=review_id,
        request_sha256=request_sha256,
        snapshot_sha256=snapshot_sha256,
    )
    if not _targeted_review_admissible(body):
        raise ValueError(
            "targeted verification requires an effective green or yellow review"
        )
    request, ticket = _targeted_ticket_from_official_review(body)
    pending = _find_targeted_result_memory(
        problem_id,
        review_id=review_id,
        request_sha256=request_sha256,
        snapshot_sha256=snapshot_sha256,
    )
    if pending is not None:
        pending_record, pending_body = pending
        outcome = pending_body["outcome"]
        return _finalize_targeted_outcome(
            problem_id=problem_id,
            record=record,
            body=body,
            ticket=ticket,
            outcome_state=outcome["state"],
            verification_receipt=outcome["verification_receipt"],
            error_sha256=outcome["error_sha256"],
            pending_record=pending_record,
            pending_body=pending_body,
        )
    targeted = body.get("targeted_verification")
    if targeted is None:
        prepared_body = deepcopy(body)
        prepared_body["targeted_verification"] = {
            "state": "prepared",
            "ticket": ticket,
            "attempt": 1,
            "retry_allowed": False,
            "verification_receipt": None,
            "error_sha256": None,
        }
        record, body = _replace_official_review_body(
            problem_id=problem_id, record=record, body=prepared_body
        )
        targeted = body["targeted_verification"]
    elif not isinstance(targeted, dict) or targeted.get("ticket") != ticket:
        raise ValueError("durable targeted verification ticket binding changed")

    state = targeted.get("state")
    if state == "completed":
        receipt = targeted.get("verification_receipt")
        transition_receipt = None
        if isinstance(receipt, dict) and receipt.get("verdict") == "wrong":
            expected_transition, fallback = _committed_red_route_transition(body)
            if body.get("route_transition") != expected_transition:
                raise ValueError(
                    "completed targeted wrong result lost its route transition"
                )
            transition_receipt = _append_route_transition_projection(
                problem_id=problem_id,
                review_body=body,
                fallback=fallback,
            )
        result = _adapter_targeted_verification_commit(
            review_id=review_id,
            request_sha256=request_sha256,
            snapshot_sha256=snapshot_sha256,
            outcome_state="completed",
            verification_receipt=receipt,
            error_sha256=None,
            publication_receipt=_publication_receipt_for_existing(
                problem_id, record, body
            ),
            route_transition_publication_receipt=transition_receipt,
        )
        return {**deepcopy(result), "verification_receipt": deepcopy(receipt)}
    if state in {"operational_blocked", "execution_unknown"}:
        result = _adapter_targeted_verification_commit(
            review_id=review_id,
            request_sha256=request_sha256,
            snapshot_sha256=snapshot_sha256,
            outcome_state=state,
            verification_receipt=None,
            error_sha256=targeted.get("error_sha256"),
            publication_receipt=_publication_receipt_for_existing(
                problem_id, record, body
            ),
            route_transition_publication_receipt=None,
        )
        return deepcopy(result)
    if state == "dispatching":
        error_sha = _targeted_error_sha256("targeted-verifier-dispatch-outcome-lost")
        return _finalize_targeted_outcome(
            problem_id=problem_id,
            record=record,
            body=body,
            ticket=ticket,
            outcome_state="execution_unknown",
            verification_receipt=None,
            error_sha256=error_sha,
        )
    if state != "prepared":
        raise ValueError("durable targeted verification attempt state is invalid")

    admission = _adapter_targeted_verification_prepare(
        review_id=review_id,
        request_sha256=request_sha256,
        snapshot_sha256=snapshot_sha256,
        ticket=ticket,
    )
    if admission["state"] != "verification_prepared":
        raise ValueError("host did not admit the exact targeted verifier ticket")
    deadline = datetime.fromisoformat(admission["verification_deadline_utc"])
    remaining_seconds = int((deadline - datetime.now(timezone.utc)).total_seconds())
    if remaining_seconds <= 0:
        raise ValueError("targeted verifier has no time remaining before T90")
    dispatching_body = deepcopy(body)
    dispatching_body["targeted_verification"]["state"] = "dispatching"
    record, body = _replace_official_review_body(
        problem_id=problem_id, record=record, body=dispatching_body
    )

    try:
        receipt = verify_targeted_claim_service(
            statement=request["snapshot"]["statement_text"],
            proof=request["snapshot"]["blueprint_text"],
            ticket=ticket,
            verification_deadline_utc=admission["verification_deadline_utc"],
            endpoint=VERIFY_TARGETED_CLAIM_URL,
            timeout_seconds=min(3600, remaining_seconds),
            api_token=os.getenv("VERIFY_API_TOKEN") or None,
        )
    except BaseException as exc:
        ambiguous = (
            isinstance(exc, requests.RequestException)
            and getattr(exc, "response", None) is None
        )
        outcome_state = "execution_unknown" if ambiguous else "operational_blocked"
        error_sha = _targeted_error_sha256(exc)
        return _finalize_targeted_outcome(
            problem_id=problem_id,
            record=record,
            body=body,
            ticket=ticket,
            outcome_state=outcome_state,
            verification_receipt=None,
            error_sha256=error_sha,
        )

    return _finalize_targeted_outcome(
        problem_id=problem_id,
        record=record,
        body=body,
        ticket=ticket,
        outcome_state="completed",
        verification_receipt=receipt,
        error_sha256=None,
    )


def _latest_official_review(
    problem_id: str,
) -> tuple[Dict[str, Any], Dict[str, Any]] | None:
    matches: List[tuple[Dict[str, Any], Dict[str, Any]]] = []
    for record in _trusted_checkpoint_records(problem_id).values():
        body = record["record"]
        if (
            record["channel"] == "route_reviews"
            and body.get("schema_version") == _REVIEW_MEMORY_SCHEMA
            and body.get("state") == "official_published"
        ):
            matches.append((dict(record), deepcopy(body)))
    if not matches:
        return None
    for _record, body in matches:
        cutoff = body.get("official_published_timestamp_utc")
        _validate_canonical_utc_timestamp(cutoff, label="official route-review cutoff")
        if (
            not isinstance(body.get("official_published_record_id"), str)
            or not isinstance(body.get("official_published_record_sha256"), str)
            or SHA256_RE.fullmatch(body["official_published_record_sha256"]) is None
        ):
            raise ValueError("official route-review cutoff commitment is invalid")
    matches.sort(key=lambda pair: pair[1]["official_published_timestamp_utc"])
    if (
        len(matches) > 1
        and matches[-1][1]["official_published_timestamp_utc"]
        == matches[-2][1]["official_published_timestamp_utc"]
    ):
        raise ValueError("latest official review cutoff is ambiguous")
    return matches[-1]


def _authoritative_handoff_review_state(
    problem_id: str,
) -> tuple[Dict[str, Any] | None, int, bool, str | None]:
    latest = _latest_official_review(problem_id)
    if latest is None:
        return None, 0, False, None
    _record, body = latest
    decision = body["decision"]
    report = body["report"]
    transition = body.get("route_transition", {})
    next_route_id = (
        transition.get("next_route_id")
        if decision["effective_verdict"] == "red"
        else None
    )
    fallback_ids = (
        list(transition.get("fallback_evidence_record_ids", []))
        if next_route_id is not None
        else []
    )
    last_review = {
        "review_id": body["review_id"],
        "snapshot_sha256": body["snapshot_sha256"],
        "route_id": decision["route_id"],
        "verdict": report["verdict"],
        "effective_verdict": decision["effective_verdict"],
        "allowed_action": decision["allowed_action"],
        "next_route_id": next_route_id,
        "fallback_evidence_record_ids": fallback_ids,
    }
    expected_route = (
        next_route_id if next_route_id is not None else decision["route_id"]
    )
    switched = next_route_id is not None
    return (
        last_review,
        0 if switched else int(decision["yellow_streak"]),
        False if switched else bool(decision["route_frozen"]),
        expected_route,
    )


def _authoritative_handoff_pending(problem_id: str) -> Dict[str, str | None]:
    verification_ticket_id: str | None = None
    latest = _latest_official_review(problem_id)
    if latest is not None:
        _record, body = latest
        report = body.get("report")
        decision = body.get("decision")
        if (
            isinstance(report, dict)
            and report.get("load_bearing_claim") is not None
            and isinstance(decision, dict)
            and decision.get("effective_verdict") in {"green", "yellow"}
        ):
            targeted = body.get("targeted_verification")
            if not isinstance(targeted, dict) or targeted.get("state") not in {
                "completed",
            }:
                _request, ticket = _targeted_ticket_from_official_review(body)
                verification_ticket_id = ticket["ticket_id"]

    advisor_ids: List[str] = []
    for record in _trusted_checkpoint_records(problem_id).values():
        body = record["record"]
        if (
            record["channel"] == "events"
            and body.get("event_type") == "advisor_checkpoint"
            and body.get("status") == "waiting_owner_advisor_decision"
            and body.get("owner_action_required") is True
            and body.get("browser_dispatch_authorized") is False
            and body.get("advisor_request_id") is None
        ):
            advisor_ids.append(str(record["record_id"]))
    if len(advisor_ids) > 1:
        raise ValueError("multiple active advisor checkpoints make handoff ambiguous")
    return {
        "verification_ticket_id": verification_ticket_id,
        "advisor_checkpoint_id": None if not advisor_ids else advisor_ids[0],
    }


def context_handoff_prepare(
    *,
    purpose: str,
    active_route: Dict[str, Any],
    new_record_ids: List[str],
    obligations: List[str],
    next_action: Dict[str, Any],
) -> Dict[str, Any]:
    """Prepare a handoff whose control state is derived, never root-authored."""

    problem_id = validate_verified_problem_id(
        _required_review_env("RETHLAS_EXPECTED_PROBLEM_ID", label="handoff problem id")
    )
    run_id = _required_review_env(_REVIEW_RUN_ENV, label="handoff run id")
    _statement, statement_sha256 = _trusted_problem_statement(problem_id)
    _blueprint, blueprint_sha256 = _trusted_blueprint(problem_id)
    active_ids = _trusted_mathematical_evidence_records(problem_id)
    record_ids = _bounded_unique_record_ids(
        new_record_ids, label="handoff new_record_ids", maximum=96
    )
    missing = sorted(set(record_ids) - set(active_ids))
    if missing:
        raise ValueError(
            f"handoff record ids are not active durable records: {missing}"
        )
    last_review, yellow_streak, route_frozen, expected_route = (
        _authoritative_handoff_review_state(problem_id)
    )
    if (
        expected_route is not None
        and isinstance(active_route, dict)
        and active_route.get("route_id") != expected_route
    ):
        raise ValueError("handoff active route disagrees with the official disposition")
    result = _adapter_context_handoff_prepare(
        purpose=purpose,
        proposal={
            "active_route": deepcopy(active_route),
            "new_record_ids": record_ids,
            "obligations": deepcopy(obligations),
            "next_action": deepcopy(next_action),
        },
        assertions={
            "run_id": run_id,
            "problem_id": problem_id,
            "statement_sha256": statement_sha256,
            "blueprint_sha256": blueprint_sha256,
            "last_review": last_review,
            "yellow_streak": yellow_streak,
            "route_frozen": route_frozen,
        },
    )
    content = _validate_authoritative_handoff(result["content"])
    if content["purpose"] != purpose:
        raise ValueError("context handoff purpose changed after host admission")
    return {**deepcopy(result), "content": content}


def _required_handoff_identity() -> Dict[str, str]:
    names = {
        "handoff_id": _HANDOFF_REQUIRED_ID_ENV,
        "content_sha256": _HANDOFF_REQUIRED_SHA_ENV,
        "thread_epoch": _HANDOFF_THREAD_EPOCH_ENV,
    }
    return {
        key: _required_review_env(env_name, label=f"context handoff {key}")
        for key, env_name in names.items()
    }


def _query_authoritative_handoff_binding() -> Dict[str, str]:
    identity = _required_handoff_identity()
    result = _adapter_context_handoff_status(
        handoff_id=identity["handoff_id"],
        content_sha256=identity["content_sha256"],
    )
    binding = result.get("binding")
    if not isinstance(binding, dict):
        raise ValueError("host did not return an authenticated handoff binding")
    if binding.get("thread_epoch") != identity["thread_epoch"] or binding.get(
        "run_id"
    ) != _required_review_env(_REVIEW_RUN_ENV, label="handoff run id"):
        raise ValueError("host handoff binding does not match this MCP epoch")
    return {key: str(value) for key, value in binding.items()}


def _validate_authoritative_handoff(content: Mapping[str, Any]) -> Dict[str, Any]:
    normalized = validate_context_handoff(content)
    problem_id = validate_verified_problem_id(
        _required_review_env("RETHLAS_EXPECTED_PROBLEM_ID", label="handoff problem id")
    )
    run_id = _required_review_env(_REVIEW_RUN_ENV, label="handoff run id")
    _statement, statement_sha256 = _trusted_problem_statement(problem_id)
    _blueprint, blueprint_sha256 = _trusted_blueprint(problem_id)
    if (
        normalized["run_id"] != run_id
        or normalized["problem_id"] != problem_id
        or normalized["statement_sha256"] != statement_sha256
        or normalized["blueprint_sha256"] != blueprint_sha256
    ):
        raise ValueError("context handoff authoritative binding changed")
    active = _trusted_mathematical_evidence_records(problem_id)
    if not set(normalized["new_record_ids"]) <= set(active):
        raise ValueError("context handoff cites inactive durable records")
    last_review, yellow_streak, route_frozen, expected_route = (
        _authoritative_handoff_review_state(problem_id)
    )
    pending = _authoritative_handoff_pending(problem_id)
    if (
        normalized["last_review"] != last_review
        or normalized["yellow_streak"] != yellow_streak
        or normalized["route_frozen"] != route_frozen
        or normalized["pending"] != pending
        or (
            expected_route is not None
            and normalized["active_route"]["route_id"] != expected_route
        )
    ):
        raise ValueError("context handoff lost official route-review state")
    return normalized


def context_handoff_get(*, handoff_id: str, content_sha256: str) -> Dict[str, Any]:
    identity = _required_handoff_identity()
    if (
        handoff_id != identity["handoff_id"]
        or content_sha256 != identity["content_sha256"]
    ):
        raise ValueError("context handoff get does not match the host-required handoff")
    binding = _query_authoritative_handoff_binding()
    result = _adapter_context_handoff_get(
        handoff_id=handoff_id,
        content_sha256=content_sha256,
        thread_epoch=binding["thread_epoch"],
        root_thread_id=binding["root_thread_id"],
        root_turn_id=binding["root_turn_id"],
    )
    content = _validate_authoritative_handoff(result["content"])
    if (
        trusted_handoff_id(content) != handoff_id
        or handoff_sha256(content) != content_sha256
    ):
        raise ValueError("context handoff get body changed after host binding")
    return {**deepcopy(result), "content": content}


def context_handoff_status(*, handoff_id: str, content_sha256: str) -> Dict[str, Any]:
    return _adapter_context_handoff_status(
        handoff_id=handoff_id, content_sha256=content_sha256
    )


def route_cycle_close(
    *,
    handoff_id: str,
    content_sha256: str,
    disposition: str,
    next_milestone: Dict[str, Any],
) -> Dict[str, Any]:
    """Request one durable next cycle; the host still owns the T90 interrupt."""

    status = _adapter_context_handoff_status(
        handoff_id=handoff_id, content_sha256=content_sha256
    )
    binding = status.get("binding")
    if not isinstance(binding, dict):
        raise ValueError("route cycle handoff lacks an authenticated current binding")
    if binding.get("run_id") != _required_review_env(
        _REVIEW_RUN_ENV, label="handoff run id"
    ):
        raise ValueError("route cycle handoff belongs to another run")
    return _adapter_route_cycle_close(
        handoff_id=handoff_id,
        content_sha256=content_sha256,
        thread_epoch=str(binding["thread_epoch"]),
        root_thread_id=str(binding["root_thread_id"]),
        root_turn_id=str(binding["root_turn_id"]),
        disposition=disposition,
        next_milestone=next_milestone,
    )


def _context_rehydrate_preflight(tool_name: str) -> None:
    required_id = os.getenv(_HANDOFF_REQUIRED_ID_ENV)
    required_sha = os.getenv(_HANDOFF_REQUIRED_SHA_ENV)
    if required_id is None and required_sha is None:
        return
    identity = _required_handoff_identity()
    binding = _query_authoritative_handoff_binding()
    result = _adapter_context_handoff_preflight(
        handoff_id=identity["handoff_id"],
        content_sha256=identity["content_sha256"],
        thread_epoch=binding["thread_epoch"],
        root_thread_id=binding["root_thread_id"],
        root_turn_id=binding["root_turn_id"],
        tool_name=tool_name,
    )
    if result["state"] != "consumed":
        raise ValueError(
            "context handoff must be consumed in this exact new epoch before other tools"
        )


def _reasoning_phase_preflight(tool_name: str) -> Dict[str, Any] | None:
    """Fail closed when authenticated cadence forbids this exact MCP tool."""

    adapter_env_names = (
        "RETHLAS_REVIEW_ADAPTER_PATH",
        "RETHLAS_REVIEW_ADAPTER_SHA256",
        "RETHLAS_REVIEW_DB",
        "RETHLAS_REVIEW_CONTROL_TOKEN",
    )
    configured = [bool(os.getenv(name)) for name in adapter_env_names]
    if not any(configured):
        # Offline unit/dev use has no host cadence. Production binds all four.
        return None
    if not all(configured):
        raise ValueError("review adapter phase guard is only partially configured")
    result = _adapter_reasoning_phase_preflight(tool_name=tool_name)
    expected_run = _required_review_env(_REVIEW_RUN_ENV, label="review run id")
    expected_problem = validate_verified_problem_id(
        _required_review_env("RETHLAS_EXPECTED_PROBLEM_ID", label="review problem id")
    )
    if result["run_id"] != expected_run or result["problem_id"] != expected_problem:
        raise ValueError("host phase preflight belongs to another run or problem")
    if result["tool_permitted"] is not True:
        raise ValueError(
            f"MCP tool {tool_name!r} is forbidden during host phase "
            f"{result['phase']!r} ({result['allowed_action']!r})"
        )
    return result


def branch_update(
    problem_id: str,
    branch_id: str,
    state: Dict[str, Any],
) -> Dict[str, Any]:
    payload = {
        "branch_id": branch_id,
        "state": state,
    }
    return memory_append(problem_id, "branch_states", payload)


def _validate_generation_control_instance(instance_id: str) -> str:
    if (
        not isinstance(instance_id, str)
        or _GENERATION_CONTROL_INSTANCE_RE.fullmatch(instance_id) is None
    ):
        raise ValueError("generation control instance id must be 32 lowercase hex")
    return instance_id


def _generation_control_root() -> Path:
    root = GENERATION_CONTROL_ROOT.absolute()
    try:
        root.mkdir(mode=0o700)
    except FileExistsError:
        pass
    metadata = root.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or root.is_symlink()
        or stat.S_IMODE(metadata.st_mode) != 0o700
        or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
    ):
        raise ValueError("generation control root must be a real owner-only directory")
    return root


def _generation_control_path(problem_id: str, instance_id: str) -> Path:
    sanitized_problem_id = validate_verified_problem_id(problem_id)
    normalized_instance = _validate_generation_control_instance(instance_id)
    problem_digest = hashlib.sha256(sanitized_problem_id.encode("utf-8")).hexdigest()
    return _generation_control_root() / f"{normalized_instance}_{problem_digest}.json"


def _replace_generation_control(path: Path, payload: Dict[str, Any]) -> None:
    """Atomically replace one deterministic current-state control record.

    Unlike immutable phase checkpoints, this file intentionally represents the
    latest owner-resume or generation-yield state.  Retrying after an ambiguous
    directory fsync writes the same desired payload and is therefore safe.
    """

    encoded = (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    if len(encoded) > MAX_GENERATION_CONTROL_FILE_BYTES:
        raise ValueError("generation control record exceeds its file-size limit")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _validate_generation_control_reason(reason: str) -> str:
    if (
        not isinstance(reason, str)
        or not reason
        or reason != reason.strip()
        or "\x00" in reason
        or len(reason.encode("utf-8")) > MAX_GENERATION_CONTROL_REASON_BYTES
    ):
        raise ValueError(
            "generation control reason must be a trimmed non-empty UTF-8 string "
            f"of at most {MAX_GENERATION_CONTROL_REASON_BYTES} bytes"
        )
    return reason


def _validate_generation_control_evidence_ids(
    evidence_record_ids: List[str],
) -> List[str]:
    if (
        not isinstance(evidence_record_ids, list)
        or not 1 <= len(evidence_record_ids) <= MAX_GENERATION_CONTROL_EVIDENCE_IDS
    ):
        raise ValueError(
            "waiting generation control requires between 1 and "
            f"{MAX_GENERATION_CONTROL_EVIDENCE_IDS} evidence record ids"
        )
    if any(
        not isinstance(record_id, str)
        or _GENERATION_CONTROL_EVIDENCE_ID_RE.fullmatch(record_id) is None
        for record_id in evidence_record_ids
    ):
        raise ValueError("generation control evidence record ids are invalid")
    if len(set(evidence_record_ids)) != len(evidence_record_ids):
        raise ValueError("generation control evidence record ids must be unique")
    return list(evidence_record_ids)


def _active_memory_records_by_id(
    problem_id: str,
) -> Dict[str, Dict[str, Any]]:
    entries_by_channel = _load_memory_entries(problem_id)
    return {
        str(entry["record_id"]): entry
        for entries in entries_by_channel.values()
        for entry in entries
        if entry["effective_active"]
    }


def _validate_generation_wait_evidence(
    problem_id: str,
    state: str,
    evidence_record_ids: List[str],
) -> None:
    active = _active_memory_records_by_id(problem_id)
    missing = sorted(set(evidence_record_ids) - set(active))
    if missing:
        raise ValueError(
            "generation control evidence is not active memory: " + ", ".join(missing)
        )

    cited = [active[record_id] for record_id in evidence_record_ids]
    branch_bound = False
    event_bound = False
    for entry in cited:
        raw_item = entry.get("item")
        record = raw_item.get("record") if isinstance(raw_item, dict) else None
        if not isinstance(record, dict):
            continue
        if entry.get("channel") == "branch_states":
            branch_state = record.get("state")
            branch_bound = branch_bound or (
                isinstance(branch_state, dict) and branch_state.get("status") == state
            )
        if entry.get("channel") != "events" or record.get("status") != state:
            continue
        if state == "waiting_cost_gate":
            event_bound = event_bound or (
                record.get("event_type") == "recursive_proving_round"
            )
        else:
            event_bound = event_bound or (
                record.get("event_type") == "advisor_checkpoint"
                and record.get("owner_action_required") is True
                and record.get("browser_dispatch_authorized") is False
                and record.get("advisor_request_id") is None
            )
    if not branch_bound:
        raise ValueError(
            "generation yield must cite an active branch state with the exact wait status"
        )
    if not event_bound:
        raise ValueError(
            "generation yield must cite the matching active evidence event"
        )


def _generation_control_payload(
    problem_id: str,
    *,
    instance_id: str,
    state: str,
    reason: str,
    evidence_record_ids: List[str],
) -> Dict[str, Any]:
    sanitized_problem_id = validate_verified_problem_id(problem_id)
    _statement, statement_digest = _trusted_problem_statement(sanitized_problem_id)
    normalized_instance = _validate_generation_control_instance(instance_id)
    normalized_reason = _validate_generation_control_reason(reason)
    if state == "running":
        if evidence_record_ids:
            raise ValueError("running generation control cannot cite wait evidence")
        normalized_evidence: List[str] = []
    elif state in GENERATION_WAIT_STATES:
        normalized_evidence = _validate_generation_control_evidence_ids(
            evidence_record_ids
        )
        _validate_generation_wait_evidence(
            sanitized_problem_id, state, normalized_evidence
        )
    else:
        raise ValueError("generation control state is invalid")

    return {
        "schema": GENERATION_CONTROL_SCHEMA,
        "instance_id": normalized_instance,
        "problem_id": sanitized_problem_id,
        "statement_sha256": statement_digest,
        "state": state,
        "reason": normalized_reason,
        "evidence_record_ids": normalized_evidence,
    }


def _set_generation_control(
    problem_id: str,
    *,
    instance_id: str,
    state: str,
    reason: str,
    evidence_record_ids: List[str],
) -> Dict[str, Any]:
    payload = _generation_control_payload(
        problem_id,
        instance_id=instance_id,
        state=state,
        reason=reason,
        evidence_record_ids=evidence_record_ids,
    )
    _replace_generation_control(
        _generation_control_path(payload["problem_id"], payload["instance_id"]),
        payload,
    )
    return dict(payload)


def generation_yield(
    problem_id: str,
    state: str,
    reason: str,
    evidence_record_ids: List[str],
) -> Dict[str, Any]:
    """Persist a truthful unfinished yield that the runner must honor."""

    if state not in GENERATION_WAIT_STATES:
        raise ValueError(
            "generation_yield state must be waiting_cost_gate or "
            "waiting_owner_advisor_decision"
        )
    instance_id = os.environ.get("RETHLAS_GENERATION_CONTROL_TOKEN", "")
    payload = _generation_control_payload(
        problem_id,
        instance_id=instance_id,
        state=state,
        reason=reason,
        evidence_record_ids=evidence_record_ids,
    )
    reason_sha256 = hashlib.sha256(payload["reason"].encode("utf-8")).hexdigest()
    admission = _adapter_generation_yield_prepare(
        state=payload["state"],
        reason_sha256=reason_sha256,
        evidence_record_ids=list(payload["evidence_record_ids"]),
    )
    if (
        admission["run_id"]
        != _required_review_env(_REVIEW_RUN_ENV, label="generation-yield run id")
        or admission["state"] != payload["state"]
        or admission["reason_sha256"] != reason_sha256
        or admission["evidence_record_ids"] != payload["evidence_record_ids"]
    ):
        raise ValueError("host generation-yield admission changed the exact wait")
    _replace_generation_control(
        _generation_control_path(payload["problem_id"], payload["instance_id"]),
        payload,
    )
    return dict(payload)


def generation_control_resume(problem_id: str, instance_id: str) -> Dict[str, Any]:
    """Record the repository owner's explicit runner invocation as a resume."""

    return _set_generation_control(
        problem_id,
        instance_id=instance_id,
        state="running",
        reason="owner_runner_started",
        evidence_record_ids=[],
    )


def generation_control_status(problem_id: str, instance_id: str) -> Dict[str, Any]:
    sanitized_problem_id = validate_verified_problem_id(problem_id)
    normalized_instance = _validate_generation_control_instance(instance_id)
    _statement, statement_digest = _trusted_problem_statement(sanitized_problem_id)
    path = _generation_control_path(sanitized_problem_id, normalized_instance)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return {
            "schema": GENERATION_CONTROL_SCHEMA,
            "instance_id": normalized_instance,
            "problem_id": sanitized_problem_id,
            "statement_sha256": statement_digest,
            "state": "running",
            "reason": "no_generation_control_record",
            "evidence_record_ids": [],
        }
    if (
        not stat.S_ISREG(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
        or metadata.st_size <= 0
        or metadata.st_size > MAX_GENERATION_CONTROL_FILE_BYTES
    ):
        raise ValueError("generation control record is not a safe bounded file")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or stat.S_IMODE(opened.st_mode) != 0o600
            or (hasattr(os, "getuid") and opened.st_uid != os.getuid())
            or opened.st_size <= 0
            or opened.st_size > MAX_GENERATION_CONTROL_FILE_BYTES
        ):
            raise ValueError("generation control record changed during open")
        raw_bytes = os.read(descriptor, MAX_GENERATION_CONTROL_FILE_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(raw_bytes) > MAX_GENERATION_CONTROL_FILE_BYTES:
        raise ValueError("generation control record exceeds its file-size limit")
    try:
        raw = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("generation control record is not UTF-8") from exc
    payload = _strict_json_loads(raw, label="generation control record")
    if not isinstance(payload, dict) or set(payload) != {
        "schema",
        "instance_id",
        "problem_id",
        "statement_sha256",
        "state",
        "reason",
        "evidence_record_ids",
    }:
        raise ValueError("generation control record has an invalid envelope")
    if (
        payload.get("schema") != GENERATION_CONTROL_SCHEMA
        or payload.get("instance_id") != normalized_instance
        or payload.get("problem_id") != sanitized_problem_id
        or payload.get("statement_sha256") != statement_digest
    ):
        raise ValueError("generation control record has invalid bindings")
    state = payload.get("state")
    reason = payload.get("reason")
    evidence = payload.get("evidence_record_ids")
    _validate_generation_control_reason(reason)
    if state == "running":
        if evidence != []:
            raise ValueError("running generation control has invalid evidence")
    elif state in GENERATION_WAIT_STATES:
        normalized_evidence = _validate_generation_control_evidence_ids(evidence)
        _validate_generation_wait_evidence(
            sanitized_problem_id, state, normalized_evidence
        )
    else:
        raise ValueError("generation control record has an invalid state")
    return dict(payload)


def generation_control_receipt(problem_id: str, instance_id: str) -> Dict[str, Any]:
    """Content-bind one trusted control projection for host cadence admission."""

    control = generation_control_status(problem_id, instance_id)
    return {
        "schema_version": "rethlas_generation_control_receipt_v1",
        "control": control,
        "record_sha256": hashlib.sha256(canonical_json_bytes(control)).hexdigest(),
    }


def build_mcp_app() -> Optional[Any]:
    if FastMCP is None:
        return None

    app = FastMCP("reasoning-agent")

    def _guarded_tool(name: str, *, rehydrate_exempt: bool = False):
        def register(function: Any) -> Any:
            @wraps(function)
            def guarded(*args: Any, **kwargs: Any) -> Any:
                if not rehydrate_exempt:
                    _context_rehydrate_preflight(name)
                _reasoning_phase_preflight(name)
                return function(*args, **kwargs)

            guarded._rethlas_rehydrate_guarded = not rehydrate_exempt  # type: ignore[attr-defined]
            guarded._rethlas_phase_guarded = True  # type: ignore[attr-defined]
            return app.tool(name=name)(guarded)

        return register

    @_guarded_tool("search_matlas_theorems")
    def _tool_search_matlas_theorems(
        query: str,
        num_results: int = 10,
    ) -> Dict[str, Any]:
        """Search official Matlas for published mathematical statements."""
        return search_matlas_theorems(query=query, num_results=num_results)

    @_guarded_tool("search_arxiv_theorems")
    def _tool_search_arxiv_theorems(
        query: str,
        num_results: int = 10,
    ) -> Dict[str, Any]:
        """Search the separate legacy Danus arXiv theorem service."""
        return search_arxiv_theorems(query=query, num_results=num_results)

    @_guarded_tool("verify_blueprint_service")
    def _tool_verify_blueprint_service(
        problem_id: str,
    ) -> Dict[str, Any]:
        """Verify and atomically publish results/{problem_id}/blueprint.md."""
        return verify_blueprint_service(problem_id=problem_id)

    @_guarded_tool("advisor_report_get")
    def _tool_advisor_report_get(
        problem_id: str,
        run_id: str,
        receipt_id: str,
        expected_receipt_sha256: str,
    ) -> Dict[str, Any]:
        """Read a digest-bound Chrome advisor receipt as untrusted data."""
        return advisor_report_get(
            problem_id=problem_id,
            run_id=run_id,
            receipt_id=receipt_id,
            expected_receipt_sha256=expected_receipt_sha256,
        )

    @_guarded_tool("memory_init")
    def _tool_memory_init(
        problem_id: str,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return memory_init(problem_id=problem_id, meta=meta)

    @_guarded_tool("memory_append")
    def _tool_memory_append(
        problem_id: str,
        channel: str,
        record: Dict[str, Any],
        active: bool = True,
        supersedes: Optional[List[str]] = None,
        return_mode: str = "metadata",
    ) -> Dict[str, Any]:
        """Append a record and return compact metadata, or the full entry on request."""
        return memory_append(
            problem_id=problem_id,
            channel=channel,
            record=record,
            active=active,
            supersedes=supersedes,
            return_mode=return_mode,
        )

    @_guarded_tool("memory_append_batch")
    def _tool_memory_append_batch(
        problem_id: str,
        items: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Persist a bounded phase checkpoint in one compact tool call."""
        return memory_append_batch(problem_id=problem_id, items=items)

    @_guarded_tool("memory_search")
    def _tool_memory_search(
        problem_id: str,
        query: str,
        channels: Optional[List[str]] = None,
        limit_per_channel: int = 10,
        max_chars: int = DEFAULT_MEMORY_SEARCH_MAX_CHARS,
        include_inactive: bool = False,
        newest_first: bool = True,
    ) -> Dict[str, Any]:
        """BM25-search active records within a whole-record character budget."""
        return memory_search(
            problem_id=problem_id,
            query=query,
            channels=channels,
            limit_per_channel=limit_per_channel,
            max_chars=max_chars,
            include_inactive=include_inactive,
            newest_first=newest_first,
        )

    @_guarded_tool("branch_update")
    def _tool_branch_update(
        problem_id: str,
        branch_id: str,
        state: Dict[str, Any],
    ) -> Dict[str, Any]:
        return branch_update(problem_id=problem_id, branch_id=branch_id, state=state)

    @_guarded_tool("review_frontier_status")
    def _tool_review_frontier_status(
        cycle_id: str,
        cycle: str,
        review_ordinal: int,
    ) -> Dict[str, Any]:
        """Return the exact pre-boundary durable ids a restricted critic may use."""
        return review_frontier_status(
            cycle_id=cycle_id,
            cycle=cycle,
            review_ordinal=review_ordinal,
        )

    @_guarded_tool("route_review_prepare")
    def _tool_route_review_prepare(
        review_id: str,
        cycle_id: str,
        cycle: str,
        review_ordinal: int,
        frontier_manifest_sha256: str,
        frontier_record_ids: List[str],
        progress_record_ids: List[str],
    ) -> Dict[str, Any]:
        """Prepare only the exact official cycle announced by the host scheduler."""
        return route_review_prepare(
            review_id=review_id,
            cycle_id=cycle_id,
            cycle=cycle,
            review_ordinal=review_ordinal,
            frontier_manifest_sha256=frontier_manifest_sha256,
            frontier_record_ids=frontier_record_ids,
            progress_record_ids=progress_record_ids,
        )

    @_guarded_tool("route_review_wait")
    def _tool_route_review_wait(
        review_id: str,
        request_sha256: str,
        snapshot_sha256: str,
    ) -> Dict[str, Any]:
        """Wait boundedly for one already-prepared independent review."""
        return route_review_wait(
            review_id=review_id,
            request_sha256=request_sha256,
            snapshot_sha256=snapshot_sha256,
        )

    @_guarded_tool("route_review_status")
    def _tool_route_review_status(
        review_id: str,
        request_sha256: str,
        snapshot_sha256: str,
    ) -> Dict[str, Any]:
        """Read one digest-bound review state without launching or retrying it."""
        return route_review_status(
            review_id=review_id,
            request_sha256=request_sha256,
            snapshot_sha256=snapshot_sha256,
        )

    @_guarded_tool("route_review_close")
    def _tool_route_review_close(
        review_id: str,
        request_sha256: str,
        snapshot_sha256: str,
    ) -> Dict[str, Any]:
        """Acknowledge one terminal review; never convert failure into a verdict."""
        return route_review_close(
            review_id=review_id,
            request_sha256=request_sha256,
            snapshot_sha256=snapshot_sha256,
        )

    @_guarded_tool("verify_review_claim")
    def _tool_verify_review_claim(
        review_id: str,
        request_sha256: str,
        snapshot_sha256: str,
    ) -> Dict[str, Any]:
        """Verify only the exact claim selected by one official route critic."""
        return verify_review_claim(
            review_id=review_id,
            request_sha256=request_sha256,
            snapshot_sha256=snapshot_sha256,
        )

    @_guarded_tool("context_handoff_prepare")
    def _tool_context_handoff_prepare(
        purpose: str,
        active_route: Dict[str, Any],
        new_record_ids: List[str],
        obligations: List[str],
        next_action: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Prepare a content-addressed <=32 KiB state handoff for a fresh thread."""
        return context_handoff_prepare(
            purpose=purpose,
            active_route=active_route,
            new_record_ids=new_record_ids,
            obligations=obligations,
            next_action=next_action,
        )

    @_guarded_tool("context_handoff_get", rehydrate_exempt=True)
    def _tool_context_handoff_get(
        handoff_id: str,
        content_sha256: str,
    ) -> Dict[str, Any]:
        """Read one exact immutable handoff after the host starts a fresh epoch."""
        return context_handoff_get(
            handoff_id=handoff_id,
            content_sha256=content_sha256,
        )

    @_guarded_tool("context_handoff_status")
    def _tool_context_handoff_status(
        handoff_id: str,
        content_sha256: str,
    ) -> Dict[str, Any]:
        """Inspect a handoff state without disclosing or mutating its content."""
        return context_handoff_status(
            handoff_id=handoff_id,
            content_sha256=content_sha256,
        )

    @_guarded_tool("route_cycle_close")
    def _tool_route_cycle_close(
        handoff_id: str,
        content_sha256: str,
        disposition: str,
        next_milestone: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Request continuation at T87; T90 remains an external hard stop."""
        return route_cycle_close(
            handoff_id=handoff_id,
            content_sha256=content_sha256,
            disposition=disposition,
            next_milestone=next_milestone,
        )

    @_guarded_tool("generation_yield")
    def _tool_generation_yield(
        problem_id: str,
        state: str,
        reason: str,
        evidence_record_ids: List[str],
    ) -> Dict[str, Any]:
        """Durably yield an unfinished run for an exact owner-side decision."""
        return generation_yield(
            problem_id=problem_id,
            state=state,
            reason=reason,
            evidence_record_ids=evidence_record_ids,
        )

    return app


APP = build_mcp_app()


def main() -> None:
    control_token = os.environ.get("RETHLAS_GENERATION_CONTROL_TOKEN", "")
    if len(sys.argv) == 3 and sys.argv[1] == "--generation-control-state":
        print(generation_control_status(sys.argv[2], control_token)["state"])
        return
    if len(sys.argv) == 3 and sys.argv[1] == "--generation-control-resume":
        generation_control_resume(sys.argv[2], control_token)
        return
    if len(sys.argv) == 3 and sys.argv[1] == "--generation-control-receipt":
        receipt = generation_control_receipt(sys.argv[2], control_token)
        print(canonical_json_bytes(receipt).decode("utf-8"))
        return
    if APP is None:
        raise SystemExit(
            "fastmcp is not installed. Install requirements from mcp/requirements.txt first."
        )
    APP.run()


if __name__ == "__main__":
    main()

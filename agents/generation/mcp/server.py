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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import requests

if __package__ in {None, ""}:
    # ``python -I /attested/snapshot/mcp/server.py`` intentionally removes the
    # script directory from sys.path. Re-add only this exact attested sibling
    # directory so the dependency-free generation-control CLI can use the same
    # module without trusting cwd or PYTHONPATH.
    sys.path.insert(0, str(Path(__file__).resolve(strict=True).parent))

try:
    from .advisor_client import advisor_report_get
except ImportError:  # pragma: no cover - direct module execution
    from advisor_client import advisor_report_get

try:
    from .proof_context import parse_blueprint
except ImportError:  # pragma: no cover - direct module execution
    from proof_context import parse_blueprint

try:
    from .verification_client import (
        expected_attestation,
        proof_digest,
        validate_service_response,
        verify_blueprint_file,
    )
except ImportError:  # pragma: no cover - direct `python mcp/server.py` execution
    from verification_client import (  # type: ignore[no-redef]
        expected_attestation,
        proof_digest,
        validate_service_response,
        verify_blueprint_file,
    )

try:
    from fastmcp import FastMCP
except (
    ImportError
):  # pragma: no cover - dependency should be installed via requirements
    FastMCP = None  # type: ignore[assignment]

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

THEOREM_SEARCH_URL = "https://leansearch.net/thm/search"
THEOREM_SEARCH_TASK = (
    "Given a math statement, retrieve useful references, such as theorems, "
    "lemmas, and definitions, that are useful for solving the given problem."
)

VERIFY_PROOF_URL = os.getenv(
    "VERIFY_PROOF_URL",
    "http://127.0.0.1:8091/verify",
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

CHANNEL_FILES: Dict[str, str] = {
    "immediate_conclusions": "immediate_conclusions.jsonl",
    "toy_examples": "toy_examples.jsonl",
    "counterexamples": "counterexamples.jsonl",
    "big_decisions": "big_decisions.jsonl",
    "subgoals": "subgoals.jsonl",
    "proof_steps": "proof_steps.jsonl",
    "failed_paths": "failed_paths.jsonl",
    "verification_reports": "verification_reports.jsonl",
    "branch_states": "branch_states.jsonl",
    "events": "events.jsonl",
}


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


def search_arxiv_theorems(
    query: str,
    num_results: int = 10,
    endpoint: str = THEOREM_SEARCH_URL,
    timeout_seconds: int = 30,
) -> Dict[str, Any]:
    if not query.strip():
        raise ValueError("query must be non-empty")
    if num_results <= 0:
        raise ValueError("num_results must be > 0")

    payload = {
        "query": query,
        "task": THEOREM_SEARCH_TASK,
        "num_results": num_results,
    }

    response = requests.post(endpoint, json=payload, timeout=timeout_seconds)
    response.raise_for_status()

    data = response.json()
    if not isinstance(data, list):
        raise ValueError("The theorem endpoint must return a JSON list")

    normalized: List[Dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        normalized.append(
            {
                "title": str(item.get("title", "")),
                "theorem": str(item.get("theorem", "")),
                "arxiv_id": str(item.get("arxiv_id", "")),
                "theorem_id": str(item.get("theorem_id", "")),
            }
        )

    return {
        "query": query,
        "count": len(normalized),
        "results": normalized,
        "endpoint": endpoint,
    }


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
        timeout_seconds=timeout_seconds,
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


def _set_generation_control(
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

    payload = {
        "schema": GENERATION_CONTROL_SCHEMA,
        "instance_id": normalized_instance,
        "problem_id": sanitized_problem_id,
        "statement_sha256": statement_digest,
        "state": state,
        "reason": normalized_reason,
        "evidence_record_ids": normalized_evidence,
    }
    _replace_generation_control(
        _generation_control_path(sanitized_problem_id, normalized_instance), payload
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
    return _set_generation_control(
        problem_id,
        instance_id=os.environ.get("RETHLAS_GENERATION_CONTROL_TOKEN", ""),
        state=state,
        reason=reason,
        evidence_record_ids=evidence_record_ids,
    )


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


def build_mcp_app() -> Optional[Any]:
    if FastMCP is None:
        return None

    app = FastMCP("reasoning-agent")

    @app.tool(name="search_arxiv_theorems")
    def _tool_search_arxiv_theorems(
        query: str,
        num_results: int = 10,
    ) -> Dict[str, Any]:
        return search_arxiv_theorems(query=query, num_results=num_results)

    @app.tool(name="verify_blueprint_service")
    def _tool_verify_blueprint_service(
        problem_id: str,
    ) -> Dict[str, Any]:
        """Verify and atomically publish results/{problem_id}/blueprint.md."""
        return verify_blueprint_service(problem_id=problem_id)

    @app.tool(name="advisor_report_get")
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

    @app.tool(name="memory_init")
    def _tool_memory_init(
        problem_id: str,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return memory_init(problem_id=problem_id, meta=meta)

    @app.tool(name="memory_append")
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

    @app.tool(name="memory_append_batch")
    def _tool_memory_append_batch(
        problem_id: str,
        items: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Persist a bounded phase checkpoint in one compact tool call."""
        return memory_append_batch(problem_id=problem_id, items=items)

    @app.tool(name="memory_search")
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

    @app.tool(name="branch_update")
    def _tool_branch_update(
        problem_id: str,
        branch_id: str,
        state: Dict[str, Any],
    ) -> Dict[str, Any]:
        return branch_update(problem_id=problem_id, branch_id=branch_id, state=state)

    @app.tool(name="generation_yield")
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
    if APP is None:
        raise SystemExit(
            "fastmcp is not installed. Install requirements from mcp/requirements.txt first."
        )
    APP.run()


if __name__ == "__main__":
    main()

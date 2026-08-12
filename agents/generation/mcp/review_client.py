"""Bounded client for the host-owned route-review control adapter.

The generation MCP process never launches a critic and never opens the
HotJoin database.  It invokes one runner-attested adapter executable with an
exact command, sends canonical JSON only on stdin, and validates the bounded
response.  The scoped control token is inherited only through the child
environment; mathematical content never appears in argv.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import stat
import subprocess
import sys
import tempfile
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

try:
    from ...review.contracts import (
        HANDOFF_ID_RE,
        RECORD_ID_RE,
        REVIEW_ID_RE,
        SHA256_RE,
        ReviewContractError,
        canonical_json_bytes,
        handoff_id,
        handoff_sha256,
        strict_json_loads,
        validate_context_handoff,
        validate_targeted_verification_ticket,
    )
    from ...review.critic import validate_review_request
except ImportError:  # pragma: no cover - trusted snapshot/direct execution
    # The runner copies the review package beside this attested MCP directory
    # and binds it on sys.path.  There is intentionally no cwd/PYTHONPATH
    # fallback here.
    from review.contracts import (  # type: ignore[no-redef]
        HANDOFF_ID_RE,
        RECORD_ID_RE,
        REVIEW_ID_RE,
        SHA256_RE,
        ReviewContractError,
        canonical_json_bytes,
        handoff_id,
        handoff_sha256,
        strict_json_loads,
        validate_context_handoff,
        validate_targeted_verification_ticket,
    )
    from review.critic import validate_review_request  # type: ignore[no-redef]


ADAPTER_COMMAND_SCHEMA = "rethlas_review_adapter_command_v1"
ADAPTER_RESPONSE_SCHEMA = "rethlas_review_adapter_response_v1"
MAX_ADAPTER_REQUEST_BYTES = 229_376
MAX_ADAPTER_RESPONSE_BYTES = 262_144
MAX_ACCEPTED_MEMORY_BATCH_PUBLICATIONS = 128
_MEMORY_BATCH_PROBLEM_COMPONENT_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9-])?$"
)
_MEMORY_BATCH_BOOT_IDENTITY_RE = re.compile(r"^[ -~]{1,128}$")
MAX_ADAPTER_STDERR_BYTES = 4_096
ADAPTER_ENV_PATH = "RETHLAS_REVIEW_ADAPTER_PATH"
ADAPTER_ENV_SHA256 = "RETHLAS_REVIEW_ADAPTER_SHA256"
ADAPTER_ENV_DB = "RETHLAS_REVIEW_DB"
CONTROL_TOKEN_ENV = "RETHLAS_REVIEW_CONTROL_TOKEN"
EXPECTED_RUN_ENV = "RETHLAS_EXPECTED_HOTJOIN_RUN_ID"
CONTROL_TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")

REVIEW_STATES = frozenset(
    {
        "prepared",
        "running",
        "completed_pending_close",
        "completed_pending_publication",
        "completed",
        "operational_blocked",
        "execution_unknown",
        "verification_required",
        "verification_prepared",
        "verification_pending_publication",
        "verification_unknown",
        "closed",
    }
)
HANDOFF_STATES = frozenset({"prepared", "available", "consumed"})
HANDOFF_PURPOSES = frozenset({"context_guard", "owner_yield", "cycle_close"})
PUBLICATION_RECEIPT_SCHEMA = "rethlas_route_review_publication_receipt_v1"
MEMORY_BATCH_PUBLICATION_RECEIPT_SCHEMA = (
    "rethlas_memory_batch_publication_receipt_v1"
)
MEMORY_BATCH_PUBLICATION_STATUS_SCHEMA = (
    "rethlas_memory_batch_publication_status_v1"
)
_MEMORY_BATCH_PUBLICATION_RECEIPT_KEYS = {
    "schema_version",
    "state",
    "run_id",
    "problem_id",
    "batch_id",
    "checkpoint_sha256",
    "commit_sha256",
    "publication_class",
    "cycle_id",
    "cutoff_action_id",
    "cutoff_kind",
    "cutoff_at_utc",
    "cutoff_monotonic",
    "accepted_at_utc",
    "accepted_at_monotonic",
    "boot_identity",
    "receipt_sha256",
}
_PUBLICATION_RECEIPT_KEYS = {
    "schema_version",
    "problem_id",
    "review_id",
    "request_sha256",
    "snapshot_sha256",
    "batch_id",
    "record_id",
    "timestamp_utc",
    "checkpoint_sha256",
    "record_sha256",
    "publication_state",
}
ROUTE_TRANSITION_PUBLICATION_RECEIPT_SCHEMA = (
    "rethlas_route_transition_publication_receipt_v1"
)
_ROUTE_TRANSITION_PUBLICATION_RECEIPT_KEYS = {
    "schema_version",
    "problem_id",
    "review_id",
    "request_sha256",
    "snapshot_sha256",
    "from_route_id",
    "to_route_id",
    "batch_id",
    "record_ids",
    "timestamp_utc",
    "checkpoint_sha256",
    "transition_sha256",
    "receipt_sha256",
}
TARGETED_RECEIPT_SCHEMA = "rethlas_targeted_claim_verification_receipt_v1"
_TARGETED_RECEIPT_KEYS = {
    "schema_version",
    "ticket_id",
    "review_id",
    "snapshot_sha256",
    "route_id",
    "blueprint_sha256",
    "blueprint_item_id",
    "blueprint_item_label",
    "claim_sha256",
    "verification_deadline_utc",
    "verification_status",
    "verdict",
    "verification_report",
    "repair_hints",
    "checked_item_ids",
    "context_attestation",
    "publication_authority",
    "whole_blueprint_verdict_authority",
    "receipt_sha256",
}
TARGETED_PUBLICATION_RECEIPT_SCHEMA = (
    "rethlas_targeted_verification_publication_receipt_v1"
)
_TARGETED_PUBLICATION_RECEIPT_KEYS = {
    "schema_version",
    "problem_id",
    "review_id",
    "request_sha256",
    "snapshot_sha256",
    "ticket_id",
    "verifier_receipt_sha256",
    "batch_id",
    "record_id",
    "timestamp_utc",
    "checkpoint_sha256",
    "record_sha256",
    "publication_state",
}


class ReviewAdapterError(RuntimeError):
    """The host review adapter was unavailable or violated its contract."""


def _exact_object(value: Any, keys: set[str], *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ReviewAdapterError(f"{label} has an unsupported shape")
    return value


def _validate_adapter_file() -> tuple[Path, tuple[int, int, int, int], bytes]:
    raw_path = os.environ.get(ADAPTER_ENV_PATH, "")
    expected_sha = os.environ.get(ADAPTER_ENV_SHA256, "")
    if not raw_path or not Path(raw_path).is_absolute():
        raise ReviewAdapterError("runner did not bind an absolute review adapter path")
    if SHA256_RE.fullmatch(expected_sha) is None:
        raise ReviewAdapterError("runner did not bind a valid review adapter SHA-256")
    path = Path(os.path.abspath(raw_path))
    try:
        before = path.lstat()
    except OSError as exc:
        raise ReviewAdapterError("review adapter path is unavailable") from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or path.is_symlink()
        or before.st_nlink != 1
        or (hasattr(os, "getuid") and before.st_uid != os.getuid())
        or stat.S_IMODE(before.st_mode) & 0o022
    ):
        raise ReviewAdapterError("review adapter must be an owner-controlled regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
            or opened.st_size < 1
            or opened.st_size > 16 * 1024 * 1024
        ):
            raise ReviewAdapterError("review adapter changed during secure open")
        digest = hashlib.sha256()
        pinned_bytes = bytearray()
        remaining = opened.st_size
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            digest.update(chunk)
            pinned_bytes.extend(chunk)
            remaining -= len(chunk)
        if remaining != 0 or os.read(descriptor, 1):
            raise ReviewAdapterError("review adapter changed during hashing")
        if digest.hexdigest() != expected_sha:
            raise ReviewAdapterError("review adapter SHA-256 does not match runner binding")
        binding = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
    finally:
        os.close(descriptor)
    return path, binding, bytes(pinned_bytes)


def _write_pinned_adapter(directory: Path, content: bytes) -> Path:
    """Materialize only already-hashed bytes inside one owner-only directory."""

    os.chmod(directory, 0o700)
    target = directory / "pinned_review_adapter.py"
    descriptor = os.open(
        target,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:  # pragma: no cover - os.write writes or raises
                raise ReviewAdapterError("pinned review adapter copy was short")
            view = view[written:]
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o500)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != len(content):
            raise ReviewAdapterError("pinned review adapter copy changed")
    finally:
        os.close(descriptor)
    if hashlib.sha256(target.read_bytes()).hexdigest() != os.environ.get(
        ADAPTER_ENV_SHA256
    ):
        raise ReviewAdapterError("pinned review adapter copy digest mismatch")
    return target


def _assert_adapter_unchanged(path: Path, binding: tuple[int, int, int, int]) -> None:
    try:
        after = path.lstat()
    except OSError as exc:
        raise ReviewAdapterError("review adapter disappeared during invocation") from exc
    observed = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if observed != binding or not stat.S_ISREG(after.st_mode) or path.is_symlink():
        raise ReviewAdapterError("review adapter changed during invocation")


def _adapter_env() -> dict[str, str]:
    token = os.environ.get(CONTROL_TOKEN_ENV, "")
    if CONTROL_TOKEN_RE.fullmatch(token) is None:
        raise ReviewAdapterError("runner did not bind a scoped review control token")
    env = {
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PYTHONDONTWRITEBYTECODE": "1",
        CONTROL_TOKEN_ENV: token,
    }
    # The DB path is control metadata, not mathematical content.  Pass it via
    # the environment so argv stays constant across requests.
    db_path = os.environ.get(ADAPTER_ENV_DB, "")
    if not db_path or not Path(db_path).is_absolute() or "\x00" in db_path:
        raise ReviewAdapterError("runner did not bind an absolute review adapter DB")
    env[ADAPTER_ENV_DB] = db_path
    return env


def _invoke_adapter(
    command: str,
    payload: Mapping[str, Any],
    *,
    timeout_seconds: int,
) -> dict[str, Any]:
    """Invoke an attested adapter once; mathematical content is stdin-only."""

    if command not in {
        "review-prepare",
        "review-wait",
        "review-status",
        "review-close",
        "context-handoff-prepare",
        "context-handoff-get",
        "context-handoff-status",
    }:
        raise ReviewAdapterError("unsupported review adapter command")
    raw = canonical_json_bytes(payload)
    if len(raw) > MAX_ADAPTER_REQUEST_BYTES:
        raise ReviewAdapterError("review adapter request exceeds its byte bound")
    path, binding, pinned_bytes = _validate_adapter_file()
    try:
        with tempfile.TemporaryDirectory(prefix="rethlas-review-adapter-") as raw_temp:
            pinned_root = Path(raw_temp)
            pinned_path = _write_pinned_adapter(pinned_root, pinned_bytes)
            completed = subprocess.run(
                [sys.executable, "-I", "-B", os.fspath(pinned_path), command],
                input=raw,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=_adapter_env(),
                cwd=os.fspath(pinned_root),
                timeout=timeout_seconds,
                check=False,
            )
    except subprocess.TimeoutExpired as exc:
        raise ReviewAdapterError("review adapter command timed out") from exc
    finally:
        _assert_adapter_unchanged(path, binding)
    if len(completed.stdout) > MAX_ADAPTER_RESPONSE_BYTES:
        raise ReviewAdapterError("review adapter response exceeds its byte bound")
    if completed.returncode != 0:
        bounded_stderr = completed.stderr[:MAX_ADAPTER_STDERR_BYTES]
        diagnostic = hashlib.sha256(bounded_stderr).hexdigest()
        raise ReviewAdapterError(
            f"review adapter rejected {command}; bounded_stderr_sha256={diagnostic}; "
            f"bounded_stderr_bytes={len(bounded_stderr)}"
        )
    try:
        decoded = strict_json_loads(completed.stdout, label="review adapter response")
    except ReviewContractError as exc:
        raise ReviewAdapterError("review adapter returned invalid JSON") from exc
    if not isinstance(decoded, dict):
        raise ReviewAdapterError("review adapter response must be an object")
    return decoded


def _command(command: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": ADAPTER_COMMAND_SCHEMA,
        "command": command,
        "payload": deepcopy(dict(payload)),
    }


def _validate_decision(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    keys = {
        "route_id",
        "raw_verdict",
        "effective_verdict",
        "yellow_streak",
        "critic_confirmed_progress_ids",
        "auto_red",
        "auto_red_reason",
        "route_frozen",
        "allowed_action",
    }
    raw = _exact_object(value, keys, label="review decision")
    if raw["raw_verdict"] not in {"green", "yellow", "red"}:
        raise ReviewAdapterError("review decision raw_verdict is invalid")
    if raw["effective_verdict"] not in {"green", "yellow", "red"}:
        raise ReviewAdapterError("review decision effective_verdict is invalid")
    if type(raw["yellow_streak"]) is not int or not 0 <= raw["yellow_streak"] <= 2:
        raise ReviewAdapterError("review decision yellow_streak is invalid")
    if not isinstance(raw["critic_confirmed_progress_ids"], list) or any(
        not isinstance(item, str) or RECORD_ID_RE.fullmatch(item) is None
        for item in raw["critic_confirmed_progress_ids"]
    ):
        raise ReviewAdapterError("review decision progress ids are invalid")
    if (
        len(raw["critic_confirmed_progress_ids"]) > 32
        or len(set(raw["critic_confirmed_progress_ids"]))
        != len(raw["critic_confirmed_progress_ids"])
    ):
        raise ReviewAdapterError("review decision progress ids are invalid")
    if type(raw["auto_red"]) is not bool or type(raw["route_frozen"]) is not bool:
        raise ReviewAdapterError("review decision boolean field is invalid")
    if raw["auto_red_reason"] is not None and not isinstance(raw["auto_red_reason"], str):
        raise ReviewAdapterError("review decision auto_red_reason is invalid")
    expected_action = {
        "green": "continue_to_next_milestone",
        "yellow": "one_bounded_cycle_on_fatal_doubt",
        "red": "freeze_route",
    }[raw["effective_verdict"]]
    if raw["allowed_action"] != expected_action:
        raise ReviewAdapterError("review decision allowed_action is inconsistent")
    if raw["route_frozen"] != (raw["effective_verdict"] == "red"):
        raise ReviewAdapterError("review decision route_frozen is inconsistent")
    if raw["raw_verdict"] == "red" and raw["effective_verdict"] != "red":
        raise ReviewAdapterError("review decision illegally weakens raw red")
    if raw["effective_verdict"] == "yellow" and raw["yellow_streak"] != 1:
        raise ReviewAdapterError("review decision yellow streak is inconsistent")
    if raw["effective_verdict"] == "green" and raw["yellow_streak"] != 0:
        raise ReviewAdapterError("review decision green streak is inconsistent")
    if raw["auto_red"]:
        if raw["effective_verdict"] != "red" or not raw["auto_red_reason"]:
            raise ReviewAdapterError("review decision auto-red fields are inconsistent")
    elif raw["auto_red_reason"] is not None:
        raise ReviewAdapterError("review decision auto-red reason is inconsistent")
    return deepcopy(raw)


def _validate_execution_summary(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    keys = {"state", "report", "error", "retry_allowed", "attempt"}
    raw = _exact_object(value, keys, label="review execution summary")
    if raw["state"] not in {"completed", "operational_blocked", "execution_unknown"}:
        raise ReviewAdapterError("review execution state is invalid")
    if raw["retry_allowed"] is not False or raw["attempt"] != 1:
        raise ReviewAdapterError("review execution illegally authorizes retry")
    if raw["state"] == "completed":
        if not isinstance(raw["report"], dict) or raw["error"] is not None:
            raise ReviewAdapterError("completed review execution is malformed")
    elif raw["report"] is not None or not isinstance(raw["error"], str):
        raise ReviewAdapterError("non-completed review execution is malformed")
    return deepcopy(raw)


def _validate_publication_receipt(value: Mapping[str, Any]) -> dict[str, Any]:
    raw = _exact_object(value, _PUBLICATION_RECEIPT_KEYS, label="review publication receipt")
    if raw["schema_version"] != PUBLICATION_RECEIPT_SCHEMA:
        raise ReviewAdapterError("review publication receipt schema is invalid")
    if raw["publication_state"] not in {"pending", "official"}:
        raise ReviewAdapterError("review publication receipt state is invalid")
    for key in ("request_sha256", "snapshot_sha256", "checkpoint_sha256", "record_sha256"):
        if SHA256_RE.fullmatch(raw[key]) is None:
            raise ReviewAdapterError(f"review publication receipt {key} is invalid")
    if REVIEW_ID_RE.fullmatch(raw["review_id"]) is None:
        raise ReviewAdapterError("review publication receipt review_id is invalid")
    if not isinstance(raw["problem_id"], str) or not raw["problem_id"]:
        raise ReviewAdapterError("review publication receipt problem_id is invalid")
    if not isinstance(raw["batch_id"], str) or re.fullmatch(r"batch_[0-9a-f]{64}", raw["batch_id"]) is None:
        raise ReviewAdapterError("review publication receipt batch_id is invalid")
    if not isinstance(raw["record_id"], str) or RECORD_ID_RE.fullmatch(raw["record_id"]) is None:
        raise ReviewAdapterError("review publication receipt record_id is invalid")
    if not isinstance(raw["timestamp_utc"], str) or not raw["timestamp_utc"]:
        raise ReviewAdapterError("review publication receipt timestamp is invalid")
    return deepcopy(raw)


def _validate_route_transition_publication_receipt(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    raw = _exact_object(
        value,
        _ROUTE_TRANSITION_PUBLICATION_RECEIPT_KEYS,
        label="route transition publication receipt",
    )
    if raw["schema_version"] != ROUTE_TRANSITION_PUBLICATION_RECEIPT_SCHEMA:
        raise ReviewAdapterError("route transition publication receipt schema is invalid")
    if not isinstance(raw["problem_id"], str) or not raw["problem_id"]:
        raise ReviewAdapterError("route transition publication problem_id is invalid")
    if REVIEW_ID_RE.fullmatch(raw["review_id"]) is None:
        raise ReviewAdapterError("route transition publication review_id is invalid")
    for key in (
        "request_sha256",
        "snapshot_sha256",
        "checkpoint_sha256",
        "transition_sha256",
        "receipt_sha256",
    ):
        if not isinstance(raw[key], str) or SHA256_RE.fullmatch(raw[key]) is None:
            raise ReviewAdapterError(
                f"route transition publication {key} is invalid"
            )
    for key in ("from_route_id",):
        if (
            not isinstance(raw[key], str)
            or not raw[key]
            or len(raw[key].encode("utf-8")) > 256
        ):
            raise ReviewAdapterError(
                f"route transition publication {key} is invalid"
            )
    to_route_id = raw["to_route_id"]
    if to_route_id is not None and (
        not isinstance(to_route_id, str)
        or not to_route_id
        or len(to_route_id.encode("utf-8")) > 256
    ):
        raise ReviewAdapterError("route transition publication to_route_id is invalid")
    if raw["from_route_id"] == to_route_id:
        raise ReviewAdapterError("route transition publication does not change route")
    if (
        not isinstance(raw["batch_id"], str)
        or re.fullmatch(r"batch_[0-9a-f]{64}", raw["batch_id"]) is None
    ):
        raise ReviewAdapterError("route transition publication batch_id is invalid")
    record_ids = raw["record_ids"]
    if (
        not isinstance(record_ids, list)
        or len(record_ids) != (1 if to_route_id is None else 2)
        or len(set(record_ids)) != len(record_ids)
        or any(
            not isinstance(record_id, str)
            or RECORD_ID_RE.fullmatch(record_id) is None
            for record_id in record_ids
        )
    ):
        raise ReviewAdapterError("route transition publication record ids are invalid")
    _canonical_adapter_utc(
        raw["timestamp_utc"], label="route transition publication timestamp"
    )
    seed = deepcopy(raw)
    receipt_sha = seed.pop("receipt_sha256")
    if hashlib.sha256(canonical_json_bytes(seed)).hexdigest() != receipt_sha:
        raise ReviewAdapterError("route transition publication content address mismatch")
    return deepcopy(raw)


def _validate_targeted_receipt(value: Mapping[str, Any]) -> dict[str, Any]:
    raw = _exact_object(value, _TARGETED_RECEIPT_KEYS, label="targeted verifier receipt")
    if raw["schema_version"] != TARGETED_RECEIPT_SCHEMA:
        raise ReviewAdapterError("targeted verifier receipt schema is invalid")
    for key in (
        "snapshot_sha256",
        "blueprint_sha256",
        "claim_sha256",
        "receipt_sha256",
    ):
        if not isinstance(raw[key], str) or SHA256_RE.fullmatch(raw[key]) is None:
            raise ReviewAdapterError(f"targeted verifier receipt {key} is invalid")
    if REVIEW_ID_RE.fullmatch(raw["review_id"]) is None:
        raise ReviewAdapterError("targeted verifier receipt review_id is invalid")
    if not isinstance(raw["ticket_id"], str) or re.fullmatch(r"claim_[0-9a-f]{32}", raw["ticket_id"]) is None:
        raise ReviewAdapterError("targeted verifier receipt ticket_id is invalid")
    if not isinstance(raw["blueprint_item_id"], str) or re.fullmatch(r"pi_[0-9a-f]{24}", raw["blueprint_item_id"]) is None:
        raise ReviewAdapterError("targeted verifier receipt item id is invalid")
    if raw["verification_status"] != "final" or raw["verdict"] not in {"correct", "wrong"}:
        raise ReviewAdapterError("targeted verifier receipt verdict is invalid")
    _canonical_adapter_utc(
        raw["verification_deadline_utc"],
        label="targeted verifier receipt deadline",
    )
    if (
        raw["publication_authority"] is not False
        or raw["whole_blueprint_verdict_authority"] is not False
    ):
        raise ReviewAdapterError("targeted verifier receipt has forbidden authority")
    seed = deepcopy(raw)
    receipt_sha = seed.pop("receipt_sha256")
    if hashlib.sha256(canonical_json_bytes(seed)).hexdigest() != receipt_sha:
        raise ReviewAdapterError("targeted verifier receipt content address mismatch")
    return deepcopy(raw)


def _validate_targeted_publication_receipt(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    raw = _exact_object(
        value,
        _TARGETED_PUBLICATION_RECEIPT_KEYS,
        label="targeted verification publication receipt",
    )
    if raw["schema_version"] != TARGETED_PUBLICATION_RECEIPT_SCHEMA:
        raise ReviewAdapterError("targeted publication receipt schema is invalid")
    if raw["publication_state"] != "pending":
        raise ReviewAdapterError("targeted publication receipt must be pending")
    for key in (
        "request_sha256",
        "snapshot_sha256",
        "verifier_receipt_sha256",
        "checkpoint_sha256",
        "record_sha256",
    ):
        if not isinstance(raw[key], str) or SHA256_RE.fullmatch(raw[key]) is None:
            raise ReviewAdapterError(f"targeted publication receipt {key} is invalid")
    if REVIEW_ID_RE.fullmatch(raw["review_id"]) is None:
        raise ReviewAdapterError("targeted publication receipt review_id is invalid")
    if not isinstance(raw["ticket_id"], str) or re.fullmatch(r"claim_[0-9a-f]{32}", raw["ticket_id"]) is None:
        raise ReviewAdapterError("targeted publication receipt ticket_id is invalid")
    if not isinstance(raw["problem_id"], str) or not raw["problem_id"]:
        raise ReviewAdapterError("targeted publication receipt problem_id is invalid")
    if not isinstance(raw["batch_id"], str) or re.fullmatch(r"batch_[0-9a-f]{64}", raw["batch_id"]) is None:
        raise ReviewAdapterError("targeted publication receipt batch_id is invalid")
    if not isinstance(raw["record_id"], str) or RECORD_ID_RE.fullmatch(raw["record_id"]) is None:
        raise ReviewAdapterError("targeted publication receipt record_id is invalid")
    if not isinstance(raw["timestamp_utc"], str) or not raw["timestamp_utc"]:
        raise ReviewAdapterError("targeted publication receipt timestamp is invalid")
    return deepcopy(raw)


def _validate_review_response(
    response: Mapping[str, Any],
    *,
    operation: str,
    review_id: str,
    request_sha256: str,
    expected_snapshot_sha256: str,
) -> dict[str, Any]:
    keys = {
        "schema_version",
        "operation",
        "review_id",
        "request_sha256",
        "snapshot_sha256",
        "state",
        "idempotent",
        "execution",
        "decision",
    }
    raw = _exact_object(response, keys, label="review adapter response")
    if raw["schema_version"] != ADAPTER_RESPONSE_SCHEMA or raw["operation"] != operation:
        raise ReviewAdapterError("review adapter response operation binding mismatch")
    if (
        raw["review_id"] != review_id
        or raw["request_sha256"] != request_sha256
        or raw["snapshot_sha256"] != expected_snapshot_sha256
    ):
        raise ReviewAdapterError("review adapter response content binding mismatch")
    if raw["state"] not in REVIEW_STATES or type(raw["idempotent"]) is not bool:
        raise ReviewAdapterError("review adapter response state is invalid")
    execution = _validate_execution_summary(raw["execution"])
    decision = _validate_decision(raw["decision"])
    if raw["state"] in {
        "completed",
        "completed_pending_close",
        "completed_pending_publication",
        "verification_required",
        "verification_prepared",
        "verification_pending_publication",
    } and (
        execution is None or execution["state"] != "completed" or decision is None
    ):
        raise ReviewAdapterError("completed review response lacks execution/decision")
    if raw["state"] in {"operational_blocked", "execution_unknown"}:
        if execution is None or execution["state"] != raw["state"] or decision is not None:
            raise ReviewAdapterError("failed review response is inconsistent")
    if raw["state"] == "verification_unknown":
        if execution is None or execution["state"] != "execution_unknown" or decision is not None:
            raise ReviewAdapterError("unknown targeted verification response is inconsistent")
    if raw["state"] in {"prepared", "running"} and (execution is not None or decision is not None):
        raise ReviewAdapterError("unfinished review response contains a result")
    if raw["state"] == "closed":
        if execution is None:
            raise ReviewAdapterError("closed review response lacks terminal execution")
        if (execution["state"] == "completed") != (decision is not None):
            raise ReviewAdapterError("closed review response has an inconsistent decision")
    return {**deepcopy(raw), "execution": execution, "decision": decision}


def route_review_prepare(
    *,
    request: Mapping[str, Any],
) -> dict[str, Any]:
    """Prepare exactly the review cycle announced by the trusted scheduler."""

    normalized_request = validate_review_request(request)
    response = _invoke_adapter(
        "review-prepare",
        _command("review_prepare", {"request": normalized_request}),
        timeout_seconds=30,
    )
    return _validate_review_response(
        response,
        operation="review_prepare",
        review_id=normalized_request["review_id"],
        request_sha256=normalized_request["request_sha256"],
        expected_snapshot_sha256=normalized_request["snapshot_sha256"],
    )


def _bound_review_operation(
    *,
    command: str,
    operation: str,
    review_id: str,
    request_sha256: str,
    expected_snapshot_sha256: str,
) -> dict[str, Any]:
    if REVIEW_ID_RE.fullmatch(review_id) is None:
        raise ReviewAdapterError("review_id is invalid")
    if SHA256_RE.fullmatch(request_sha256) is None:
        raise ReviewAdapterError("request_sha256 is invalid")
    if SHA256_RE.fullmatch(expected_snapshot_sha256) is None:
        raise ReviewAdapterError("snapshot_sha256 is invalid")
    response = _invoke_adapter(
        command,
        _command(
            operation,
            {
                "review_id": review_id,
                "request_sha256": request_sha256,
                "snapshot_sha256": expected_snapshot_sha256,
            },
        ),
        timeout_seconds=(330 if command == "review-wait" else 30),
    )
    return _validate_review_response(
        response,
        operation=operation,
        review_id=review_id,
        request_sha256=request_sha256,
        expected_snapshot_sha256=expected_snapshot_sha256,
    )


def route_review_wait(
    *,
    review_id: str,
    request_sha256: str,
    snapshot_sha256: str,
) -> dict[str, Any]:
    return _bound_review_operation(
        command="review-wait",
        operation="review_wait",
        review_id=review_id,
        request_sha256=request_sha256,
        expected_snapshot_sha256=snapshot_sha256,
    )


def route_review_status(
    *, review_id: str, request_sha256: str, snapshot_sha256: str
) -> dict[str, Any]:
    return _bound_review_operation(
        command="review-status",
        operation="review_status",
        review_id=review_id,
        request_sha256=request_sha256,
        expected_snapshot_sha256=snapshot_sha256,
    )


def _canonical_adapter_utc(value: Any, *, label: str) -> str:
    if not isinstance(value, str):
        raise ReviewAdapterError(f"{label} is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ReviewAdapterError(f"{label} is invalid") from exc
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() != timedelta(0)
        or value != parsed.astimezone(timezone.utc).isoformat()
    ):
        raise ReviewAdapterError(f"{label} is not canonical UTC")
    return value


def _valid_memory_batch_problem_id(value: object) -> bool:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > 128
        or value != value.strip()
        or "\\" in value
    ):
        return False
    parts = value.split("/")
    return bool(
        parts
        and all(
            part not in {"", ".", ".."}
            and _MEMORY_BATCH_PROBLEM_COMPONENT_RE.fullmatch(part) is not None
            for part in parts
        )
    )


def review_due_status(
    *,
    cycle_id: str,
    cycle: str,
    review_ordinal: int,
) -> dict[str, Any]:
    """Resolve one exact review boundary from authenticated host state."""

    if cycle not in {"minute30", "minute60"} or review_ordinal != {
        "minute30": 1,
        "minute60": 2,
    }[cycle]:
        raise ReviewAdapterError("review due status cadence binding is invalid")
    assertions = {"cycle_id": cycle_id}
    if any(
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > 256
        for value in assertions.values()
    ):
        raise ReviewAdapterError("review due status id binding is invalid")
    response = _invoke_adapter(
        "review-status",
        _command(
            "review_status",
            {
                "operation": "review_due_status",
                "cycle_id": cycle_id,
                "cycle": cycle,
                "review_ordinal": review_ordinal,
            },
        ),
        timeout_seconds=30,
    )
    raw = _exact_object(
        response,
        {
            "schema_version",
            "operation",
            "review_id",
            "cycle_id",
            "cycle",
            "review_ordinal",
            "due_at_utc",
            "state",
            "active_route_id",
            "root_thread_id",
            "root_turn_id",
            "root_terminal_sha256",
        },
        label="review due status response",
    )
    if (
        raw["schema_version"] != ADAPTER_RESPONSE_SCHEMA
        or raw["operation"] != "review_due_status"
        or raw["cycle_id"] != cycle_id
        or raw["cycle"] != cycle
        or raw["review_ordinal"] != review_ordinal
    ):
        raise ReviewAdapterError("review due status response binding mismatch")
    if REVIEW_ID_RE.fullmatch(raw["review_id"]) is None:
        raise ReviewAdapterError("review due status host review_id is invalid")
    if raw["state"] != "completed":
        raise ReviewAdapterError("review boundary is not durably delivered")
    active_route_id = raw["active_route_id"]
    if (
        not isinstance(active_route_id, str)
        or not active_route_id
        or len(active_route_id.encode("utf-8")) > 256
    ):
        raise ReviewAdapterError("review due status active route is invalid")
    for key in ("root_thread_id", "root_turn_id"):
        value = raw[key]
        if (
            not isinstance(value, str)
            or not value
            or len(value.encode("utf-8")) > 256
        ):
            raise ReviewAdapterError(f"review due status {key} is invalid")
    if (
        not isinstance(raw["root_terminal_sha256"], str)
        or SHA256_RE.fullmatch(raw["root_terminal_sha256"]) is None
    ):
        raise ReviewAdapterError("review due status terminal digest is invalid")
    due_at_utc = _canonical_adapter_utc(
        raw["due_at_utc"], label="review due status due_at_utc"
    )
    return {**deepcopy(raw), "due_at_utc": due_at_utc}


_REVIEW_ONLY_TOOLS = frozenset(
    {
        "route_review_prepare",
        "review_frontier_status",
        "route_review_wait",
        "route_review_status",
        "route_review_close",
        "verify_review_claim",
        "context_handoff_prepare",
        "context_handoff_get",
        "context_handoff_status",
        "route_cycle_close",
    }
)


def reasoning_phase_preflight(*, tool_name: str) -> dict[str, Any]:
    """Ask authenticated cadence state whether this exact MCP tool is legal."""

    if (
        not isinstance(tool_name, str)
        or not tool_name
        or len(tool_name.encode("utf-8")) > 256
    ):
        raise ReviewAdapterError("reasoning phase preflight tool name is invalid")
    response = _invoke_adapter(
        "review-status",
        _command(
            "review_status",
            {"operation": "reasoning_phase_preflight", "tool_name": tool_name},
        ),
        timeout_seconds=30,
    )
    raw = _exact_object(
        response,
        {
            "schema_version",
            "operation",
            "run_id",
            "problem_id",
            "phase",
            "allowed_action",
            "active_review_id",
            "review_due_at_utc",
            "review_due_monotonic",
            "hard_stop_at_utc",
            "hard_stop_monotonic",
            "tool_permitted",
        },
        label="reasoning phase preflight response",
    )
    if (
        raw["schema_version"] != ADAPTER_RESPONSE_SCHEMA
        or raw["operation"] != "reasoning_phase_preflight"
        or type(raw["tool_permitted"]) is not bool
    ):
        raise ReviewAdapterError("reasoning phase preflight response is invalid")
    for key in ("run_id", "problem_id", "phase", "allowed_action"):
        value = raw[key]
        if (
            not isinstance(value, str)
            or not value
            or len(value.encode("utf-8")) > 256
        ):
            raise ReviewAdapterError(f"reasoning phase preflight {key} is invalid")
    active_review_id = raw["active_review_id"]
    if active_review_id is not None and (
        not isinstance(active_review_id, str)
        or REVIEW_ID_RE.fullmatch(active_review_id) is None
    ):
        raise ReviewAdapterError("reasoning phase preflight review id is invalid")
    review_due = raw["review_due_at_utc"]
    review_due_monotonic = raw["review_due_monotonic"]
    if review_due is not None:
        _canonical_adapter_utc(
            review_due, label="reasoning phase preflight review due time"
        )
        if (
            isinstance(review_due_monotonic, bool)
            or not isinstance(review_due_monotonic, (int, float))
            or not math.isfinite(float(review_due_monotonic))
            or float(review_due_monotonic) <= 0
        ):
            raise ReviewAdapterError(
                "reasoning phase preflight review monotonic due time is invalid"
            )
    elif review_due_monotonic is not None:
        raise ReviewAdapterError(
            "reasoning phase preflight review monotonic due time lacks wall time"
        )
    _canonical_adapter_utc(
        raw["hard_stop_at_utc"],
        label="reasoning phase preflight hard-stop time",
    )
    hard_stop_monotonic = raw["hard_stop_monotonic"]
    if (
        isinstance(hard_stop_monotonic, bool)
        or not isinstance(hard_stop_monotonic, (int, float))
        or not math.isfinite(float(hard_stop_monotonic))
        or float(hard_stop_monotonic) <= 0
    ):
        raise ReviewAdapterError(
            "reasoning phase preflight hard-stop monotonic time is invalid"
        )
    if raw["allowed_action"] == "independent_review_only":
        expected = tool_name in _REVIEW_ONLY_TOOLS
        if raw["tool_permitted"] != expected:
            raise ReviewAdapterError(
                "host phase permission conflicts with the review-only allowlist"
            )
    return deepcopy(raw)


def _validate_memory_batch_publication_receipt(value: object) -> dict[str, Any]:
    raw = _exact_object(
        value,
        _MEMORY_BATCH_PUBLICATION_RECEIPT_KEYS,
        label="memory batch publication receipt",
    )
    if (
        raw["schema_version"] != MEMORY_BATCH_PUBLICATION_RECEIPT_SCHEMA
        or raw["state"] not in {"accepted", "rejected"}
        or not isinstance(raw["run_id"], str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", raw["run_id"])
        is None
        or not _valid_memory_batch_problem_id(raw["problem_id"])
        or re.fullmatch(r"batch_[0-9a-f]{64}", str(raw["batch_id"])) is None
        or SHA256_RE.fullmatch(str(raw["checkpoint_sha256"])) is None
        or SHA256_RE.fullmatch(str(raw["commit_sha256"])) is None
        or raw["publication_class"]
        not in {"reasoning_checkpoint", "control_only"}
        or re.fullmatch(r"cycle_[0-9a-f]{32}", str(raw["cycle_id"])) is None
        or re.fullmatch(r"cadact_[0-9a-f]{32}", str(raw["cutoff_action_id"])) is None
        or raw["cutoff_kind"] not in {"review_1", "review_2", "hard_stop"}
        or not isinstance(raw["boot_identity"], str)
        or _MEMORY_BATCH_BOOT_IDENTITY_RE.fullmatch(raw["boot_identity"]) is None
    ):
        raise ReviewAdapterError("memory batch publication receipt is invalid")
    _canonical_adapter_utc(
        raw["cutoff_at_utc"], label="memory batch publication cutoff"
    )
    if (
        type(raw["cutoff_monotonic"]) is not float
        or not math.isfinite(float(raw["cutoff_monotonic"]))
        or float(raw["cutoff_monotonic"]) <= 0
    ):
        raise ReviewAdapterError(
            "memory batch publication cutoff monotonic time is invalid"
        )
    accepted_at_utc = raw["accepted_at_utc"]
    accepted_at_monotonic = raw["accepted_at_monotonic"]
    if raw["state"] == "accepted":
        accepted_text = _canonical_adapter_utc(
            accepted_at_utc, label="memory batch publication acceptance"
        )
        if (
            type(accepted_at_monotonic) is not float
            or not math.isfinite(float(accepted_at_monotonic))
            or float(accepted_at_monotonic) <= 0
        ):
            raise ReviewAdapterError(
                "memory batch publication acceptance monotonic time is invalid"
            )
        if (
            datetime.fromisoformat(accepted_text)
            >= datetime.fromisoformat(raw["cutoff_at_utc"])
            or float(accepted_at_monotonic) >= float(raw["cutoff_monotonic"])
        ):
            raise ReviewAdapterError(
                "memory batch publication acceptance crossed its cutoff"
            )
    elif accepted_at_utc is not None or accepted_at_monotonic is not None:
        raise ReviewAdapterError(
            "rejected memory batch publication has an acceptance time"
        )
    seed = {key: item for key, item in raw.items() if key != "receipt_sha256"}
    if (
        not isinstance(raw["receipt_sha256"], str)
        or SHA256_RE.fullmatch(raw["receipt_sha256"]) is None
        or hashlib.sha256(canonical_json_bytes(seed)).hexdigest()
        != raw["receipt_sha256"]
    ):
        raise ReviewAdapterError("memory batch publication receipt digest is invalid")
    return deepcopy(raw)


def memory_batch_publication_commit(
    *,
    problem_id: str,
    batch_id: str,
    checkpoint_sha256: str,
    commit_sha256: str,
    publication_class: str,
) -> dict[str, Any]:
    if not _valid_memory_batch_problem_id(problem_id):
        raise ReviewAdapterError("memory batch publication problem id is invalid")
    response = _invoke_adapter(
        "review-status",
        _command(
            "review_status",
            {
                "operation": "memory_batch_publication_commit",
                "problem_id": problem_id,
                "batch_id": batch_id,
                "checkpoint_sha256": checkpoint_sha256,
                "commit_sha256": commit_sha256,
                "publication_class": publication_class,
            },
        ),
        timeout_seconds=30,
    )
    receipt = _validate_memory_batch_publication_receipt(response)
    expected_run = os.environ.get(EXPECTED_RUN_ENV, "")
    if not expected_run:
        raise ReviewAdapterError("runner did not bind an expected hot-join run id")
    expected = {
        "run_id": expected_run,
        "problem_id": problem_id,
        "batch_id": batch_id,
        "checkpoint_sha256": checkpoint_sha256,
        "commit_sha256": commit_sha256,
        "publication_class": publication_class,
    }
    if any(receipt[key] != value for key, value in expected.items()):
        raise ReviewAdapterError(
            "memory batch publication receipt does not bind its request"
        )
    return receipt


def memory_batch_publication_status(*, problem_id: str) -> dict[str, Any]:
    if not _valid_memory_batch_problem_id(problem_id):
        raise ReviewAdapterError("memory batch publication problem id is invalid")
    response = _invoke_adapter(
        "review-status",
        _command(
            "review_status",
            {
                "operation": "memory_batch_publication_status",
                "problem_id": problem_id,
            },
        ),
        timeout_seconds=30,
    )
    expected_run = os.environ.get(EXPECTED_RUN_ENV, "")
    if not expected_run:
        raise ReviewAdapterError("runner did not bind an expected hot-join run id")
    return _validate_memory_batch_publication_status(
        response,
        expected_run_id=expected_run,
        expected_problem_id=problem_id,
    )


def _validate_memory_batch_publication_status(
    value: object,
    *,
    expected_run_id: str,
    expected_problem_id: str,
) -> dict[str, Any]:
    """Purely validate one already-decoded host publication manifest."""

    raw = _exact_object(
        value,
        {"schema_version", "run_id", "problem_id", "receipts"},
        label="memory batch publication status",
    )
    receipts = raw["receipts"]
    if (
        raw["schema_version"] != MEMORY_BATCH_PUBLICATION_STATUS_SCHEMA
        or not isinstance(expected_run_id, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", expected_run_id)
        is None
        or not _valid_memory_batch_problem_id(expected_problem_id)
        or raw["run_id"] != expected_run_id
        or not isinstance(raw["run_id"], str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", raw["run_id"])
        is None
        or raw["problem_id"] != expected_problem_id
        or not isinstance(receipts, list)
        or len(receipts) > MAX_ACCEPTED_MEMORY_BATCH_PUBLICATIONS
    ):
        raise ReviewAdapterError("memory batch publication status is invalid")
    normalized = [_validate_memory_batch_publication_receipt(item) for item in receipts]
    if any(
        item["state"] != "accepted"
        or item["run_id"] != raw["run_id"]
        or item["problem_id"] != raw["problem_id"]
        for item in normalized
    ):
        raise ReviewAdapterError(
            "memory batch publication manifest has cross-bound receipts"
        )
    if [item["batch_id"] for item in normalized] != sorted(
        item["batch_id"] for item in normalized
    ) or len({item["batch_id"] for item in normalized}) != len(normalized):
        raise ReviewAdapterError("memory batch publication manifest is not exact")
    return {**deepcopy(raw), "receipts": normalized}


def validate_memory_batch_publication_status_snapshot(
    snapshot_json: str,
    *,
    expected_run_id: str,
    expected_problem_id: str,
) -> dict[str, Any]:
    """Validate a canonical, bounded owner-authenticated read-only snapshot.

    This helper is deliberately pure: it reads no environment, database,
    adapter, or capability.  The owner wrapper authenticates the adapter call;
    this function only validates the exact value transported to the dedicated
    generation-control receipt CLI.
    """

    if type(snapshot_json) is not str:
        raise ReviewAdapterError("memory batch publication snapshot is not text")
    try:
        encoded = snapshot_json.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ReviewAdapterError(
            "memory batch publication snapshot is not UTF-8"
        ) from exc
    if not encoded or len(encoded) > MAX_ADAPTER_RESPONSE_BYTES:
        raise ReviewAdapterError(
            "memory batch publication snapshot exceeds its byte bound"
        )
    try:
        decoded = strict_json_loads(
            encoded, label="memory batch publication snapshot"
        )
        canonical = canonical_json_bytes(decoded)
    except ReviewContractError as exc:
        raise ReviewAdapterError(
            f"memory batch publication snapshot is invalid: {exc}"
        ) from exc
    if canonical != encoded:
        raise ReviewAdapterError(
            "memory batch publication snapshot is not canonical JSON"
        )
    return _validate_memory_batch_publication_status(
        decoded,
        expected_run_id=expected_run_id,
        expected_problem_id=expected_problem_id,
    )


def route_review_targeted_verification_prepare(
    *,
    review_id: str,
    request_sha256: str,
    snapshot_sha256: str,
    ticket: Mapping[str, Any],
) -> dict[str, Any]:
    normalized_ticket = validate_targeted_verification_ticket(ticket)
    if (
        normalized_ticket["review_id"] != review_id
        or normalized_ticket["snapshot_sha256"] != snapshot_sha256
    ):
        raise ReviewAdapterError("targeted verification ticket binding mismatch")
    response = _invoke_adapter(
        "review-status",
        _command(
            "review_status",
            {
                "operation": "targeted_verification_prepare",
                "review_id": review_id,
                "request_sha256": request_sha256,
                "snapshot_sha256": snapshot_sha256,
                "ticket": normalized_ticket,
            },
        ),
        timeout_seconds=30,
    )
    response_keys = {
        "schema_version",
        "operation",
        "review_id",
        "request_sha256",
        "snapshot_sha256",
        "state",
        "idempotent",
        "execution",
        "decision",
        "verification_deadline_utc",
    }
    raw = _exact_object(
        response, response_keys, label="targeted verification admission response"
    )
    deadline_raw = raw["verification_deadline_utc"]
    if not isinstance(deadline_raw, str):
        raise ReviewAdapterError("targeted verification deadline is invalid")
    try:
        deadline = datetime.fromisoformat(deadline_raw)
    except ValueError as exc:
        raise ReviewAdapterError("targeted verification deadline is invalid") from exc
    if (
        deadline.tzinfo is None
        or deadline.utcoffset() != timedelta(0)
        or deadline_raw != deadline.astimezone(timezone.utc).isoformat()
    ):
        raise ReviewAdapterError("targeted verification deadline is not canonical UTC")
    base = dict(raw)
    base.pop("verification_deadline_utc")
    validated = _validate_review_response(
        base,
        operation="targeted_verification_prepare",
        review_id=review_id,
        request_sha256=request_sha256,
        expected_snapshot_sha256=snapshot_sha256,
    )
    return {**validated, "verification_deadline_utc": deadline_raw}


def route_review_targeted_verification_commit(
    *,
    review_id: str,
    request_sha256: str,
    snapshot_sha256: str,
    outcome_state: str,
    verification_receipt: Mapping[str, Any] | None,
    error_sha256: str | None,
    publication_receipt: Mapping[str, Any],
    route_transition_publication_receipt: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if outcome_state == "completed":
        if verification_receipt is None or error_sha256 is not None:
            raise ReviewAdapterError("completed targeted verification needs one receipt")
        receipt = _validate_targeted_receipt(verification_receipt)
        if (
            receipt["review_id"] != review_id
            or receipt["snapshot_sha256"] != snapshot_sha256
        ):
            raise ReviewAdapterError("targeted verifier receipt binding mismatch")
    elif outcome_state in {"operational_blocked", "execution_unknown"}:
        if verification_receipt is not None or not isinstance(error_sha256, str) or SHA256_RE.fullmatch(error_sha256) is None:
            raise ReviewAdapterError("failed targeted verification needs one error digest")
        receipt = None
    else:
        raise ReviewAdapterError("targeted verification outcome state is invalid")
    if publication_receipt.get("schema_version") == TARGETED_PUBLICATION_RECEIPT_SCHEMA:
        publication = _validate_targeted_publication_receipt(publication_receipt)
    elif publication_receipt.get("schema_version") == PUBLICATION_RECEIPT_SCHEMA:
        publication = _validate_publication_receipt(publication_receipt)
        if publication["publication_state"] != "official":
            raise ReviewAdapterError("targeted final publication receipt must be official")
    else:
        raise ReviewAdapterError("targeted publication receipt schema is unsupported")
    if (
        publication["review_id"] != review_id
        or publication["request_sha256"] != request_sha256
        or publication["snapshot_sha256"] != snapshot_sha256
    ):
        raise ReviewAdapterError("targeted publication receipt binding mismatch")
    if publication["schema_version"] == TARGETED_PUBLICATION_RECEIPT_SCHEMA:
        expected_result_sha = (
            receipt["receipt_sha256"] if receipt is not None else error_sha256
        )
        if publication["verifier_receipt_sha256"] != expected_result_sha:
            raise ReviewAdapterError("targeted result/publication digest mismatch")
    transition_receipt = (
        None
        if route_transition_publication_receipt is None
        else _validate_route_transition_publication_receipt(
            route_transition_publication_receipt
        )
    )
    completed_wrong = receipt is not None and receipt["verdict"] == "wrong"
    is_official = publication["schema_version"] == PUBLICATION_RECEIPT_SCHEMA
    if publication["schema_version"] == TARGETED_PUBLICATION_RECEIPT_SCHEMA:
        if transition_receipt is not None:
            raise ReviewAdapterError(
                "pending targeted ACK cannot precede route transition publication"
            )
    elif completed_wrong != (transition_receipt is not None):
        raise ReviewAdapterError(
            "official targeted wrong result requires exactly one route transition"
        )
    if transition_receipt is not None and (
        not is_official
        or transition_receipt["review_id"] != review_id
        or transition_receipt["request_sha256"] != request_sha256
        or transition_receipt["snapshot_sha256"] != snapshot_sha256
        or transition_receipt["from_route_id"] != receipt["route_id"]
    ):
        raise ReviewAdapterError("targeted route transition publication binding mismatch")
    response = _invoke_adapter(
        "review-status",
        _command(
            "review_status",
            {
                "operation": "targeted_verification_commit",
                "review_id": review_id,
                "request_sha256": request_sha256,
                "snapshot_sha256": snapshot_sha256,
                "outcome": {
                    "state": outcome_state,
                    "verification_receipt": receipt,
                    "error_sha256": error_sha256,
                },
                "publication_receipt": publication,
                "route_transition_publication_receipt": transition_receipt,
            },
        ),
        timeout_seconds=30,
    )
    return _validate_review_response(
        response,
        operation="targeted_verification_commit",
        review_id=review_id,
        request_sha256=request_sha256,
        expected_snapshot_sha256=snapshot_sha256,
    )


def route_review_close(
    *,
    review_id: str,
    request_sha256: str,
    snapshot_sha256: str,
    publication_receipt: Mapping[str, Any],
    next_route_id: str | None,
    fallback_evidence_record_ids: list[str],
    route_transition_publication_receipt: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if REVIEW_ID_RE.fullmatch(review_id) is None:
        raise ReviewAdapterError("review_id is invalid")
    if SHA256_RE.fullmatch(request_sha256) is None or SHA256_RE.fullmatch(snapshot_sha256) is None:
        raise ReviewAdapterError("review request binding is invalid")
    receipt = _validate_publication_receipt(publication_receipt)
    if (
        receipt["review_id"] != review_id
        or receipt["request_sha256"] != request_sha256
        or receipt["snapshot_sha256"] != snapshot_sha256
    ):
        raise ReviewAdapterError("review publication receipt binding mismatch")
    if next_route_id is not None and (
        not isinstance(next_route_id, str)
        or not next_route_id
        or len(next_route_id.encode("utf-8")) > 256
    ):
        raise ReviewAdapterError("review next route id is invalid")
    if (
        not isinstance(fallback_evidence_record_ids, list)
        or len(fallback_evidence_record_ids) > 32
        or any(
            not isinstance(item, str) or RECORD_ID_RE.fullmatch(item) is None
            for item in fallback_evidence_record_ids
        )
        or len(set(fallback_evidence_record_ids)) != len(fallback_evidence_record_ids)
    ):
        raise ReviewAdapterError("review fallback evidence ids are invalid")
    transition_receipt = (
        None
        if route_transition_publication_receipt is None
        else _validate_route_transition_publication_receipt(
            route_transition_publication_receipt
        )
    )
    if receipt["publication_state"] == "pending" and transition_receipt is not None:
        raise ReviewAdapterError(
            "pending review ACK cannot precede route transition publication"
        )
    if next_route_id is None:
        if fallback_evidence_record_ids:
            raise ReviewAdapterError(
                "review without a fallback cannot cite fallback evidence"
            )
    else:
        if not fallback_evidence_record_ids:
            raise ReviewAdapterError(
                "review fallback requires durable evidence record ids"
            )
        if receipt["publication_state"] == "official" and transition_receipt is None:
            raise ReviewAdapterError(
                "official fallback review requires its transition publication"
            )
    if transition_receipt is not None and (
        transition_receipt["review_id"] != review_id
        or transition_receipt["request_sha256"] != request_sha256
        or transition_receipt["snapshot_sha256"] != snapshot_sha256
        or transition_receipt["to_route_id"] != next_route_id
    ):
        raise ReviewAdapterError("route transition publication binding mismatch")
    response = _invoke_adapter(
        "review-close",
        _command(
            "review_close",
            {
                "review_id": review_id,
                "request_sha256": request_sha256,
                "snapshot_sha256": snapshot_sha256,
                "publication_receipt": receipt,
                "route_transition": {
                    "next_route_id": next_route_id,
                    "fallback_evidence_record_ids": list(
                        fallback_evidence_record_ids
                    ),
                    "publication_receipt": transition_receipt,
                },
            },
        ),
        timeout_seconds=30,
    )
    return _validate_review_response(
        response,
        operation="review_close",
        review_id=review_id,
        request_sha256=request_sha256,
        expected_snapshot_sha256=snapshot_sha256,
    )


def _validate_handoff_response(
    response: Mapping[str, Any],
    *,
    operation: str,
    expected_handoff_id: str,
    expected_content_sha256: str,
    require_content: bool,
) -> dict[str, Any]:
    keys = {
        "schema_version",
        "operation",
        "handoff_id",
        "content_sha256",
        "state",
        "idempotent",
        "content",
        "binding",
    }
    raw = _exact_object(response, keys, label="context handoff response")
    if raw["schema_version"] != ADAPTER_RESPONSE_SCHEMA or raw["operation"] != operation:
        raise ReviewAdapterError("context handoff operation binding mismatch")
    if raw["handoff_id"] != expected_handoff_id or raw["content_sha256"] != expected_content_sha256:
        raise ReviewAdapterError("context handoff content binding mismatch")
    if raw["state"] not in HANDOFF_STATES or type(raw["idempotent"]) is not bool:
        raise ReviewAdapterError("context handoff state is invalid")
    binding_raw = raw["binding"]
    binding = None
    if binding_raw is not None:
        binding = _exact_object(
            binding_raw,
            {
                "run_id",
                "cycle_id",
                "thread_epoch",
                "root_thread_id",
                "root_turn_id",
                "rehydration_state",
            },
            label="context handoff binding",
        )
        if binding["rehydration_state"] not in {"awaiting_rehydrate", "consumed"}:
            raise ReviewAdapterError("context handoff rehydration state is invalid")
        if any(
            not isinstance(binding[key], str)
            or not binding[key]
            or len(binding[key].encode("utf-8")) > 256
            for key in (
                "run_id",
                "cycle_id",
                "thread_epoch",
                "root_thread_id",
                "root_turn_id",
            )
        ):
            raise ReviewAdapterError("context handoff binding id is invalid")
    if operation == "context_handoff_prepare":
        pass
    elif binding is None:
        raise ReviewAdapterError("context handoff response lacks its epoch binding")
    elif operation in {"context_handoff_get", "context_handoff_preflight"} and (
        binding["rehydration_state"] != "consumed"
    ):
        raise ReviewAdapterError("context handoff operation was not consumed")
    if require_content:
        content = validate_context_handoff(raw["content"])
        if handoff_sha256(content) != expected_content_sha256:
            raise ReviewAdapterError("context handoff body digest mismatch")
    elif raw["content"] is not None:
        raise ReviewAdapterError("context handoff status unexpectedly disclosed content")
    return {**deepcopy(raw), "binding": None if binding is None else deepcopy(binding)}


def context_handoff_prepare(
    *, purpose: str, proposal: Mapping[str, Any], assertions: Mapping[str, Any]
) -> dict[str, Any]:
    proposal_keys = {"active_route", "new_record_ids", "obligations", "next_action"}
    assertion_keys = {
        "run_id",
        "problem_id",
        "statement_sha256",
        "blueprint_sha256",
        "last_review",
        "yellow_streak",
        "route_frozen",
    }
    if purpose not in HANDOFF_PURPOSES:
        raise ReviewAdapterError("context handoff purpose is invalid")
    normalized_proposal = _exact_object(
        dict(proposal), proposal_keys, label="context handoff proposal"
    )
    normalized_assertions = _exact_object(
        dict(assertions), assertion_keys, label="context handoff assertions"
    )
    if len(canonical_json_bytes({
        "purpose": purpose,
        "proposal": normalized_proposal,
        "assertions": normalized_assertions,
    })) > 32_768:
        raise ReviewAdapterError("context handoff proposal exceeds its byte bound")
    response = _invoke_adapter(
        "context-handoff-prepare",
        _command(
            "context_handoff_prepare",
            {
                "operation": "context_handoff_prepare",
                "purpose": purpose,
                "proposal": deepcopy(normalized_proposal),
                "assertions": deepcopy(normalized_assertions),
            },
        ),
        timeout_seconds=30,
    )
    if not isinstance(response.get("content"), dict):
        raise ReviewAdapterError("context handoff prepare omitted authoritative content")
    content = validate_context_handoff(response["content"])
    if content["purpose"] != purpose:
        raise ReviewAdapterError("host changed the context handoff purpose")
    expected_id = handoff_id(content)
    expected_sha = handoff_sha256(content)
    return _validate_handoff_response(
        response,
        operation="context_handoff_prepare",
        expected_handoff_id=expected_id,
        expected_content_sha256=expected_sha,
        require_content=True,
    )


def generation_yield_prepare(
    *,
    state: str,
    reason_sha256: str,
    evidence_record_ids: list[str],
) -> dict[str, Any]:
    """Obtain a durable owner-yield admission before local wait publication."""

    if state not in {"waiting_cost_gate", "waiting_owner_advisor_decision"}:
        raise ReviewAdapterError("generation yield state is invalid")
    if SHA256_RE.fullmatch(reason_sha256) is None:
        raise ReviewAdapterError("generation yield reason digest is invalid")
    if (
        not isinstance(evidence_record_ids, list)
        or not 1 <= len(evidence_record_ids) <= 16
        or any(
            not isinstance(record_id, str)
            or RECORD_ID_RE.fullmatch(record_id) is None
            for record_id in evidence_record_ids
        )
        or len(set(evidence_record_ids)) != len(evidence_record_ids)
    ):
        raise ReviewAdapterError("generation yield evidence ids are invalid")
    response = _invoke_adapter(
        "review-status",
        _command(
            "review_status",
            {
                "operation": "generation_yield_prepare",
                "state": state,
                "reason_sha256": reason_sha256,
                "evidence_record_ids": list(evidence_record_ids),
            },
        ),
        timeout_seconds=30,
    )
    raw = _exact_object(
        response,
        {
            "schema_version",
            "operation",
            "admission_id",
            "run_id",
            "cycle_id",
            "handoff_id",
            "content_sha256",
            "to_thread_epoch",
            "root_thread_id",
            "root_turn_id",
            "state",
            "reason_sha256",
            "evidence_record_ids",
        },
        label="generation yield admission",
    )
    if (
        raw["schema_version"] != "rethlas_generation_yield_admission_v1"
        or raw["operation"] != "generation_yield_prepare"
        or raw["state"] != state
        or raw["reason_sha256"] != reason_sha256
        or raw["evidence_record_ids"] != evidence_record_ids
    ):
        raise ReviewAdapterError("generation yield admission binding mismatch")
    for key in (
        "admission_id",
        "run_id",
        "cycle_id",
        "root_thread_id",
        "root_turn_id",
    ):
        value = raw[key]
        if (
            not isinstance(value, str)
            or not value
            or len(value.encode("utf-8")) > 256
        ):
            raise ReviewAdapterError(f"generation yield admission {key} is invalid")
    if (
        not isinstance(raw["handoff_id"], str)
        or HANDOFF_ID_RE.fullmatch(raw["handoff_id"]) is None
        or not isinstance(raw["content_sha256"], str)
        or SHA256_RE.fullmatch(raw["content_sha256"]) is None
        or raw["handoff_id"] != f"handoff_{raw['content_sha256']}"
    ):
        raise ReviewAdapterError("generation yield handoff binding is invalid")
    if (
        type(raw["to_thread_epoch"]) is not int
        or raw["to_thread_epoch"] < 1
    ):
        raise ReviewAdapterError("generation yield target epoch is invalid")
    return deepcopy(raw)


def _bound_handoff_operation(
    *, command: str, operation: str, handoff_id_value: str, content_sha256: str
) -> dict[str, Any]:
    if HANDOFF_ID_RE.fullmatch(handoff_id_value) is None:
        raise ReviewAdapterError("handoff_id is invalid")
    if SHA256_RE.fullmatch(content_sha256) is None:
        raise ReviewAdapterError("content_sha256 is invalid")
    if handoff_id_value != f"handoff_{content_sha256}":
        raise ReviewAdapterError("handoff id and content digest disagree")
    response = _invoke_adapter(
        command,
        _command(
            operation,
            {"handoff_id": handoff_id_value, "content_sha256": content_sha256},
        ),
        timeout_seconds=30,
    )
    return _validate_handoff_response(
        response,
        operation=operation,
        expected_handoff_id=handoff_id_value,
        expected_content_sha256=content_sha256,
        require_content=command == "context-handoff-get",
    )


def context_handoff_get(
    *,
    handoff_id: str,
    content_sha256: str,
    thread_epoch: str,
    root_thread_id: str,
    root_turn_id: str,
) -> dict[str, Any]:
    for label, value in {
        "thread_epoch": thread_epoch,
        "root_thread_id": root_thread_id,
        "root_turn_id": root_turn_id,
    }.items():
        if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 256:
            raise ReviewAdapterError(f"context handoff {label} is invalid")
    if HANDOFF_ID_RE.fullmatch(handoff_id) is None or SHA256_RE.fullmatch(content_sha256) is None:
        raise ReviewAdapterError("context handoff binding is invalid")
    if handoff_id != f"handoff_{content_sha256}":
        raise ReviewAdapterError("handoff id and content digest disagree")
    response = _invoke_adapter(
        "context-handoff-get",
        _command(
            "context_handoff_get",
            {
                "handoff_id": handoff_id,
                "content_sha256": content_sha256,
                "thread_epoch": thread_epoch,
                "root_thread_id": root_thread_id,
                "root_turn_id": root_turn_id,
            },
        ),
        timeout_seconds=30,
    )
    return _validate_handoff_response(
        response,
        operation="context_handoff_get",
        expected_handoff_id=handoff_id,
        expected_content_sha256=content_sha256,
        require_content=True,
    )


def context_handoff_status(*, handoff_id: str, content_sha256: str) -> dict[str, Any]:
    return _bound_handoff_operation(
        command="context-handoff-status",
        operation="context_handoff_status",
        handoff_id_value=handoff_id,
        content_sha256=content_sha256,
    )


def context_handoff_preflight(
    *,
    handoff_id: str,
    content_sha256: str,
    thread_epoch: str,
    root_thread_id: str,
    root_turn_id: str,
    tool_name: str,
) -> dict[str, Any]:
    values = {
        "thread_epoch": thread_epoch,
        "root_thread_id": root_thread_id,
        "root_turn_id": root_turn_id,
        "tool_name": tool_name,
    }
    if any(
        not isinstance(value, str) or not value or len(value.encode("utf-8")) > 256
        for value in values.values()
    ):
        raise ReviewAdapterError("context handoff preflight binding is invalid")
    if HANDOFF_ID_RE.fullmatch(handoff_id) is None or SHA256_RE.fullmatch(content_sha256) is None:
        raise ReviewAdapterError("context handoff preflight digest is invalid")
    response = _invoke_adapter(
        "context-handoff-status",
        _command(
            "context_handoff_status",
            {
                "operation": "context_handoff_preflight",
                "handoff_id": handoff_id,
                "content_sha256": content_sha256,
                **values,
            },
        ),
        timeout_seconds=30,
    )
    return _validate_handoff_response(
        response,
        operation="context_handoff_preflight",
        expected_handoff_id=handoff_id,
        expected_content_sha256=content_sha256,
        require_content=False,
    )


def route_cycle_close(
    *,
    handoff_id: str,
    content_sha256: str,
    thread_epoch: str,
    root_thread_id: str,
    root_turn_id: str,
    disposition: str,
    next_milestone: Mapping[str, Any],
) -> dict[str, Any]:
    if disposition != "continue_next_cycle":
        raise ReviewAdapterError("route cycle disposition must be continue_next_cycle")
    milestone = _exact_object(
        dict(next_milestone),
        {"description", "test"},
        label="route cycle next milestone",
    )
    if any(
        not isinstance(milestone[key], str)
        or not milestone[key].strip()
        or len(milestone[key].encode("utf-8")) > 8_192
        for key in ("description", "test")
    ):
        raise ReviewAdapterError("route cycle next milestone is invalid")
    for label, value in {
        "thread_epoch": thread_epoch,
        "root_thread_id": root_thread_id,
        "root_turn_id": root_turn_id,
    }.items():
        if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 256:
            raise ReviewAdapterError(f"route cycle {label} is invalid")
    if HANDOFF_ID_RE.fullmatch(handoff_id) is None or SHA256_RE.fullmatch(content_sha256) is None:
        raise ReviewAdapterError("route cycle handoff binding is invalid")
    if handoff_id != f"handoff_{content_sha256}":
        raise ReviewAdapterError("route cycle handoff id and digest disagree")
    response = _invoke_adapter(
        "context-handoff-status",
        _command(
            "context_handoff_status",
            {
                "operation": "route_cycle_close",
                "handoff_id": handoff_id,
                "content_sha256": content_sha256,
                "thread_epoch": thread_epoch,
                "root_thread_id": root_thread_id,
                "root_turn_id": root_turn_id,
                "disposition": disposition,
                "next_milestone": deepcopy(milestone),
            },
        ),
        timeout_seconds=30,
    )
    return _validate_handoff_response(
        response,
        operation="route_cycle_close",
        expected_handoff_id=handoff_id,
        expected_content_sha256=content_sha256,
        require_content=False,
    )

#!/usr/bin/env python3
"""Thin, durable human hot-join adapter for the Rethlas generator.

The adapter deliberately sits outside ``agents/generation`` so the generation
agent cannot rewrite its control plane.  It owns only conversation transport:
an owner-only SQLite ledger and the Codex app-server thread/turn RPCs.  It does
not import, call, wrap, or modify the proof verifier or publication client.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


SCHEMA_VERSION = 2
HOTJOIN_CONTROL_PLANE_VERSION = 1
ZERO_DIGEST = "0" * 64
MESSAGE_MODES = frozenset({"steer", "queue", "interrupt"})
MESSAGE_STATES = frozenset(
    {
        "queued",
        "deferred",
        "dispatching",
        "interrupting",
        "delivery_unknown",
        "delivered",
        "failed",
        "interrupted",
        "responded",
    }
)
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
SECRET_RE = re.compile(
    r"(?i)((?:authorization|api[_-]?key|token|secret)\s*[=:]\s*)([^\s,;]+)"
)
SECRET_ENV_NAME_RE = re.compile(
    r"(?i)(?:^|[_-])(?:token|secret|password|api[_-]?key|authorization)(?:$|[_-])"
)
SAFE_TOKEN_USAGE_KEY_PARTS = frozenset(
    {
        ("token", "usage"),
        ("token", "usage", "count", "finality"),
        ("token", "usage", "cumulative", "growth", "sample", "count"),
        ("token", "usage", "cumulative", "growth", "sample", "totals"),
        ("token", "usage", "duplicate", "notification", "count"),
        ("token", "usage", "finality"),
        ("token", "usage", "notification", "count"),
        ("token", "usage", "observed"),
    }
)
TOKEN_USAGE_REQUIRED_BREAKDOWN_FIELDS = frozenset(
    {
        "cachedInputTokens",
        "inputTokens",
        "outputTokens",
        "reasoningOutputTokens",
        "totalTokens",
    }
)
TOKEN_USAGE_OPTIONAL_BREAKDOWN_FIELDS = frozenset({"cacheWriteInputTokens"})
TOKEN_USAGE_FINALITY_VALUES = frozenset(
    {
        "not_observed_after_bounded_post_terminal_settle",
        "observed_not_schema_attested_final",
        "unknown_after_adapter_interruption",
    }
)
TOKEN_USAGE_COUNT_FINALITY_VALUES = frozenset(
    {
        "not_observed_after_bounded_post_terminal_settle",
        "observed_not_schema_attested_inference_count",
    }
)
MAX_MESSAGE_BYTES = 65_536
MAX_TURN_PROMPT_BYTES = 1_048_576
DEFAULT_RPC_TIMEOUT_SECONDS = 30.0
DEFAULT_APP_SERVER_CLOSE_GRACE_SECONDS = 2.0
DEFAULT_POLL_SECONDS = 0.10
DEFAULT_IDLE_GRACE_SECONDS = 1.0
DEFAULT_LEASE_SECONDS = 120.0
MAX_APP_SERVER_LINE_BYTES = 8 * 1024 * 1024
MAX_APP_SERVER_STDERR_LINE_CHARS = 4096
MAX_QUEUED_NOTIFICATIONS = 10_000
MAX_QUEUED_NOTIFICATION_BYTES = 32 * 1024 * 1024
MAX_AUDIT_PAYLOAD_BYTES = 256 * 1024
MAX_AUDIT_EVENTS_PER_RUN = 20_000
MAX_MODEL_CATALOG_PAGES = 32
MAX_MODEL_CATALOG_ENTRIES = 4096
MAX_REROUTE_FIELD_BYTES = 4096
SUPPORTED_MODEL_REROUTE_REASONS = frozenset({"highRiskCyberActivity"})
DEFAULT_POST_TERMINAL_SETTLE_SECONDS = 0.25
MAX_POST_TERMINAL_SETTLE_SECONDS = 5.0
DEFAULT_STATE_DB = (
    Path(__file__).resolve().parent / ".rethlas_hotjoin" / "messages.sqlite3"
)
REQUIRED_APP_SERVER_METHODS = (
    "initialize",
    "item/completed",
    "model/list",
    "model/rerouted",
    "thread/read",
    "thread/resume",
    "thread/start",
    "thread/tokenUsage/updated",
    "turn/completed",
    "turn/interrupt",
    "turn/start",
    "turn/started",
    "turn/steer",
)


class HotJoinError(RuntimeError):
    """Base class for adapter failures."""


class CapabilityError(HotJoinError):
    """The installed app-server does not support the required RPC contract."""


class ProtocolError(HotJoinError):
    """The app-server violated its JSONL/RPC contract."""


class RpcError(HotJoinError):
    """One app-server RPC returned an error object."""

    def __init__(self, method: str, error: object) -> None:
        self.method = method
        self.error = error
        super().__init__(f"app-server RPC {method} failed: {_safe_error_text(error)}")


class IdempotencyConflict(HotJoinError):
    """A client message id was reused with different content."""


class LeaseBusy(HotJoinError):
    """Another adapter owns the generator run."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _validate_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or RUN_ID_RE.fullmatch(run_id) is None:
        raise ValueError(
            "run_id must be 1-128 ASCII letters, digits, '.', '_', ':', or '-', "
            "starting with a letter or digit"
        )
    return run_id


def _safe_stderr_line(raw: str) -> str:
    # RPC failures can echo an entire config as one string.  Bound work before
    # applying regexes, then redact both ordinary key names and composite env
    # names such as VERIFY_API_TOKEN.
    redacted = raw[:32_768].rstrip("\n")
    redacted = re.sub(
        r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+",
        "Bearer <redacted>",
        redacted,
    )
    redacted = re.sub(
        r"""(?ix)(["'][^"']*(?:authorization|api[_-]?key|token|secret|password)[^"']*["']\s*:\s*["'])([^"']*)(["'])""",
        r"\1<redacted>\3",
        redacted,
    )
    redacted = SECRET_RE.sub(r"\1<redacted>", redacted)
    redacted = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "sk-<redacted>", redacted)
    return redacted[:4096]


def _canonical_key_parts(key: str) -> tuple[str, ...]:
    camel_separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key)
    camel_separated = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", camel_separated)
    return tuple(
        part.casefold() for part in re.split(r"[^A-Za-z0-9]+", camel_separated) if part
    )


def _is_sensitive_key(key: str) -> bool:
    """Recognize secret-bearing keys without treating words like tokenizer as tokens."""

    parts = _canonical_key_parts(key)
    if SECRET_ENV_NAME_RE.search(key):
        return True
    if any(part in {"authorization", "password", "secret", "token"} for part in parts):
        return True
    if any(left == "api" and right == "key" for left, right in zip(parts, parts[1:])):
        return True
    compact = "".join(parts)
    return (
        compact == "apikey"
        or compact.endswith(("password", "secret", "token"))
        or compact.startswith("authorization")
    )


def _is_nonnegative_int(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= 0


def _project_token_usage_breakdown(value: object) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    fields = set(value)
    allowed = (
        TOKEN_USAGE_REQUIRED_BREAKDOWN_FIELDS | TOKEN_USAGE_OPTIONAL_BREAKDOWN_FIELDS
    )
    if (
        not TOKEN_USAGE_REQUIRED_BREAKDOWN_FIELDS.issubset(fields)
        or not fields.issubset(allowed)
        or any(not isinstance(field, str) for field in fields)
        or any(not _is_nonnegative_int(amount) for amount in value.values())
    ):
        return None
    return {field: value[field] for field in sorted(fields)}


def _project_safe_telemetry(
    parts: tuple[str, ...], value: object
) -> tuple[bool, object]:
    if parts == ("token", "usage"):
        if not isinstance(value, dict) or not {"last", "total"}.issubset(value):
            return False, None
        if not set(value).issubset({"last", "modelContextWindow", "total"}):
            return False, None
        last = _project_token_usage_breakdown(value.get("last"))
        total = _project_token_usage_breakdown(value.get("total"))
        if last is None or total is None or set(last) != set(total):
            return False, None
        projected: dict[str, object] = {"last": last, "total": total}
        if "modelContextWindow" in value:
            context_window = value["modelContextWindow"]
            if context_window is not None and not _is_nonnegative_int(context_window):
                return False, None
            projected["modelContextWindow"] = context_window
        return True, projected
    if parts == ("token", "usage", "observed"):
        return (value is None or isinstance(value, bool)), value
    if parts in {
        ("token", "usage", "cumulative", "growth", "sample", "count"),
        ("token", "usage", "duplicate", "notification", "count"),
        ("token", "usage", "notification", "count"),
    }:
        return _is_nonnegative_int(value), value
    if parts == ("token", "usage", "finality"):
        return (isinstance(value, str) and value in TOKEN_USAGE_FINALITY_VALUES), value
    if parts == ("token", "usage", "count", "finality"):
        return (
            isinstance(value, str) and value in TOKEN_USAGE_COUNT_FINALITY_VALUES
        ), value
    if parts == ("token", "usage", "cumulative", "growth", "sample", "totals"):
        if value is None:
            return True, None
        projected_breakdown = _project_token_usage_breakdown(value)
        return projected_breakdown is not None, projected_breakdown
    return False, None


def _redact_sensitive_object(value: object, *, key: str | None = None) -> object:
    if key is not None:
        parts = _canonical_key_parts(key)
        if parts in SAFE_TOKEN_USAGE_KEY_PARTS:
            valid, projected = _project_safe_telemetry(parts, value)
            return projected if valid else "<redacted>"
        if _is_sensitive_key(key):
            return "<redacted>"
    if isinstance(value, dict):
        return {
            str(child_key): _redact_sensitive_object(child_value, key=str(child_key))
            for child_key, child_value in value.items()
        }
    if isinstance(value, list):
        return [_redact_sensitive_object(child) for child in value]
    if isinstance(value, str):
        return _safe_stderr_line(value)
    return value


def _safe_error_text(error: object) -> str:
    return _canonical_json(_redact_sensitive_object(error))[:8192]


def _mcp_args_commitment(args: Sequence[str], cwd: str) -> list[object]:
    committed: list[object] = []
    cwd_path = Path(cwd)
    for value in args:
        candidate = Path(value)
        resolved_candidate = (
            candidate if candidate.is_absolute() else cwd_path / candidate
        )
        try:
            metadata = resolved_candidate.lstat()
        except OSError:
            committed.append({"literal": value})
            continue
        if not stat.S_ISREG(metadata.st_mode) or resolved_candidate.is_symlink():
            committed.append({"literal": value})
            continue
        if metadata.st_size > 20_000_000:
            raise ValueError("MCP argument file exceeds 20 MB fingerprint limit")
        digest = hashlib.sha256()
        with resolved_candidate.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        committed.append(
            {
                "file_name": resolved_candidate.name,
                "sha256": digest.hexdigest(),
            }
        )
    return committed


def _adapter_code_commitment(path: Path | None = None) -> dict[str, object]:
    adapter_path = Path(__file__) if path is None else path
    metadata = adapter_path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or adapter_path.is_symlink()
        or metadata.st_size > 5_000_000
    ):
        raise HotJoinError(
            "hot-join control plane must be a regular non-symlink file under 5 MB"
        )
    return {
        "control_plane_version": HOTJOIN_CONTROL_PLANE_VERSION,
        "sha256": hashlib.sha256(adapter_path.read_bytes()).hexdigest(),
    }


def _mcp_env_commitment(env: Mapping[str, str]) -> tuple[dict[str, str], list[str]]:
    committed: dict[str, str] = {}
    rotatable: list[str] = []
    for key, value in sorted(env.items()):
        if _is_sensitive_key(key):
            committed[key] = "<rotatable-secret>"
            rotatable.append(key)
        else:
            committed[key] = value
    return committed, rotatable


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _json_loads_strict(raw: str) -> Any:
    return json.loads(
        raw,
        object_pairs_hook=_reject_duplicate_json_keys,
        parse_constant=_reject_json_constant,
    )


def _event_digest(
    *,
    run_id: str,
    event_id: str,
    kind: str,
    actor: str,
    created_at_utc: str,
    payload_json: str,
    previous_digest: str,
) -> str:
    material = {
        "actor": actor,
        "created_at_utc": created_at_utc,
        "event_id": event_id,
        "kind": kind,
        "payload": _json_loads_strict(payload_json),
        "previous_digest": previous_digest,
        "run_id": run_id,
    }
    return hashlib.sha256(_canonical_json(material).encode("utf-8")).hexdigest()


def _secure_database_file(path: Path) -> Path:
    absolute = Path(os.path.abspath(os.fspath(path)))
    parent = absolute.parent
    # A final-component lstat alone does not protect
    # ``dir/link/subdir/database`` from being redirected through an ancestor.
    existing_components: list[Path] = []
    current = parent
    while current != current.parent:
        existing_components.append(current)
        current = current.parent
    existing_components.append(current)
    for component in reversed(existing_components):
        if component.is_symlink():
            raise HotJoinError(f"hot-join state path traverses a symlink: {component}")
    parent_existed = parent.exists()
    parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    parent_metadata = parent.lstat()
    if not stat.S_ISDIR(parent_metadata.st_mode) or parent.is_symlink():
        raise HotJoinError(
            f"hot-join state parent must be a non-symlink directory: {parent}"
        )
    if hasattr(os, "getuid") and parent_metadata.st_uid != os.getuid():
        raise HotJoinError(
            f"hot-join state parent is not owned by the current user: {parent}"
        )
    if parent_existed:
        if stat.S_IMODE(parent_metadata.st_mode) & 0o077:
            raise HotJoinError(
                "existing hot-join state parent must not grant group/other access: "
                f"{parent}"
            )
    else:
        try:
            os.chmod(parent, 0o700)
        except OSError as exc:
            raise HotJoinError(
                f"cannot secure hot-join state parent {parent}: {exc}"
            ) from exc

    def validate_existing_database() -> None:
        metadata = absolute.lstat()
        if not stat.S_ISREG(metadata.st_mode) or absolute.is_symlink():
            raise HotJoinError(
                f"hot-join database must be a regular non-symlink file: {absolute}"
            )
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise HotJoinError(
                f"hot-join database is not owned by the current user: {absolute}"
            )

    if absolute.exists() or absolute.is_symlink():
        validate_existing_database()
    else:
        try:
            descriptor = os.open(absolute, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            # Another local owner process may win the first-open race after
            # our existence check.  Treat that as success only after repeating
            # every regular-file, symlink, and ownership check.
            validate_existing_database()
        else:
            os.close(descriptor)
        validate_existing_database()
    os.chmod(absolute, 0o600)
    return absolute


@dataclass(frozen=True)
class MessageRecord:
    message_id: str
    client_message_id: str
    mode: str
    text: str
    state: str
    accepted_sequence: int
    attempt_id: str | None
    thread_id: str | None
    turn_id: str | None


@dataclass(frozen=True)
class LeaseToken:
    owner_id: str
    fence: int


@dataclass(frozen=True)
class TurnIntentRecord:
    client_message_id: str
    kind: str
    prompt: str
    config: dict[str, Any]
    state: str
    thread_id: str
    turn_id: str | None
    message_id: str | None
    dispatch_count: int


@dataclass(frozen=True)
class PendingTerminal:
    turn: dict[str, Any]
    assistant_message: str
    deadline_monotonic: float
    expected_interruption: bool


class ConversationLedger:
    """Owner-only SQLite store with an immutable, hash-chained event ledger."""

    def __init__(self, path: Path | str = DEFAULT_STATE_DB) -> None:
        self.path = _secure_database_file(Path(path))
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=10.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                INSERT OR IGNORE INTO metadata(key, value)
                    VALUES ('schema_version', '2');

                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    problem_id TEXT NOT NULL,
                    owner_uid INTEGER NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL,
                    thread_id TEXT,
                    active_turn_id TEXT,
                    generator_fingerprint TEXT,
                    generation INTEGER NOT NULL DEFAULT 0,
                    last_sequence INTEGER NOT NULL DEFAULT 0,
                    head_digest TEXT NOT NULL DEFAULT
                        '0000000000000000000000000000000000000000000000000000000000000000'
                );

                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    kind TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    previous_digest TEXT NOT NULL,
                    digest TEXT NOT NULL UNIQUE
                );
                CREATE INDEX IF NOT EXISTS events_run_sequence
                    ON events(run_id, sequence);

                CREATE TRIGGER IF NOT EXISTS events_are_immutable_update
                BEFORE UPDATE ON events BEGIN
                    SELECT RAISE(ABORT, 'hot-join events are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS events_are_immutable_delete
                BEFORE DELETE ON events BEGIN
                    SELECT RAISE(ABORT, 'hot-join events are append-only');
                END;

                CREATE TABLE IF NOT EXISTS messages (
                    message_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    client_message_id TEXT NOT NULL,
                    mode TEXT NOT NULL CHECK(mode IN ('steer', 'queue', 'interrupt')),
                    text TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN
                        ('queued', 'deferred', 'dispatching', 'interrupting', 'delivery_unknown',
                         'delivered', 'failed', 'interrupted', 'responded')),
                    accepted_sequence INTEGER NOT NULL REFERENCES events(sequence),
                    attempt_id TEXT,
                    thread_id TEXT,
                    turn_id TEXT,
                    UNIQUE(run_id, client_message_id)
                );
                CREATE INDEX IF NOT EXISTS messages_pending
                    ON messages(run_id, state, accepted_sequence);

                CREATE TABLE IF NOT EXISTS turn_intents (
                    client_message_id TEXT NOT NULL,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    kind TEXT NOT NULL CHECK(kind IN ('bootstrap', 'owner')),
                    prompt TEXT NOT NULL,
                    prompt_sha256 TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    config_digest TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN
                        ('dispatching', 'active', 'delivery_unknown',
                         'retry_authorized', 'completed', 'failed', 'interrupted')),
                    dispatch_count INTEGER NOT NULL DEFAULT 0
                        CHECK(dispatch_count >= 0),
                    thread_id TEXT NOT NULL,
                    turn_id TEXT,
                    message_id TEXT REFERENCES messages(message_id),
                    PRIMARY KEY(run_id, client_message_id)
                );
                CREATE INDEX IF NOT EXISTS turn_intents_pending
                    ON turn_intents(run_id, state);

                CREATE TABLE IF NOT EXISTS run_quarantines (
                    run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
                    kind TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    thread_id TEXT,
                    turn_id TEXT,
                    payload_json TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL
                );
                CREATE TRIGGER IF NOT EXISTS run_quarantines_are_immutable_update
                BEFORE UPDATE ON run_quarantines BEGIN
                    SELECT RAISE(ABORT, 'run quarantines are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS run_quarantines_are_immutable_delete
                BEFORE DELETE ON run_quarantines BEGIN
                    SELECT RAISE(ABORT, 'run quarantines are immutable');
                END;

                CREATE TABLE IF NOT EXISTS leases (
                    run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
                    owner_id TEXT NOT NULL,
                    fence INTEGER NOT NULL,
                    expires_epoch REAL NOT NULL
                );
                """
            )
            version = str(
                connection.execute(
                    "SELECT value FROM metadata WHERE key = 'schema_version'"
                ).fetchone()["value"]
            )
            if version == "1":
                connection.execute("BEGIN IMMEDIATE")
                current = str(
                    connection.execute(
                        "SELECT value FROM metadata WHERE key = 'schema_version'"
                    ).fetchone()["value"]
                )
                if current == "1":
                    columns = {
                        str(row["name"])
                        for row in connection.execute(
                            "PRAGMA table_info(turn_intents)"
                        ).fetchall()
                    }
                    if "dispatch_count" not in columns:
                        # Every v1 dispatching row crossed the old pre-call
                        # intent boundary. Conservatively treat it as having a
                        # possible external side effect; never classify it as
                        # a fresh prepared intent after migration.
                        connection.execute(
                            """
                            ALTER TABLE turn_intents
                            ADD COLUMN dispatch_count INTEGER NOT NULL DEFAULT 1
                                CHECK(dispatch_count >= 0)
                            """
                        )
                    connection.execute(
                        "UPDATE metadata SET value = '2' WHERE key = 'schema_version'"
                    )
                elif current != str(SCHEMA_VERSION):
                    raise HotJoinError(
                        f"unsupported hot-join database schema {current}; "
                        f"expected {SCHEMA_VERSION}"
                    )
                connection.commit()
                version = str(
                    connection.execute(
                        "SELECT value FROM metadata WHERE key = 'schema_version'"
                    ).fetchone()["value"]
                )
            columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(turn_intents)"
                ).fetchall()
            }
            if version != str(SCHEMA_VERSION) or "dispatch_count" not in columns:
                raise HotJoinError(
                    f"unsupported hot-join database schema {version}; expected {SCHEMA_VERSION}"
                )
        os.chmod(self.path, 0o600)

    @staticmethod
    def _owner_uid() -> int:
        return os.getuid() if hasattr(os, "getuid") else 0

    def _require_owner(self, row: sqlite3.Row) -> None:
        if int(row["owner_uid"]) != self._owner_uid():
            raise HotJoinError("hot-join run belongs to a different local user")

    def _run_row(self, connection: sqlite3.Connection, run_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM runs WHERE run_id = ?", (_validate_run_id(run_id),)
        ).fetchone()
        if row is None:
            raise HotJoinError(f"unknown hot-join run: {run_id}")
        self._require_owner(row)
        return row

    def _require_lease(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        lease: LeaseToken,
    ) -> None:
        row = connection.execute(
            "SELECT owner_id, fence, expires_epoch FROM leases WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if (
            row is None
            or row["owner_id"] != lease.owner_id
            or int(row["fence"]) != lease.fence
            or float(row["expires_epoch"]) <= time.time()
        ):
            raise LeaseBusy(
                "adapter lease is stale, expired, or owned by another broker"
            )

    def _append_event(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        kind: str,
        actor: str,
        payload: Mapping[str, Any],
    ) -> tuple[int, str, str]:
        run = self._run_row(connection, run_id)
        previous_digest = str(run["head_digest"])
        event_id = f"evt_{uuid.uuid4().hex}"
        created_at_utc = _utc_now()
        payload_json = _canonical_json(dict(payload))
        digest = _event_digest(
            run_id=run_id,
            event_id=event_id,
            kind=kind,
            actor=actor,
            created_at_utc=created_at_utc,
            payload_json=payload_json,
            previous_digest=previous_digest,
        )
        cursor = connection.execute(
            """
            INSERT INTO events(
                event_id, run_id, kind, actor, created_at_utc, payload_json,
                previous_digest, digest
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                run_id,
                kind,
                actor,
                created_at_utc,
                payload_json,
                previous_digest,
                digest,
            ),
        )
        sequence = int(cursor.lastrowid)
        connection.execute(
            """
            UPDATE runs
            SET last_sequence = ?, head_digest = ?, updated_at_utc = ?
            WHERE run_id = ?
            """,
            (sequence, digest, created_at_utc, run_id),
        )
        return sequence, event_id, digest

    def create_run(self, run_id: str, problem_id: str) -> dict[str, Any]:
        run_id = _validate_run_id(run_id)
        if not isinstance(problem_id, str) or not problem_id.strip():
            raise ValueError("problem_id must be non-empty")
        owner_uid = self._owner_uid()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if existing is not None:
                self._require_owner(existing)
                if existing["problem_id"] != problem_id:
                    raise IdempotencyConflict(
                        f"run_id {run_id} already belongs to problem {existing['problem_id']}"
                    )
                connection.commit()
                return self.status(run_id)
            now = _utc_now()
            connection.execute(
                """
                INSERT INTO runs(
                    run_id, problem_id, owner_uid, created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (run_id, problem_id, owner_uid, now, now),
            )
            self._append_event(
                connection,
                run_id=run_id,
                kind="run_created",
                actor="owner",
                payload={"problem_id": problem_id, "owner_uid": owner_uid},
            )
            connection.commit()
        return self.status(run_id)

    def enqueue_message(
        self,
        run_id: str,
        *,
        text: str,
        mode: str = "steer",
        client_message_id: str | None = None,
    ) -> dict[str, Any]:
        run_id = _validate_run_id(run_id)
        if mode not in MESSAGE_MODES:
            raise ValueError(f"mode must be one of {sorted(MESSAGE_MODES)}")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("message text must be non-empty")
        if len(text.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise ValueError(f"message exceeds {MAX_MESSAGE_BYTES} UTF-8 bytes")
        normalized_client_id = client_message_id or f"user_{uuid.uuid4().hex}"
        if (
            not isinstance(normalized_client_id, str)
            or not normalized_client_id
            or len(normalized_client_id.encode("utf-8")) > 256
            or any(ord(character) < 0x20 for character in normalized_client_id)
        ):
            raise ValueError("client_message_id must be 1-256 printable UTF-8 bytes")

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._run_row(connection, run_id)
            existing = connection.execute(
                """
                SELECT * FROM messages
                WHERE run_id = ? AND client_message_id = ?
                """,
                (run_id, normalized_client_id),
            ).fetchone()
            if existing is not None:
                if existing["text"] != text or existing["mode"] != mode:
                    raise IdempotencyConflict(
                        "client_message_id was already used with different text or mode"
                    )
                connection.commit()
                return {
                    "accepted": True,
                    "idempotent_replay": True,
                    "message_id": existing["message_id"],
                    "client_message_id": normalized_client_id,
                    "mode": mode,
                    "state": existing["state"],
                    "accepted_sequence": existing["accepted_sequence"],
                }

            message_id = f"msg_{uuid.uuid4().hex}"
            sequence, event_id, digest = self._append_event(
                connection,
                run_id=run_id,
                kind="message_accepted",
                actor="owner",
                payload={
                    "client_message_id": normalized_client_id,
                    "message_id": message_id,
                    "mode": mode,
                    "text": text,
                },
            )
            connection.execute(
                """
                INSERT INTO messages(
                    message_id, run_id, client_message_id, mode, text, state,
                    accepted_sequence
                ) VALUES (?, ?, ?, ?, ?, 'queued', ?)
                """,
                (message_id, run_id, normalized_client_id, mode, text, sequence),
            )
            connection.commit()
        return {
            "accepted": True,
            "idempotent_replay": False,
            "message_id": message_id,
            "client_message_id": normalized_client_id,
            "mode": mode,
            "state": "queued",
            "accepted_sequence": sequence,
            "event_id": event_id,
            "event_digest": digest,
        }

    @staticmethod
    def _message_from_row(row: sqlite3.Row) -> MessageRecord:
        return MessageRecord(
            message_id=str(row["message_id"]),
            client_message_id=str(row["client_message_id"]),
            mode=str(row["mode"]),
            text=str(row["text"]),
            state=str(row["state"]),
            accepted_sequence=int(row["accepted_sequence"]),
            attempt_id=row["attempt_id"],
            thread_id=row["thread_id"],
            turn_id=row["turn_id"],
        )

    def pending_messages(self, run_id: str) -> list[MessageRecord]:
        with self._connect() as connection:
            self._run_row(connection, run_id)
            rows = connection.execute(
                """
                SELECT * FROM messages
                WHERE run_id = ? AND state IN
                    ('queued', 'deferred', 'dispatching', 'interrupting', 'delivery_unknown')
                ORDER BY accepted_sequence
                """,
                (run_id,),
            ).fetchall()
        return [self._message_from_row(row) for row in rows]

    def begin_delivery(
        self,
        run_id: str,
        message_id: str,
        *,
        thread_id: str,
        turn_id: str | None,
        action: str,
        lease: LeaseToken,
    ) -> str:
        if action not in {"turn/start", "turn/steer", "turn/interrupt"}:
            raise ValueError("invalid app-server delivery action")
        attempt_id = f"attempt_{uuid.uuid4().hex}"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._run_row(connection, run_id)
            self._require_lease(connection, run_id, lease)
            row = connection.execute(
                "SELECT * FROM messages WHERE run_id = ? AND message_id = ?",
                (run_id, message_id),
            ).fetchone()
            if row is None:
                raise HotJoinError(f"unknown message: {message_id}")
            if row["state"] in {"delivered", "failed", "interrupted", "responded"}:
                connection.commit()
                return str(row["attempt_id"] or attempt_id)
            if row["state"] != "queued":
                raise HotJoinError(
                    f"message cannot begin delivery from state {row['state']}"
                )
            self._append_event(
                connection,
                run_id=run_id,
                kind="delivery_attempted",
                actor="adapter",
                payload={
                    "action": action,
                    "attempt_id": attempt_id,
                    "message_id": message_id,
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                },
            )
            next_state = "interrupting" if action == "turn/interrupt" else "dispatching"
            connection.execute(
                """
                UPDATE messages
                SET state = ?, attempt_id = ?, thread_id = ?, turn_id = ?
                WHERE message_id = ?
                """,
                (next_state, attempt_id, thread_id, turn_id, message_id),
            )
            connection.commit()
        return attempt_id

    def mark_delivery_unknown(
        self,
        run_id: str,
        message_id: str,
        *,
        reason: str,
        lease: LeaseToken,
    ) -> None:
        """Preserve an ambiguous app-server side effect for explicit owner action.

        A crash after app-server acceptance but before the local acknowledgement
        cannot be made exactly-once by SQLite.  The adapter therefore never
        silently resends an unobservable attempt.
        """

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._run_row(connection, run_id)
            self._require_lease(connection, run_id, lease)
            row = connection.execute(
                "SELECT * FROM messages WHERE run_id = ? AND message_id = ?",
                (run_id, message_id),
            ).fetchone()
            if row is None:
                raise HotJoinError(f"unknown message: {message_id}")
            if row["state"] in {
                "delivered",
                "delivery_unknown",
                "failed",
                "interrupted",
                "responded",
            }:
                connection.commit()
                return
            if row["state"] not in {"dispatching", "interrupting"}:
                raise HotJoinError(
                    f"message delivery cannot become unknown from state {row['state']}"
                )
            self._append_event(
                connection,
                run_id=run_id,
                kind="delivery_outcome_unknown",
                actor="adapter",
                payload={"message_id": message_id, "reason": reason},
            )
            connection.execute(
                "UPDATE messages SET state = 'delivery_unknown' WHERE message_id = ?",
                (message_id,),
            )
            connection.commit()

    def retry_unknown(self, run_id: str, message_id: str) -> None:
        """Explicitly authorize retrying one ambiguous message delivery."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._run_row(connection, run_id)
            row = connection.execute(
                "SELECT * FROM messages WHERE run_id = ? AND message_id = ?",
                (run_id, message_id),
            ).fetchone()
            if row is None:
                raise HotJoinError(f"unknown message: {message_id}")
            if row["state"] != "delivery_unknown":
                raise HotJoinError(
                    "only delivery_unknown messages can be retried explicitly"
                )
            self._append_event(
                connection,
                run_id=run_id,
                kind="unknown_delivery_retry_authorized",
                actor="owner",
                payload={"message_id": message_id},
            )
            connection.execute(
                """
                UPDATE messages
                SET state = 'queued', attempt_id = NULL, turn_id = NULL
                WHERE message_id = ?
                """,
                (message_id,),
            )
            intent = connection.execute(
                """
                SELECT client_message_id, state FROM turn_intents
                WHERE run_id = ? AND message_id = ?
                """,
                (run_id, message_id),
            ).fetchone()
            if intent is not None:
                if intent["state"] != "delivery_unknown":
                    raise HotJoinError(
                        "message and turn/start uncertainty projections disagree"
                    )
                connection.execute(
                    """
                    UPDATE turn_intents SET state = 'retry_authorized'
                    WHERE run_id = ? AND client_message_id = ?
                    """,
                    (run_id, intent["client_message_id"]),
                )
            connection.commit()

    def mark_delivered(
        self,
        run_id: str,
        message_id: str,
        *,
        attempt_id: str,
        thread_id: str,
        turn_id: str,
        rpc_method: str,
        lease: LeaseToken,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._run_row(connection, run_id)
            self._require_lease(connection, run_id, lease)
            row = connection.execute(
                "SELECT * FROM messages WHERE run_id = ? AND message_id = ?",
                (run_id, message_id),
            ).fetchone()
            if row is None:
                raise HotJoinError(f"unknown message: {message_id}")
            if row["state"] in {"delivered", "failed", "interrupted", "responded"}:
                connection.commit()
                return
            if row["state"] != "dispatching":
                raise HotJoinError(
                    f"message cannot be acknowledged delivered from state {row['state']}"
                )
            if row["attempt_id"] != attempt_id:
                raise HotJoinError(
                    "delivery attempt id does not match durable message state"
                )
            self._append_event(
                connection,
                run_id=run_id,
                kind="message_delivered",
                actor="app_server",
                payload={
                    "attempt_id": attempt_id,
                    "message_id": message_id,
                    "rpc_method": rpc_method,
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                },
            )
            connection.execute(
                """
                UPDATE messages
                SET state = 'delivered', thread_id = ?, turn_id = ?
                WHERE message_id = ?
                """,
                (thread_id, turn_id, message_id),
            )
            connection.commit()

    def requeue_message(
        self,
        run_id: str,
        message_id: str,
        *,
        reason: str,
        lease: LeaseToken,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._run_row(connection, run_id)
            self._require_lease(connection, run_id, lease)
            row = connection.execute(
                "SELECT * FROM messages WHERE run_id = ? AND message_id = ?",
                (run_id, message_id),
            ).fetchone()
            if row is None:
                raise HotJoinError(f"unknown message: {message_id}")
            if row["state"] == "queued":
                connection.commit()
                return
            if row["state"] in {"delivered", "failed", "interrupted", "responded"}:
                connection.commit()
                return
            if row["state"] not in {"dispatching", "interrupting"}:
                raise HotJoinError(
                    f"message cannot be automatically requeued from state {row['state']}"
                )
            self._append_event(
                connection,
                run_id=run_id,
                kind="message_requeued",
                actor="adapter",
                payload={"message_id": message_id, "reason": reason},
            )
            connection.execute(
                """
                UPDATE messages
                SET state = 'queued', attempt_id = NULL, turn_id = NULL
                WHERE message_id = ?
                """,
                (message_id,),
            )
            connection.commit()

    def defer_message_until_turn_ends(
        self,
        run_id: str,
        message_id: str,
        *,
        reason: str,
        lease: LeaseToken,
    ) -> None:
        """Durably queue a rejected same-turn steer behind its active turn."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._run_row(connection, run_id)
            self._require_lease(connection, run_id, lease)
            row = connection.execute(
                "SELECT * FROM messages WHERE run_id = ? AND message_id = ?",
                (run_id, message_id),
            ).fetchone()
            if row is None:
                raise HotJoinError(f"unknown message: {message_id}")
            if row["state"] == "deferred":
                connection.commit()
                return
            if row["state"] != "dispatching" or not row["turn_id"]:
                raise HotJoinError(
                    f"message cannot be deferred from state {row['state']}"
                )
            self._append_event(
                connection,
                run_id=run_id,
                kind="message_deferred_until_turn_end",
                actor="adapter",
                payload={
                    "message_id": message_id,
                    "reason": reason,
                    "turn_id": row["turn_id"],
                },
            )
            connection.execute(
                "UPDATE messages SET state = 'deferred' WHERE message_id = ?",
                (message_id,),
            )
            connection.commit()

    @staticmethod
    def _turn_intent_from_row(row: sqlite3.Row) -> TurnIntentRecord:
        config = _json_loads_strict(str(row["config_json"]))
        if not isinstance(config, dict):
            raise HotJoinError("durable turn intent config is not an object")
        dispatch_count = int(row["dispatch_count"])
        state = str(row["state"])
        if state == "dispatching" and dispatch_count == 0:
            state = "prepared"
        return TurnIntentRecord(
            client_message_id=str(row["client_message_id"]),
            kind=str(row["kind"]),
            prompt=str(row["prompt"]),
            config=config,
            state=state,
            thread_id=str(row["thread_id"]),
            turn_id=row["turn_id"],
            message_id=row["message_id"],
            dispatch_count=dispatch_count,
        )

    def turn_intents(
        self, run_id: str, *, states: set[str] | None = None
    ) -> list[TurnIntentRecord]:
        with self._connect() as connection:
            self._run_row(connection, run_id)
            if states:
                unknown = states - {
                    "active",
                    "completed",
                    "delivery_unknown",
                    "dispatching",
                    "failed",
                    "interrupted",
                    "prepared",
                    "retry_authorized",
                }
                if unknown:
                    raise ValueError(f"unknown turn intent states: {sorted(unknown)}")
                database_states = set(states)
                if "prepared" in database_states:
                    database_states.remove("prepared")
                    database_states.add("dispatching")
                placeholders = ",".join("?" for _ in database_states)
                rows = connection.execute(
                    f"""
                    SELECT * FROM turn_intents
                    WHERE run_id = ? AND state IN ({placeholders})
                    ORDER BY rowid
                    """,
                    [run_id, *sorted(database_states)],
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM turn_intents WHERE run_id = ? ORDER BY rowid",
                    (run_id,),
                ).fetchall()
        records = [self._turn_intent_from_row(row) for row in rows]
        return (
            records
            if states is None
            else [record for record in records if record.state in states]
        )

    def prepare_turn_intent(
        self,
        run_id: str,
        *,
        client_message_id: str,
        kind: str,
        prompt: str,
        config: Mapping[str, Any],
        thread_id: str,
        message_id: str | None,
        lease: LeaseToken,
    ) -> TurnIntentRecord:
        if kind not in {"bootstrap", "owner"}:
            raise ValueError("turn intent kind must be bootstrap or owner")
        if len(prompt.encode("utf-8")) > MAX_TURN_PROMPT_BYTES:
            raise ValueError(f"turn prompt exceeds {MAX_TURN_PROMPT_BYTES} UTF-8 bytes")
        if not prompt.strip() or not client_message_id or not thread_id:
            raise ValueError(
                "turn intent prompt, client id, and thread id are required"
            )
        if (kind == "bootstrap") != (message_id is None):
            raise ValueError("only owner turn intents bind a durable message id")
        config_json = _canonical_json(dict(config))
        config_digest = hashlib.sha256(config_json.encode("utf-8")).hexdigest()
        prompt_digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = self._run_row(connection, run_id)
            self._require_lease(connection, run_id, lease)
            if run["active_turn_id"] is not None:
                raise HotJoinError(
                    "cannot prepare turn/start while another turn is active"
                )
            existing = connection.execute(
                """
                SELECT * FROM turn_intents
                WHERE run_id = ? AND client_message_id = ?
                """,
                (run_id, client_message_id),
            ).fetchone()
            expected = {
                "config_digest": config_digest,
                "kind": kind,
                "message_id": message_id,
                "prompt_sha256": prompt_digest,
                "thread_id": thread_id,
            }
            if existing is not None:
                actual = {key: existing[key] for key in expected}
                if actual != expected:
                    raise IdempotencyConflict(
                        "turn/start client id is bound to different prompt or config"
                    )
                is_prepared = (
                    existing["state"] == "dispatching"
                    and int(existing["dispatch_count"]) == 0
                )
                if not is_prepared and existing["state"] != "retry_authorized":
                    raise HotJoinError(
                        f"turn/start intent cannot be resent from state {existing['state']}"
                    )
            else:
                if kind == "owner":
                    message = connection.execute(
                        """
                        SELECT * FROM messages
                        WHERE run_id = ? AND message_id = ?
                        """,
                        (run_id, message_id),
                    ).fetchone()
                    if (
                        message is None
                        or message["state"] != "dispatching"
                        or message["client_message_id"] != client_message_id
                    ):
                        raise HotJoinError(
                            "owner turn/start intent lacks its dispatching message"
                        )
                self._append_event(
                    connection,
                    run_id=run_id,
                    kind="turn_start_intent",
                    actor="adapter",
                    payload={
                        "client_message_id": client_message_id,
                        "config": dict(config),
                        "config_digest": config_digest,
                        "kind": kind,
                        "message_id": message_id,
                        "prompt": prompt,
                        "prompt_sha256": prompt_digest,
                        "thread_id": thread_id,
                    },
                )
                connection.execute(
                    """
                    INSERT INTO turn_intents(
                        client_message_id, run_id, kind, prompt, prompt_sha256,
                        config_json, config_digest, state, dispatch_count,
                        thread_id, message_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'dispatching', 0, ?, ?)
                    """,
                    (
                        client_message_id,
                        run_id,
                        kind,
                        prompt,
                        prompt_digest,
                        config_json,
                        config_digest,
                        thread_id,
                        message_id,
                    ),
                )
            row = connection.execute(
                """
                SELECT * FROM turn_intents
                WHERE run_id = ? AND client_message_id = ?
                """,
                (run_id, client_message_id),
            ).fetchone()
            connection.commit()
        assert row is not None
        return self._turn_intent_from_row(row)

    def begin_turn_intent_dispatch(
        self,
        run_id: str,
        *,
        client_message_id: str,
        lease: LeaseToken,
    ) -> TurnIntentRecord:
        """Fence the exact durable intent immediately before ``turn/start``."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = self._run_row(connection, run_id)
            self._require_lease(connection, run_id, lease)
            if run["active_turn_id"] is not None:
                raise HotJoinError(
                    "cannot dispatch turn/start while another turn is active"
                )
            intent = connection.execute(
                """
                SELECT * FROM turn_intents
                WHERE run_id = ? AND client_message_id = ?
                """,
                (run_id, client_message_id),
            ).fetchone()
            if intent is None:
                raise HotJoinError("turn/start dispatch lacks a durable intent")
            dispatch_count = int(intent["dispatch_count"])
            is_prepared = intent["state"] == "dispatching" and dispatch_count == 0
            is_retry = intent["state"] == "retry_authorized"
            if not is_prepared and not is_retry:
                raise HotJoinError(
                    "turn/start cannot be dispatched from state "
                    f"{intent['state']} after {dispatch_count} prior dispatches"
                )
            next_count = dispatch_count + 1
            self._append_event(
                connection,
                run_id=run_id,
                kind=(
                    "turn_start_retry_attempted"
                    if is_retry
                    else "turn_start_dispatch_started"
                ),
                actor="adapter",
                payload={
                    "client_message_id": client_message_id,
                    "config_digest": intent["config_digest"],
                    "dispatch_count": next_count,
                    "kind": intent["kind"],
                    "message_id": intent["message_id"],
                    "prompt_sha256": intent["prompt_sha256"],
                    "thread_id": intent["thread_id"],
                },
            )
            connection.execute(
                """
                UPDATE turn_intents
                SET state = 'dispatching', dispatch_count = ?, turn_id = NULL
                WHERE run_id = ? AND client_message_id = ?
                """,
                (next_count, run_id, client_message_id),
            )
            row = connection.execute(
                """
                SELECT * FROM turn_intents
                WHERE run_id = ? AND client_message_id = ?
                """,
                (run_id, client_message_id),
            ).fetchone()
            connection.commit()
        assert row is not None
        return self._turn_intent_from_row(row)

    def bind_turn_intent_applied(
        self,
        run_id: str,
        *,
        client_message_id: str,
        turn_id: str,
        source: str,
        lease: LeaseToken,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = self._run_row(connection, run_id)
            self._require_lease(connection, run_id, lease)
            intent = connection.execute(
                """
                SELECT * FROM turn_intents
                WHERE run_id = ? AND client_message_id = ?
                """,
                (run_id, client_message_id),
            ).fetchone()
            if intent is None:
                raise HotJoinError("turn/start result lacks a durable pre-call intent")
            if intent["state"] == "active" and intent["turn_id"] == turn_id:
                connection.commit()
                return
            if (
                intent["state"] != "dispatching"
                or int(intent["dispatch_count"]) < 1
                or intent["turn_id"] is not None
            ):
                raise HotJoinError(
                    f"turn/start cannot be acknowledged from state {intent['state']}"
                )
            if run["active_turn_id"] not in {None, turn_id}:
                raise HotJoinError(
                    "turn/start conflicts with another durable active turn"
                )
            self._append_event(
                connection,
                run_id=run_id,
                kind="turn_started",
                actor="app_server",
                payload={
                    "client_message_id": client_message_id,
                    "config_digest": intent["config_digest"],
                    "prompt_sha256": intent["prompt_sha256"],
                    "source": source,
                    "thread_id": intent["thread_id"],
                    "turn_id": turn_id,
                },
            )
            connection.execute(
                """
                UPDATE turn_intents SET state = 'active', turn_id = ?
                WHERE run_id = ? AND client_message_id = ?
                """,
                (turn_id, run_id, client_message_id),
            )
            if run["active_turn_id"] is None:
                connection.execute(
                    """
                    UPDATE runs
                    SET active_turn_id = ?, generation = generation + 1
                    WHERE run_id = ?
                    """,
                    (turn_id, run_id),
                )
            message_id = intent["message_id"]
            if message_id is not None:
                message = connection.execute(
                    "SELECT * FROM messages WHERE run_id = ? AND message_id = ?",
                    (run_id, message_id),
                ).fetchone()
                if (
                    message is None
                    or message["state"] != "dispatching"
                    or not message["attempt_id"]
                ):
                    raise HotJoinError(
                        "owner turn/start intent lacks its exact dispatching attempt"
                    )
                self._append_event(
                    connection,
                    run_id=run_id,
                    kind="message_delivered",
                    actor="app_server",
                    payload={
                        "attempt_id": message["attempt_id"],
                        "message_id": message_id,
                        "rpc_method": source,
                        "thread_id": intent["thread_id"],
                        "turn_id": turn_id,
                    },
                )
                connection.execute(
                    """
                    UPDATE messages
                    SET state = 'delivered', thread_id = ?, turn_id = ?
                    WHERE message_id = ?
                    """,
                    (intent["thread_id"], turn_id, message_id),
                )
            connection.commit()

    def mark_turn_intent_unknown(
        self,
        run_id: str,
        *,
        client_message_id: str,
        reason: str,
        lease: LeaseToken,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._run_row(connection, run_id)
            self._require_lease(connection, run_id, lease)
            intent = connection.execute(
                """
                SELECT * FROM turn_intents
                WHERE run_id = ? AND client_message_id = ?
                """,
                (run_id, client_message_id),
            ).fetchone()
            if intent is None:
                raise HotJoinError("unknown turn/start intent")
            if intent["state"] == "delivery_unknown":
                connection.commit()
                return
            if intent["state"] != "dispatching" or int(intent["dispatch_count"]) < 1:
                raise HotJoinError(
                    f"turn/start cannot become unknown from state {intent['state']}"
                )
            self._append_event(
                connection,
                run_id=run_id,
                kind="turn_start_outcome_unknown",
                actor="adapter",
                payload={
                    "client_message_id": client_message_id,
                    "reason": reason,
                },
            )
            connection.execute(
                """
                UPDATE turn_intents SET state = 'delivery_unknown'
                WHERE run_id = ? AND client_message_id = ?
                """,
                (run_id, client_message_id),
            )
            message_id = intent["message_id"]
            if message_id is not None:
                message = connection.execute(
                    "SELECT state FROM messages WHERE message_id = ?",
                    (message_id,),
                ).fetchone()
                if message is not None and message["state"] == "dispatching":
                    self._append_event(
                        connection,
                        run_id=run_id,
                        kind="delivery_outcome_unknown",
                        actor="adapter",
                        payload={"message_id": message_id, "reason": reason},
                    )
                    connection.execute(
                        """
                        UPDATE messages SET state = 'delivery_unknown'
                        WHERE message_id = ?
                        """,
                        (message_id,),
                    )
            connection.commit()

    def mark_turn_intent_rejected(
        self,
        run_id: str,
        *,
        client_message_id: str,
        reason: str,
        lease: LeaseToken,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._run_row(connection, run_id)
            self._require_lease(connection, run_id, lease)
            intent = connection.execute(
                """
                SELECT * FROM turn_intents
                WHERE run_id = ? AND client_message_id = ?
                """,
                (run_id, client_message_id),
            ).fetchone()
            if (
                intent is None
                or intent["state"] != "dispatching"
                or int(intent["dispatch_count"]) < 1
            ):
                raise HotJoinError("turn/start rejection lacks a dispatching intent")
            self._append_event(
                connection,
                run_id=run_id,
                kind="turn_start_rejected",
                actor="app_server",
                payload={"client_message_id": client_message_id, "reason": reason},
            )
            connection.execute(
                """
                UPDATE turn_intents SET state = 'retry_authorized'
                WHERE run_id = ? AND client_message_id = ?
                """,
                (run_id, client_message_id),
            )
            message_id = intent["message_id"]
            if message_id is not None:
                message = connection.execute(
                    "SELECT state FROM messages WHERE message_id = ?",
                    (message_id,),
                ).fetchone()
                if message is not None and message["state"] == "dispatching":
                    self._append_event(
                        connection,
                        run_id=run_id,
                        kind="message_requeued",
                        actor="adapter",
                        payload={"message_id": message_id, "reason": reason},
                    )
                    connection.execute(
                        """
                        UPDATE messages
                        SET state = 'queued', attempt_id = NULL, turn_id = NULL
                        WHERE message_id = ?
                        """,
                        (message_id,),
                    )
            connection.commit()

    def retry_unknown_turn(self, run_id: str, client_message_id: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._run_row(connection, run_id)
            intent = connection.execute(
                """
                SELECT state FROM turn_intents
                WHERE run_id = ? AND client_message_id = ?
                """,
                (run_id, client_message_id),
            ).fetchone()
            if intent is None or intent["state"] != "delivery_unknown":
                raise HotJoinError(
                    "only delivery_unknown turn intents can be retried explicitly"
                )
            self._append_event(
                connection,
                run_id=run_id,
                kind="unknown_turn_retry_authorized",
                actor="owner",
                payload={"client_message_id": client_message_id},
            )
            connection.execute(
                """
                UPDATE turn_intents SET state = 'retry_authorized'
                WHERE run_id = ? AND client_message_id = ?
                """,
                (run_id, client_message_id),
            )
            connection.commit()

    def quarantine_run(
        self,
        run_id: str,
        *,
        kind: str,
        reason: str,
        thread_id: str | None,
        turn_id: str | None,
        payload: Mapping[str, Any],
        audit_kind: str,
        lease: LeaseToken,
    ) -> None:
        if not kind or not reason or not audit_kind.startswith("audit_"):
            raise ValueError("quarantine kind, reason, and audit kind are required")
        raw_payload = dict(payload)
        raw_payload_json = _canonical_json(raw_payload)
        if len(raw_payload_json.encode("utf-8")) > MAX_AUDIT_PAYLOAD_BYTES:
            raise HotJoinError("quarantine payload exceeds the protected limit")
        protected_payload = _redact_sensitive_object(raw_payload)
        if not isinstance(protected_payload, dict):
            raise HotJoinError("quarantine payload projection is not an object")
        payload_json = _canonical_json(protected_payload)
        raw_payload_sha256 = hashlib.sha256(
            raw_payload_json.encode("utf-8")
        ).hexdigest()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._run_row(connection, run_id)
            self._require_lease(connection, run_id, lease)
            existing = connection.execute(
                "SELECT * FROM run_quarantines WHERE run_id = ?", (run_id,)
            ).fetchone()
            if existing is not None:
                connection.commit()
                return
            # Reserve one immutable fatal audit receipt beyond the ordinary
            # audit-event budget. Otherwise routine audit exhaustion could
            # prevent the safety quarantine itself from becoming durable.
            self._append_event(
                connection,
                run_id=run_id,
                kind=audit_kind,
                actor="app_server" if kind == "model_rerouted" else "adapter",
                payload=protected_payload,
            )
            created_at_utc = _utc_now()
            self._append_event(
                connection,
                run_id=run_id,
                kind="run_quarantined",
                actor="adapter",
                payload={
                    "kind": kind,
                    "reason": reason,
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                    "raw_payload_sha256": raw_payload_sha256,
                },
            )
            connection.execute(
                """
                INSERT INTO run_quarantines(
                    run_id, kind, reason, thread_id, turn_id, payload_json,
                    created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    kind,
                    reason,
                    thread_id,
                    turn_id,
                    payload_json,
                    created_at_utc,
                ),
            )
            connection.commit()

    def assert_not_quarantined(self, run_id: str) -> None:
        with self._connect() as connection:
            self._run_row(connection, run_id)
            row = connection.execute(
                "SELECT kind, reason FROM run_quarantines WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is not None:
            raise HotJoinError(
                f"run is permanently quarantined ({row['kind']}): {row['reason']}"
            )

    def has_expected_interrupt(self, run_id: str, turn_id: str) -> bool:
        with self._connect() as connection:
            self._run_row(connection, run_id)
            row = connection.execute(
                """
                SELECT 1 FROM messages
                WHERE run_id = ? AND turn_id = ? AND state = 'interrupting'
                LIMIT 1
                """,
                (run_id, turn_id),
            ).fetchone()
        return row is not None

    def record_audit_event(
        self,
        run_id: str,
        *,
        kind: str,
        payload: Mapping[str, Any],
        actor: str,
        lease: LeaseToken,
    ) -> None:
        if not kind.startswith("audit_"):
            raise ValueError("protected audit event kind must start with audit_")
        payload_json = _canonical_json(dict(payload))
        if len(payload_json.encode("utf-8")) > MAX_AUDIT_PAYLOAD_BYTES:
            raise HotJoinError(f"audit payload exceeds {MAX_AUDIT_PAYLOAD_BYTES} bytes")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._run_row(connection, run_id)
            self._require_lease(connection, run_id, lease)
            count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) AS count FROM events
                    WHERE run_id = ? AND kind LIKE 'audit_%'
                    """,
                    (run_id,),
                ).fetchone()["count"]
            )
            if count >= MAX_AUDIT_EVENTS_PER_RUN:
                raise HotJoinError("protected audit event budget exhausted")
            self._append_event(
                connection,
                run_id=run_id,
                kind=kind,
                actor=actor,
                payload=dict(payload),
            )
            connection.commit()

    def finalize_turn(
        self,
        run_id: str,
        *,
        turn_id: str,
        status: str,
        assistant_message: str,
        error: object | None,
        terminal_audit: Mapping[str, Any],
        lease: LeaseToken,
    ) -> int:
        """Atomically close a turn, queue interrupted text, and receipt replies."""

        if status not in {"completed", "interrupted", "failed"}:
            raise ValueError("terminal turn status is invalid")
        terminal_payload = dict(terminal_audit)
        if (
            terminal_payload.get("id") != turn_id
            or terminal_payload.get("status") != status
        ):
            raise ValueError("terminal audit must bind the exact turn id and status")
        if terminal_payload.get("error") != error:
            raise ValueError("terminal audit error must match the projected failure")
        raw_error_commitment = hashlib.sha256(
            _canonical_json(error).encode("utf-8")
        ).hexdigest()
        redacted_error = _redact_sensitive_object(error)
        redacted_terminal_payload = dict(terminal_payload)
        redacted_terminal_payload["error"] = redacted_error
        redacted_terminal_payload["error_sha256"] = raw_error_commitment
        terminal_payload = redacted_terminal_payload
        if (
            len(_canonical_json(terminal_payload).encode("utf-8"))
            > MAX_AUDIT_PAYLOAD_BYTES
        ):
            raise HotJoinError("terminal audit payload exceeds the protected limit")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = self._run_row(connection, run_id)
            self._require_lease(connection, run_id, lease)
            if run["active_turn_id"] not in {None, turn_id}:
                raise HotJoinError("terminal event conflicts with another active turn")
            delivered_rows = connection.execute(
                """
                SELECT message_id FROM messages
                WHERE run_id = ? AND turn_id = ? AND state = 'delivered'
                ORDER BY accepted_sequence
                """,
                (run_id, turn_id),
            ).fetchall()
            interrupted_rows = connection.execute(
                """
                SELECT message_id FROM messages
                WHERE run_id = ? AND turn_id = ? AND state = 'interrupting'
                ORDER BY accepted_sequence
                """,
                (run_id, turn_id),
            ).fetchall()
            deferred_rows = connection.execute(
                """
                SELECT message_id FROM messages
                WHERE run_id = ? AND turn_id = ? AND state = 'deferred'
                ORDER BY accepted_sequence
                """,
                (run_id, turn_id),
            ).fetchall()
            message_ids = [str(row["message_id"]) for row in delivered_rows]
            interrupted_ids = [str(row["message_id"]) for row in interrupted_rows]
            deferred_ids = [str(row["message_id"]) for row in deferred_rows]
            if (
                run["active_turn_id"] != turn_id
                and not message_ids
                and not interrupted_ids
                and not deferred_ids
            ):
                connection.commit()
                return 0
            self._append_event(
                connection,
                run_id=run_id,
                kind="turn_terminal",
                actor="app_server",
                payload={
                    "error": redacted_error,
                    "error_sha256": raw_error_commitment,
                    "status": status,
                    "turn_id": turn_id,
                },
            )
            audit_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) AS count FROM events
                    WHERE run_id = ? AND kind LIKE 'audit_%'
                    """,
                    (run_id,),
                ).fetchone()["count"]
            )
            if audit_count >= MAX_AUDIT_EVENTS_PER_RUN:
                raise HotJoinError("protected audit event budget exhausted")
            self._append_event(
                connection,
                run_id=run_id,
                kind="audit_turn_terminal",
                actor="app_server",
                payload={"thread_id": run["thread_id"], "turn": terminal_payload},
            )
            for message_id in interrupted_ids:
                self._append_event(
                    connection,
                    run_id=run_id,
                    kind="interrupted_turn_ended",
                    actor="app_server",
                    payload={"message_id": message_id, "turn_id": turn_id},
                )
            for message_id in deferred_ids:
                self._append_event(
                    connection,
                    run_id=run_id,
                    kind="deferred_turn_ended",
                    actor="app_server",
                    payload={"message_id": message_id, "turn_id": turn_id},
                )
            self._append_event(
                connection,
                run_id=run_id,
                kind={
                    "completed": "assistant_response_completed",
                    "failed": "assistant_response_failed",
                    "interrupted": "assistant_response_interrupted",
                }[status],
                actor="app_server",
                payload={
                    "assistant_message": assistant_message,
                    "error": redacted_error,
                    "error_sha256": raw_error_commitment,
                    "message_ids": message_ids,
                    "status": status,
                    "turn_id": turn_id,
                },
            )
            queued_ids = [*interrupted_ids, *deferred_ids]
            if queued_ids:
                placeholders = ",".join("?" for _ in queued_ids)
                connection.execute(
                    f"""
                    UPDATE messages
                    SET state = 'queued', attempt_id = NULL, turn_id = NULL
                    WHERE message_id IN ({placeholders})
                    """,
                    queued_ids,
                )
            if message_ids:
                placeholders = ",".join("?" for _ in message_ids)
                connection.execute(
                    f"UPDATE messages SET state = ? WHERE message_id IN ({placeholders})",
                    [
                        {
                            "completed": "responded",
                            "failed": "failed",
                            "interrupted": "interrupted",
                        }[status],
                        *message_ids,
                    ],
                )
            connection.execute(
                "UPDATE runs SET active_turn_id = NULL WHERE run_id = ? AND active_turn_id = ?",
                (run_id, turn_id),
            )
            connection.execute(
                """
                UPDATE turn_intents SET state = ?
                WHERE run_id = ? AND turn_id = ? AND state = 'active'
                """,
                (status, run_id, turn_id),
            )
            connection.commit()
        return len(queued_ids)

    def bind_thread(self, run_id: str, thread_id: str, *, lease: LeaseToken) -> None:
        if not isinstance(thread_id, str) or not thread_id:
            raise ValueError("thread_id must be non-empty")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._run_row(connection, run_id)
            self._require_lease(connection, run_id, lease)
            existing = row["thread_id"]
            if existing is not None and existing != thread_id:
                raise HotJoinError(
                    f"run is already bound to a different app-server thread: {existing}"
                )
            if existing is None:
                self._append_event(
                    connection,
                    run_id=run_id,
                    kind="thread_bound",
                    actor="adapter",
                    payload={"thread_id": thread_id},
                )
                connection.execute(
                    "UPDATE runs SET thread_id = ? WHERE run_id = ?",
                    (thread_id, run_id),
                )
            connection.commit()

    def bind_generator_fingerprint(
        self,
        run_id: str,
        *,
        fingerprint: str,
        descriptor: Mapping[str, Any],
    ) -> None:
        if re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
            raise ValueError("generator fingerprint must be a SHA-256 hex digest")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._run_row(connection, run_id)
            existing = row["generator_fingerprint"]
            if existing is not None and existing != fingerprint:
                raise IdempotencyConflict(
                    "run id is already bound to a different generator configuration"
                )
            if existing is None:
                self._append_event(
                    connection,
                    run_id=run_id,
                    kind="generator_configuration_bound",
                    actor="adapter",
                    payload={
                        "descriptor": dict(descriptor),
                        "fingerprint": fingerprint,
                    },
                )
                connection.execute(
                    "UPDATE runs SET generator_fingerprint = ? WHERE run_id = ?",
                    (fingerprint, run_id),
                )
            connection.commit()

    def set_active_turn(self, run_id: str, turn_id: str, *, lease: LeaseToken) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = self._run_row(connection, run_id)
            self._require_lease(connection, run_id, lease)
            if run["active_turn_id"] == turn_id:
                connection.commit()
                return
            if run["active_turn_id"] is not None:
                raise HotJoinError(
                    "cannot replace a different durable active generator turn"
                )
            self._append_event(
                connection,
                run_id=run_id,
                kind="turn_started",
                actor="app_server",
                payload={"turn_id": turn_id},
            )
            connection.execute(
                "UPDATE runs SET active_turn_id = ?, generation = generation + 1 WHERE run_id = ?",
                (turn_id, run_id),
            )
            connection.commit()

    def acquire_lease(
        self,
        run_id: str,
        owner_id: str,
        *,
        ttl_seconds: float = DEFAULT_LEASE_SECONDS,
    ) -> LeaseToken:
        if ttl_seconds <= 0:
            raise ValueError("lease ttl must be positive")
        now = time.time()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._run_row(connection, run_id)
            existing = connection.execute(
                "SELECT * FROM leases WHERE run_id = ?", (run_id,)
            ).fetchone()
            if (
                existing is not None
                and existing["owner_id"] != owner_id
                and float(existing["expires_epoch"]) > now
            ):
                raise LeaseBusy(
                    f"run {run_id} is owned by adapter {existing['owner_id']}"
                )
            if existing is None:
                fence = 1
            elif (
                existing["owner_id"] == owner_id
                and float(existing["expires_epoch"]) > now
            ):
                fence = int(existing["fence"])
            else:
                fence = int(existing["fence"]) + 1
            connection.execute(
                """
                INSERT INTO leases(run_id, owner_id, fence, expires_epoch)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    owner_id = excluded.owner_id,
                    fence = excluded.fence,
                    expires_epoch = excluded.expires_epoch
                """,
                (run_id, owner_id, fence, now + ttl_seconds),
            )
            connection.commit()
        return LeaseToken(owner_id=owner_id, fence=fence)

    def renew_lease(
        self,
        run_id: str,
        lease: LeaseToken,
        *,
        ttl_seconds: float = DEFAULT_LEASE_SECONDS,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("lease ttl must be positive")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_lease(connection, run_id, lease)
            connection.execute(
                """
                UPDATE leases SET expires_epoch = ?
                WHERE run_id = ? AND owner_id = ? AND fence = ?
                """,
                (time.time() + ttl_seconds, run_id, lease.owner_id, lease.fence),
            )
            connection.commit()

    def release_lease(self, run_id: str, lease: LeaseToken) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE leases SET expires_epoch = 0
                WHERE run_id = ? AND owner_id = ? AND fence = ?
                """,
                (run_id, lease.owner_id, lease.fence),
            )
            connection.commit()

    def assert_lease(self, run_id: str, lease: LeaseToken) -> None:
        with self._connect() as connection:
            self._run_row(connection, run_id)
            self._require_lease(connection, run_id, lease)

    def status(self, run_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN")
            row = self._run_row(connection, run_id)
            counts = {
                state: int(
                    connection.execute(
                        "SELECT COUNT(*) AS count FROM messages WHERE run_id = ? AND state = ?",
                        (run_id, state),
                    ).fetchone()["count"]
                )
                for state in sorted(MESSAGE_STATES)
            }
            intent_counts = {
                str(intent_row["state"]): int(intent_row["count"])
                for intent_row in connection.execute(
                    """
                    SELECT
                        CASE
                            WHEN state = 'dispatching' AND dispatch_count = 0
                            THEN 'prepared'
                            ELSE state
                        END AS state,
                        COUNT(*) AS count
                    FROM turn_intents
                    WHERE run_id = ?
                    GROUP BY 1 ORDER BY 1
                    """,
                    (run_id,),
                ).fetchall()
            }
            quarantine_row = connection.execute(
                "SELECT * FROM run_quarantines WHERE run_id = ?", (run_id,)
            ).fetchone()
            quarantine = (
                {
                    "created_at_utc": quarantine_row["created_at_utc"],
                    "kind": quarantine_row["kind"],
                    "payload": _json_loads_strict(quarantine_row["payload_json"]),
                    "reason": quarantine_row["reason"],
                    "thread_id": quarantine_row["thread_id"],
                    "turn_id": quarantine_row["turn_id"],
                }
                if quarantine_row is not None
                else None
            )
            connection.commit()
        return {
            "run_id": row["run_id"],
            "problem_id": row["problem_id"],
            "thread_id": row["thread_id"],
            "active_turn_id": row["active_turn_id"],
            "generator_fingerprint": row["generator_fingerprint"],
            "generation": row["generation"],
            "last_sequence": row["last_sequence"],
            "head_digest": row["head_digest"],
            "message_counts": counts,
            "quarantine": quarantine,
            "turn_intent_counts": intent_counts,
        }

    def events(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Read ordered receipts without mutating or acknowledging the ledger."""

        if after_sequence < 0:
            raise ValueError("after_sequence must be non-negative")
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self._connect() as connection:
            self._run_row(connection, run_id)
            rows = connection.execute(
                """
                SELECT sequence, event_id, kind, actor, created_at_utc,
                       payload_json, previous_digest, digest
                FROM events
                WHERE run_id = ? AND sequence > ?
                ORDER BY sequence
                LIMIT ?
                """,
                (run_id, after_sequence, limit),
            ).fetchall()
        return [
            {
                "sequence": int(row["sequence"]),
                "event_id": row["event_id"],
                "kind": row["kind"],
                "actor": row["actor"],
                "created_at_utc": row["created_at_utc"],
                "payload": _json_loads_strict(row["payload_json"]),
                "previous_digest": row["previous_digest"],
                "digest": row["digest"],
            }
            for row in rows
        ]

    def verify_chain(self, run_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN")
            run = self._run_row(connection, run_id)
            events = connection.execute(
                "SELECT * FROM events WHERE run_id = ? ORDER BY sequence", (run_id,)
            ).fetchall()
            connection.commit()
        previous = ZERO_DIGEST
        for row in events:
            if row["previous_digest"] != previous:
                raise HotJoinError(
                    f"event chain previous digest mismatch at sequence {row['sequence']}"
                )
            computed = _event_digest(
                run_id=run_id,
                event_id=row["event_id"],
                kind=row["kind"],
                actor=row["actor"],
                created_at_utc=row["created_at_utc"],
                payload_json=row["payload_json"],
                previous_digest=row["previous_digest"],
            )
            if computed != row["digest"]:
                raise HotJoinError(
                    f"event digest mismatch at sequence {row['sequence']}"
                )
            previous = computed
        if previous != run["head_digest"]:
            raise HotJoinError("run head digest does not match append-only event chain")
        return {
            "run_id": run_id,
            "event_count": len(events),
            "head_digest": previous,
            "valid": True,
        }


@dataclass(frozen=True)
class CapabilityReceipt:
    codex_version: str
    schema_digest: str
    required_methods: tuple[str, ...]
    resume_supports_provider_model_fallback: bool = False


def _walk_json(value: object) -> Iterable[object]:
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def preflight_app_server(
    codex_bin: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    timeout_seconds: float = 30.0,
) -> CapabilityReceipt:
    """Validate the installed binary's generated schema before any model turn."""

    try:
        version_result = runner(
            [codex_bin, "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CapabilityError(
            f"cannot execute Codex CLI for hot-join preflight: {exc}"
        ) from exc
    version = version_result.stdout.strip()
    if not version:
        raise CapabilityError("Codex CLI returned an empty version string")

    with tempfile.TemporaryDirectory(prefix="rethlas-app-server-schema-") as temp_dir:
        try:
            runner(
                [
                    codex_bin,
                    "app-server",
                    "generate-json-schema",
                    "--experimental",
                    "--out",
                    temp_dir,
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise CapabilityError(
                f"Codex app-server schema generation failed before model startup: {exc}"
            ) from exc
        schema_path = Path(temp_dir) / "codex_app_server_protocol.v2.schemas.json"
        if not schema_path.is_file():
            raise CapabilityError(
                "Codex app-server did not generate its v2 schema bundle"
            )
        raw = schema_path.read_bytes()
        try:
            schema = _json_loads_strict(raw.decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise CapabilityError(
                f"Codex app-server schema is invalid JSON: {exc}"
            ) from exc

    required_methods = REQUIRED_APP_SERVER_METHODS
    discovered_methods: set[str] = set()
    for node in _walk_json(schema):
        if not isinstance(node, dict):
            continue
        method = node.get("method")
        if isinstance(method, dict):
            values = method.get("enum")
            if isinstance(values, list):
                discovered_methods.update(
                    value for value in values if isinstance(value, str)
                )
    missing = [
        method for method in required_methods if method not in discovered_methods
    ]
    if missing:
        raise CapabilityError(
            "installed Codex app-server lacks required hot-join RPCs: "
            + ", ".join(missing)
        )

    definitions = schema.get("definitions") if isinstance(schema, dict) else None
    thread_start = (
        definitions.get("ThreadStartParams") if isinstance(definitions, dict) else None
    )
    thread_start_properties = (
        thread_start.get("properties", {}) if isinstance(thread_start, dict) else {}
    )
    if not isinstance(thread_start_properties, dict) or not {
        "approvalPolicy",
        "allowProviderModelFallback",
        "config",
        "cwd",
        "ephemeral",
        "model",
        "sandbox",
    }.issubset(thread_start_properties):
        raise CapabilityError(
            "Codex app-server thread/start cannot establish the required generator configuration"
        )
    steer = (
        definitions.get("TurnSteerParams") if isinstance(definitions, dict) else None
    )
    if not isinstance(steer, dict):
        raise CapabilityError("Codex app-server schema lacks TurnSteerParams")
    required = set(steer.get("required", []))
    properties = steer.get("properties", {})
    if (
        not {"threadId", "expectedTurnId", "input"}.issubset(required)
        or not isinstance(properties, dict)
        or "clientUserMessageId" not in properties
    ):
        raise CapabilityError(
            "Codex app-server turn/steer lacks expectedTurnId/input/clientUserMessageId contract"
        )
    steer_response = (
        definitions.get("TurnSteerResponse") if isinstance(definitions, dict) else None
    )
    steer_response_properties = (
        steer_response.get("properties", {}) if isinstance(steer_response, dict) else {}
    )
    steer_response_required = (
        set(steer_response.get("required", []))
        if isinstance(steer_response, dict)
        else set()
    )
    if (
        not isinstance(steer_response_properties, dict)
        or "turnId" not in steer_response_properties
        or "turnId" not in steer_response_required
    ):
        raise CapabilityError("Codex app-server turn/steer cannot confirm its turn id")
    start = (
        definitions.get("TurnStartParams") if isinstance(definitions, dict) else None
    )
    start_properties = start.get("properties", {}) if isinstance(start, dict) else {}
    start_required = (
        set(start.get("required", [])) if isinstance(start, dict) else set()
    )
    if (
        not isinstance(start_properties, dict)
        or not {
            "approvalPolicy",
            "clientUserMessageId",
            "cwd",
            "effort",
            "model",
        }.issubset(start_properties)
        or not {"threadId", "input"}.issubset(start_required)
    ):
        raise CapabilityError(
            "Codex app-server turn/start lacks idempotent user-message contract"
        )
    interrupt = (
        definitions.get("TurnInterruptParams")
        if isinstance(definitions, dict)
        else None
    )
    interrupt_required = (
        set(interrupt.get("required", [])) if isinstance(interrupt, dict) else set()
    )
    if not {"threadId", "turnId"}.issubset(interrupt_required):
        raise CapabilityError(
            "Codex app-server turn/interrupt lacks exact thread/turn preconditions"
        )
    resume = (
        definitions.get("ThreadResumeParams") if isinstance(definitions, dict) else None
    )
    resume_properties = resume.get("properties", {}) if isinstance(resume, dict) else {}
    if not isinstance(resume_properties, dict) or not {
        "approvalPolicy",
        "config",
        "cwd",
        "model",
        "sandbox",
        "threadId",
    }.issubset(resume_properties):
        raise CapabilityError(
            "Codex app-server thread/resume cannot restore generator configuration safely"
        )
    read = (
        definitions.get("ThreadReadParams") if isinstance(definitions, dict) else None
    )
    read_properties = read.get("properties", {}) if isinstance(read, dict) else {}
    if not isinstance(read_properties, dict) or not {
        "threadId",
        "includeTurns",
    }.issubset(read_properties):
        raise CapabilityError(
            "Codex app-server thread/read cannot hydrate turn history"
        )
    user_contract = any(
        isinstance(node, dict)
        and node.get("title") == "UserMessageThreadItem"
        and isinstance(node.get("properties"), dict)
        and "clientId" in node["properties"]
        for node in _walk_json(schema)
    )
    turn = definitions.get("Turn") if isinstance(definitions, dict) else None
    turn_properties = turn.get("properties", {}) if isinstance(turn, dict) else {}
    if (
        not user_contract
        or not isinstance(turn_properties, dict)
        or not {"id", "items", "status"}.issubset(turn_properties)
    ):
        raise CapabilityError(
            "Codex app-server thread history lacks durable message reconciliation fields"
        )
    sandbox = definitions.get("SandboxMode") if isinstance(definitions, dict) else None
    sandbox_values = (
        set(sandbox.get("enum", [])) if isinstance(sandbox, dict) else set()
    )
    if "workspace-write" not in sandbox_values:
        raise CapabilityError(
            "Codex app-server schema does not support the generator workspace sandbox"
        )

    def require_definition(
        name: str,
        *,
        properties: set[str] = frozenset(),
        required_fields: set[str] = frozenset(),
    ) -> dict[str, Any]:
        value = definitions.get(name) if isinstance(definitions, dict) else None
        value_properties = (
            value.get("properties", {}) if isinstance(value, dict) else {}
        )
        value_required = (
            set(value.get("required", [])) if isinstance(value, dict) else set()
        )
        if (
            not isinstance(value, dict)
            or not isinstance(value_properties, dict)
            or not properties.issubset(value_properties)
            or not required_fields.issubset(value_required)
        ):
            raise CapabilityError(f"Codex app-server schema lacks the {name} contract")
        return value

    def require_property_reference(name: str, field: str, target: str) -> None:
        definition = require_definition(name, properties={field})
        property_schema = definition["properties"][field]
        expected_reference = f"#/definitions/{target}"
        if expected_reference not in _canonical_json(property_schema):
            raise CapabilityError(
                f"Codex app-server schema does not bind {name}.{field} to {target}"
            )

    def require_property_type(name: str, field: str, expected_type: str) -> None:
        definition = require_definition(name, properties={field})
        property_schema = definition["properties"][field]
        declared = (
            property_schema.get("type") if isinstance(property_schema, dict) else None
        )
        declared_types = set(declared) if isinstance(declared, list) else {declared}
        if expected_type not in declared_types:
            raise CapabilityError(
                f"Codex app-server schema gives {name}.{field} the wrong type"
            )

    require_definition(
        "InitializeParams",
        properties={"capabilities", "clientInfo"},
        required_fields={"clientInfo"},
    )
    require_definition("InitializeCapabilities", properties={"experimentalApi"})
    require_definition(
        "ClientInfo",
        properties={"name", "title", "version"},
        required_fields={"name", "version"},
    )
    require_property_reference("InitializeParams", "clientInfo", "ClientInfo")
    require_property_reference(
        "InitializeParams", "capabilities", "InitializeCapabilities"
    )
    require_definition(
        "ModelListParams", properties={"cursor", "includeHidden", "limit"}
    )
    require_definition(
        "ModelListResponse", properties={"data", "nextCursor"}, required_fields={"data"}
    )
    require_definition(
        "Model",
        properties={"id", "model", "supportedReasoningEfforts"},
        required_fields={"id", "model", "supportedReasoningEfforts"},
    )
    require_definition(
        "ReasoningEffortOption",
        properties={"reasoningEffort"},
        required_fields={"reasoningEffort"},
    )
    require_property_type("ModelListResponse", "data", "array")
    require_property_reference("ModelListResponse", "data", "Model")
    require_property_type("Model", "id", "string")
    require_property_type("Model", "model", "string")
    require_property_type("Model", "supportedReasoningEfforts", "array")
    require_property_reference(
        "Model", "supportedReasoningEfforts", "ReasoningEffortOption"
    )
    require_property_reference(
        "ReasoningEffortOption", "reasoningEffort", "ReasoningEffort"
    )
    for response_name in ("ThreadStartResponse", "ThreadResumeResponse"):
        require_definition(
            response_name,
            properties={
                "approvalPolicy",
                "cwd",
                "model",
                "reasoningEffort",
                "runtimeWorkspaceRoots",
                "sandbox",
                "thread",
            },
            required_fields={"approvalPolicy", "cwd", "model", "sandbox", "thread"},
        )
        require_property_reference(response_name, "thread", "Thread")
        require_property_reference(response_name, "sandbox", "SandboxPolicy")
        require_property_reference(response_name, "cwd", "AbsolutePathBuf")
        require_property_type(response_name, "model", "string")
        require_property_type(response_name, "runtimeWorkspaceRoots", "array")
        require_property_reference(
            response_name, "runtimeWorkspaceRoots", "AbsolutePathBuf"
        )
    require_definition(
        "ThreadReadResponse", properties={"thread"}, required_fields={"thread"}
    )
    require_definition(
        "Thread",
        properties={"cwd", "ephemeral", "id", "turns"},
        required_fields={"cwd", "ephemeral", "id", "turns"},
    )
    require_property_reference("ThreadReadResponse", "thread", "Thread")
    require_property_type("Thread", "id", "string")
    require_property_reference("Thread", "cwd", "AbsolutePathBuf")
    require_property_type("Thread", "ephemeral", "boolean")
    require_property_type("Thread", "turns", "array")
    require_property_reference("Thread", "turns", "Turn")
    require_definition(
        "TurnStartResponse", properties={"turn"}, required_fields={"turn"}
    )
    require_property_reference("TurnStartResponse", "turn", "Turn")
    require_definition("TurnInterruptResponse")
    require_definition(
        "Turn",
        properties={"durationMs", "error", "id", "items", "status"},
        required_fields={"id", "items", "status"},
    )
    require_property_type("Turn", "id", "string")
    require_property_type("Turn", "items", "array")
    require_property_reference("Turn", "items", "ThreadItem")
    require_property_reference("Turn", "status", "TurnStatus")
    require_property_reference("Turn", "error", "TurnError")
    for notification_name, notification_properties in (
        (
            "TurnStartedNotification",
            {"threadId", "turn"},
        ),
        (
            "TurnCompletedNotification",
            {"threadId", "turn"},
        ),
        (
            "ItemCompletedNotification",
            {"completedAtMs", "item", "threadId", "turnId"},
        ),
        (
            "ThreadTokenUsageUpdatedNotification",
            {"threadId", "tokenUsage", "turnId"},
        ),
        (
            "ModelReroutedNotification",
            {"fromModel", "reason", "threadId", "toModel", "turnId"},
        ),
    ):
        require_definition(
            notification_name,
            properties=notification_properties,
            required_fields=notification_properties,
        )
    for notification_name in (
        "TurnStartedNotification",
        "TurnCompletedNotification",
    ):
        require_property_reference(notification_name, "turn", "Turn")
        require_property_type(notification_name, "threadId", "string")
    require_property_reference("ItemCompletedNotification", "item", "ThreadItem")
    require_property_reference(
        "ThreadTokenUsageUpdatedNotification", "tokenUsage", "ThreadTokenUsage"
    )
    require_definition(
        "ThreadTokenUsage",
        properties={"last", "modelContextWindow", "total"},
        required_fields={"last", "total"},
    )
    require_property_reference("ThreadTokenUsage", "last", "TokenUsageBreakdown")
    require_property_reference("ThreadTokenUsage", "total", "TokenUsageBreakdown")
    require_property_type("ThreadTokenUsage", "modelContextWindow", "integer")
    token_fields = {
        "cacheWriteInputTokens",
        "cachedInputTokens",
        "inputTokens",
        "outputTokens",
        "reasoningOutputTokens",
        "totalTokens",
    }
    require_definition(
        "TokenUsageBreakdown",
        properties=token_fields,
        required_fields=token_fields - {"cacheWriteInputTokens"},
    )
    for token_field in token_fields:
        require_property_type("TokenUsageBreakdown", token_field, "integer")
    require_property_reference(
        "ModelReroutedNotification", "reason", "ModelRerouteReason"
    )
    reroute_reason = require_definition("ModelRerouteReason")
    if set(reroute_reason.get("enum", [])) != set(SUPPORTED_MODEL_REROUTE_REASONS):
        raise CapabilityError(
            "Codex app-server model reroute reasons differ from the audited contract"
        )
    for notification_name in (
        "ItemCompletedNotification",
        "ThreadTokenUsageUpdatedNotification",
        "ModelReroutedNotification",
    ):
        require_property_type(notification_name, "threadId", "string")
        require_property_type(notification_name, "turnId", "string")
    require_property_type("TurnSteerResponse", "turnId", "string")
    sandbox_policy = (
        definitions.get("SandboxPolicy") if isinstance(definitions, dict) else None
    )
    workspace_write_contract = None
    if isinstance(sandbox_policy, dict):
        for option in sandbox_policy.get("oneOf", []):
            if (
                isinstance(option, dict)
                and isinstance(option.get("properties"), dict)
                and "workspaceWrite"
                in _canonical_json(option["properties"].get("type"))
            ):
                workspace_write_contract = option
                break
    workspace_properties = (
        workspace_write_contract.get("properties", {})
        if isinstance(workspace_write_contract, dict)
        else {}
    )
    if (
        not isinstance(workspace_properties, dict)
        or workspace_properties.get("networkAccess", {}).get("type") != "boolean"
        or workspace_properties.get("writableRoots", {}).get("type") != "array"
        or "#/definitions/AbsolutePathBuf"
        not in _canonical_json(workspace_properties.get("writableRoots"))
    ):
        raise CapabilityError(
            "Codex app-server cannot attest confined offline workspaceWrite policy"
        )
    return CapabilityReceipt(
        codex_version=version,
        schema_digest=hashlib.sha256(raw).hexdigest(),
        required_methods=required_methods,
        resume_supports_provider_model_fallback=(
            "allowProviderModelFallback" in resume_properties
        ),
    )


class AppServerClient:
    """Strict JSONL RPC client with exactly one stdout reader thread."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        process_factory: Callable[..., Any] = subprocess.Popen,
        rpc_timeout_seconds: float = DEFAULT_RPC_TIMEOUT_SECONDS,
        close_grace_seconds: float = DEFAULT_APP_SERVER_CLOSE_GRACE_SECONDS,
    ) -> None:
        if close_grace_seconds < 0 or close_grace_seconds > 30:
            raise ValueError("app-server close grace must be between 0 and 30 seconds")
        self.command = list(command)
        self.process_factory = process_factory
        self.rpc_timeout_seconds = rpc_timeout_seconds
        self.close_grace_seconds = close_grace_seconds
        self.process: Any | None = None
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._pending: dict[int, queue.Queue[object]] = {}
        self._abandoned_ids: set[int] = set()
        self._notifications: queue.Queue[tuple[dict[str, Any], int]] = queue.Queue(
            maxsize=MAX_QUEUED_NOTIFICATIONS
        )
        self._notification_bytes = 0
        self._next_id = 1
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._fatal: BaseException | None = None
        self._closed = threading.Event()
        self._stderr_tail: list[str] = []

    def start(self) -> None:
        if self.process is not None:
            raise ProtocolError("app-server client was already started")
        try:
            self.process = self.process_factory(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise ProtocolError(f"cannot start Codex app-server: {exc}") from exc
        if self.process.stdin is None or self.process.stdout is None:
            raise ProtocolError("Codex app-server did not expose stdin/stdout pipes")
        self._reader = threading.Thread(
            target=self._read_loop,
            name="rethlas-app-server-jsonl-reader",
            daemon=True,
        )
        self._reader.start()
        if self.process.stderr is not None:
            self._stderr_reader = threading.Thread(
                target=self._stderr_loop,
                name="rethlas-app-server-stderr-reader",
                daemon=True,
            )
            self._stderr_reader.start()

    def _stderr_loop(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        while True:
            line = self.process.stderr.readline(MAX_APP_SERVER_STDERR_LINE_CHARS + 1)
            if line == "":
                return
            oversized_chunk = len(
                line
            ) > MAX_APP_SERVER_STDERR_LINE_CHARS or not line.endswith("\n")
            self._stderr_tail.append(
                _safe_stderr_line(line)
                + (" <truncated-chunk>" if oversized_chunk else "")
            )
            del self._stderr_tail[:-100]

    def _fail(self, error: BaseException) -> None:
        with self._state_lock:
            if self._fatal is None:
                self._fatal = error
            pending = list(self._pending.values())
            self._pending.clear()
        for response_queue in pending:
            response_queue.put(error)
        self._closed.set()

    def _reject_server_request(self, request_id: object, method: str) -> None:
        if isinstance(request_id, bool) or not isinstance(request_id, (int, str)):
            raise ProtocolError("app-server request id must be an integer or string")
        response = {
            "id": request_id,
            "error": {
                "code": -32601,
                "message": (
                    "Rethlas hot-join is noninteractive for server-initiated requests; "
                    f"unsupported method {method}"
                ),
            },
        }
        assert self.process is not None and self.process.stdin is not None
        with self._write_lock:
            self.process.stdin.write(_canonical_json(response) + "\n")
            self.process.stdin.flush()

    def _read_loop(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        try:
            while True:
                raw_line = self.process.stdout.readline(MAX_APP_SERVER_LINE_BYTES + 1)
                if raw_line == "":
                    break
                if not raw_line.endswith("\n"):
                    raise ProtocolError(
                        "app-server emitted an unterminated JSONL record"
                    )
                line_bytes = len(raw_line.encode("utf-8"))
                if line_bytes > MAX_APP_SERVER_LINE_BYTES:
                    raise ProtocolError(
                        f"app-server JSONL record exceeds {MAX_APP_SERVER_LINE_BYTES} bytes"
                    )
                line = raw_line[:-1]
                if not line:
                    raise ProtocolError("app-server emitted an empty JSONL record")
                try:
                    message = _json_loads_strict(line)
                except ValueError as exc:
                    raise ProtocolError(
                        f"app-server emitted invalid JSONL: {exc}"
                    ) from exc
                if not isinstance(message, dict):
                    raise ProtocolError("app-server JSONL record must be an object")
                if "id" in message and isinstance(message.get("method"), str):
                    self._reject_server_request(message["id"], message["method"])
                    continue
                if "id" in message:
                    response_id = message["id"]
                    if isinstance(response_id, bool) or not isinstance(
                        response_id, int
                    ):
                        raise ProtocolError("app-server response id must be an integer")
                    with self._state_lock:
                        response_queue = self._pending.pop(response_id, None)
                        abandoned = response_id in self._abandoned_ids
                        self._abandoned_ids.discard(response_id)
                    if response_queue is None:
                        if abandoned:
                            continue
                        if self._closed.is_set():
                            continue
                        raise ProtocolError(
                            f"app-server returned unknown response id {response_id}"
                        )
                    response_queue.put(message)
                    continue
                if not isinstance(message.get("method"), str):
                    raise ProtocolError("app-server notification must contain a method")
                with self._state_lock:
                    if (
                        self._notification_bytes + line_bytes
                        > MAX_QUEUED_NOTIFICATION_BYTES
                    ):
                        raise ProtocolError(
                            "app-server notification byte backlog overflow"
                        )
                    try:
                        self._notifications.put_nowait((message, line_bytes))
                    except queue.Full as exc:
                        raise ProtocolError(
                            "app-server notification count backlog overflow"
                        ) from exc
                    self._notification_bytes += line_bytes
            if self._closed.is_set():
                return
            raise ProtocolError("app-server stdout closed")
        except BaseException as exc:  # one reader owns all transport failure fan-out
            self._fail(exc)

    def _check_live(self) -> None:
        if self._fatal is not None:
            raise ProtocolError(str(self._fatal)) from self._fatal
        if self.process is None:
            raise ProtocolError("app-server client is not started")
        if self.process.poll() is not None:
            detail = "\n".join(self._stderr_tail[-20:])
            raise ProtocolError(
                f"app-server exited with code {self.process.returncode}"
                + (f": {detail}" if detail else "")
            )

    def call(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> Any:
        self._check_live()
        response_queue: queue.Queue[object] = queue.Queue(maxsize=1)
        with self._state_lock:
            if self._fatal is not None:
                raise ProtocolError(str(self._fatal)) from self._fatal
            request_id = self._next_id
            self._next_id += 1
            self._pending[request_id] = response_queue
        request = {"id": request_id, "method": method, "params": dict(params)}
        assert self.process is not None and self.process.stdin is not None
        try:
            with self._write_lock:
                self.process.stdin.write(_canonical_json(request) + "\n")
                self.process.stdin.flush()
        except (OSError, ValueError) as exc:
            with self._state_lock:
                self._pending.pop(request_id, None)
            error = ProtocolError(f"cannot write app-server request {method}: {exc}")
            self._fail(error)
            raise error from exc
        try:
            timeout = (
                self.rpc_timeout_seconds if timeout_seconds is None else timeout_seconds
            )
            response = response_queue.get(timeout=timeout)
        except queue.Empty as exc:
            with self._state_lock:
                self._pending.pop(request_id, None)
                self._abandoned_ids.add(request_id)
                # A bounded set prevents a malicious or broken server from
                # growing memory through permanently missing late responses.
                if len(self._abandoned_ids) > 4096:
                    self._abandoned_ids.remove(min(self._abandoned_ids))
            raise ProtocolError(f"app-server RPC {method} timed out") from exc
        if isinstance(response, BaseException):
            raise ProtocolError(str(response)) from response
        if not isinstance(response, dict):
            raise ProtocolError("app-server response must be an object")
        response_fields = set(response)
        if response_fields == {"id", "result"}:
            return response["result"]
        if response_fields == {"id", "error"}:
            error = response["error"]
            if (
                not isinstance(error, dict)
                or isinstance(error.get("code"), bool)
                or not isinstance(error.get("code"), int)
                or not isinstance(error.get("message"), str)
                or not error["message"]
            ):
                raise ProtocolError(
                    "app-server RPC error response has an invalid error object"
                )
            raise RpcError(method, error)
        raise ProtocolError(
            "app-server response must be exactly {id,result} xor {id,error}"
        )

    def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        self._check_live()
        request = {"method": method, "params": dict(params or {})}
        assert self.process is not None and self.process.stdin is not None
        with self._write_lock:
            self.process.stdin.write(_canonical_json(request) + "\n")
            self.process.stdin.flush()

    def next_notification(self, timeout_seconds: float) -> dict[str, Any] | None:
        try:
            notification, size = self._notifications.get(timeout=timeout_seconds)
            with self._state_lock:
                self._notification_bytes = max(0, self._notification_bytes - size)
            return notification
        except queue.Empty:
            self._check_live()
            return None

    def initialize(self) -> Any:
        result = self.call(
            "initialize",
            {
                "clientInfo": {
                    "name": "rethlas-hotjoin-adapter",
                    "title": "Rethlas Hot Join Adapter",
                    "version": "1",
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        # Experimental v2 defines the request but no ``initialized`` client
        # notification.  Do not emit an untyped legacy protocol message.
        if not isinstance(result, dict):
            raise ProtocolError("app-server initialize result must be an object")
        return result

    def close(self) -> None:
        process = self.process
        if process is None:
            return
        self._closed.set()
        with self._state_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for response_queue in pending:
            response_queue.put(ProtocolError("app-server client closed"))
        try:
            if process.stdin is not None:
                process.stdin.close()
        except OSError:
            pass
        if process.poll() is None:
            try:
                process.wait(timeout=self.close_grace_seconds)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        if (
            self._reader is not None
            and self._reader is not threading.current_thread()
            and self._reader.is_alive()
        ):
            self._reader.join(timeout=5)
        if (
            self._stderr_reader is not None
            and self._stderr_reader is not threading.current_thread()
            and self._stderr_reader.is_alive()
        ):
            self._stderr_reader.join(timeout=5)
        self._closed.set()

    def __enter__(self) -> AppServerClient:
        self.start()
        try:
            self.initialize()
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _extract_identifier(payload: object, *path: str) -> str | None:
    current = payload
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current if isinstance(current, str) and current else None


def _visible_client_message_ids(payload: object) -> set[str]:
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        return set()
    return {
        client_id
        for item in payload["items"]
        if isinstance(item, dict) and item.get("type") == "userMessage"
        if isinstance((client_id := item.get("clientId")), str) and client_id
    }


def _turn_id_for_client_message(payload: object, client_message_id: str) -> str | None:
    for record in _turn_records(payload):
        turn_id = record.get("id")
        if (
            isinstance(turn_id, str)
            and turn_id
            and client_message_id in _visible_client_message_ids(record)
        ):
            return turn_id
    return None


def _turn_records_for_client_message(
    payload: object, client_message_id: str
) -> list[dict[str, Any]]:
    return [
        record
        for record in _turn_records(payload)
        if client_message_id in _visible_client_message_ids(record)
    ]


def _turn_record(payload: object, turn_id: str) -> dict[str, Any] | None:
    for record in _turn_records(payload):
        if record.get("id") == turn_id:
            return record
    return None


def _turn_records(payload: object) -> list[dict[str, Any]]:
    turns = payload.get("turns") if isinstance(payload, dict) else None
    if not isinstance(turns, list):
        return []
    records: list[dict[str, Any]] = []
    for node in turns:
        if (
            isinstance(node, dict)
            and isinstance(node.get("id"), str)
            and isinstance(node.get("items"), list)
            and node.get("status")
            in {"completed", "interrupted", "failed", "inProgress"}
        ):
            records.append(node)
    return records


def _assistant_text(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    direct_item = payload.get("item")
    if isinstance(direct_item, dict):
        items: list[object] = [direct_item]
    elif isinstance(payload.get("items"), list):
        items = payload["items"]
    elif payload.get("type") == "agentMessage":
        items = [payload]
    else:
        items = []
    candidates = [
        text
        for item in items
        if isinstance(item, dict) and item.get("type") == "agentMessage"
        if isinstance((text := item.get("text")), str) and text
    ]
    return candidates[-1] if candidates else ""


def _is_nonsteerable_turn_error(error: object) -> bool:
    return any(
        isinstance(node, dict) and "activeTurnNotSteerable" in node
        for node in _walk_json(error)
    )


def _terminal_audit(turn: object) -> dict[str, Any]:
    if not isinstance(turn, dict):
        raise ProtocolError("terminal turn payload must be an object")
    turn_id = turn.get("id")
    status = turn.get("status")
    if not isinstance(turn_id, str) or status not in {
        "completed",
        "failed",
        "interrupted",
    }:
        raise ProtocolError("terminal turn payload has invalid identity or status")
    duration = turn.get("durationMs")
    if duration is not None and (
        isinstance(duration, bool) or not isinstance(duration, int) or duration < 0
    ):
        raise ProtocolError("terminal turn durationMs must be non-negative or null")
    for timestamp_name in ("startedAt", "completedAt"):
        timestamp = turn.get(timestamp_name)
        if timestamp is not None and (
            isinstance(timestamp, bool)
            or not isinstance(timestamp, int)
            or timestamp < 0
        ):
            raise ProtocolError(
                f"terminal turn {timestamp_name} must be non-negative or null"
            )
    return {
        "completedAt": turn.get("completedAt"),
        "durationMs": duration,
        "error": turn.get("error"),
        "id": turn_id,
        "raw_turn_sha256": hashlib.sha256(
            _canonical_json(turn).encode("utf-8")
        ).hexdigest(),
        "startedAt": turn.get("startedAt"),
        "status": status,
    }


def _canonical_token_usage(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or not {"last", "total"}.issubset(value):
        raise ProtocolError("tokenUsage must contain exact last and total breakdowns")
    if not set(value).issubset({"last", "modelContextWindow", "total"}):
        raise ProtocolError("tokenUsage contains an unaudited field")
    required_breakdown = {
        "cachedInputTokens",
        "inputTokens",
        "outputTokens",
        "reasoningOutputTokens",
        "totalTokens",
    }
    allowed_breakdown = required_breakdown | {"cacheWriteInputTokens"}
    projected: dict[str, Any] = {}
    for label in ("last", "total"):
        breakdown = value.get(label)
        if (
            not isinstance(breakdown, dict)
            or not required_breakdown.issubset(breakdown)
            or not set(breakdown).issubset(allowed_breakdown)
        ):
            raise ProtocolError(f"tokenUsage.{label} has an invalid field set")
        if any(
            isinstance(amount, bool) or not isinstance(amount, int) or amount < 0
            for amount in breakdown.values()
        ):
            raise ProtocolError(
                f"tokenUsage.{label} must contain non-negative integers"
            )
        projected[label] = {key: breakdown[key] for key in sorted(breakdown)}
    context_window = value.get("modelContextWindow")
    if "modelContextWindow" in value:
        if context_window is not None and (
            isinstance(context_window, bool)
            or not isinstance(context_window, int)
            or context_window < 0
        ):
            raise ProtocolError(
                "tokenUsage.modelContextWindow must be non-negative or null"
            )
        projected["modelContextWindow"] = context_window
    return projected


def _token_usage_cumulative_total_changed(
    previous: Mapping[str, Any] | None,
    current: Mapping[str, Any],
) -> bool:
    """Classify one exact token-usage notification without inventing calls.

    App-server attests notifications and cumulative counters, not a schema-level
    inference identifier.  A repeated cumulative total is therefore a duplicate
    notification, while a monotone total whose delta equals ``last`` is an
    observable cumulative-growth sample.  The first observation after adapter
    attachment is counted as growth because no earlier total is available.
    """

    current_last = current["last"]
    current_total = current["total"]
    if set(current_last) != set(current_total):
        raise ProtocolError(
            "tokenUsage.last and tokenUsage.total must use the same fields"
        )
    if previous is None:
        return True

    previous_last = previous["last"]
    previous_total = previous["total"]
    if set(previous_last) != set(previous_total) or set(previous_total) != set(
        current_total
    ):
        raise ProtocolError("tokenUsage cumulative breakdown fields changed")
    if current_total == previous_total:
        if current_last != previous_last:
            raise ProtocolError(
                "tokenUsage repeated a cumulative total with a different last sample"
            )
        return False

    for field, amount in current_total.items():
        delta = amount - previous_total[field]
        if delta < 0:
            raise ProtocolError("tokenUsage cumulative total moved backwards")
        if delta != current_last[field]:
            raise ProtocolError(
                "tokenUsage cumulative growth does not equal the last sample"
            )
    return True


def _add_token_usage_breakdown(
    aggregate: Mapping[str, int] | None,
    increment: Mapping[str, int],
) -> dict[str, int]:
    if aggregate is None:
        return dict(increment)
    if set(aggregate) != set(increment):
        raise ProtocolError("tokenUsage growth aggregate fields changed")
    return {field: aggregate[field] + increment[field] for field in aggregate}


def _validated_thread_read(result: object, expected_thread_id: str) -> dict[str, Any]:
    thread = result.get("thread") if isinstance(result, dict) else None
    if (
        not isinstance(thread, dict)
        or thread.get("id") != expected_thread_id
        or not isinstance(thread.get("turns"), list)
    ):
        raise ProtocolError(
            "thread/read response did not attest the exact thread history"
        )
    return thread


class GeneratorHotJoin:
    """Drive one existing Rethlas generator thread and deliver owner messages."""

    def __init__(
        self,
        ledger: ConversationLedger,
        run_id: str,
        client: AppServerClient,
        *,
        owner_id: str | None = None,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        idle_grace_seconds: float = DEFAULT_IDLE_GRACE_SECONDS,
        resume_supports_provider_model_fallback: bool = False,
        post_terminal_settle_seconds: float = DEFAULT_POST_TERMINAL_SETTLE_SECONDS,
    ) -> None:
        if not 0 <= post_terminal_settle_seconds <= MAX_POST_TERMINAL_SETTLE_SECONDS:
            raise ValueError(
                "post-terminal settle must be between 0 and "
                f"{MAX_POST_TERMINAL_SETTLE_SECONDS} seconds"
            )
        self.ledger = ledger
        self.run_id = _validate_run_id(run_id)
        self.client = client
        self.owner_id = owner_id or f"adapter_{os.getpid()}_{uuid.uuid4().hex}"
        self.poll_seconds = poll_seconds
        self.idle_grace_seconds = idle_grace_seconds
        self.thread_id: str | None = None
        self.active_turn_id: str | None = None
        self.latest_assistant_message = ""
        self.terminal_failure: str | None = None
        self.lease: LeaseToken | None = None
        self.turn_config: dict[str, Any] | None = None
        self.requested_model: str | None = None
        self.requested_effort: str | None = None
        self.post_terminal_settle_seconds = post_terminal_settle_seconds
        self.pending_terminal: PendingTerminal | None = None
        self.latest_token_usage_by_turn: dict[str, dict[str, Any]] = {}
        self.latest_token_usage_by_thread: dict[str, dict[str, Any]] = {}
        self.token_usage_notification_counts: dict[str, int] = {}
        self.token_usage_cumulative_growth_counts: dict[str, int] = {}
        self.token_usage_duplicate_notification_counts: dict[str, int] = {}
        self.token_usage_cumulative_growth_totals: dict[str, dict[str, int]] = {}
        self.resume_supports_provider_model_fallback = (
            resume_supports_provider_model_fallback
        )

    def _lease(self) -> LeaseToken:
        if self.lease is None:
            raise LeaseBusy("generator broker has not acquired its run lease")
        return self.lease

    @staticmethod
    def _thread_id(result: object) -> str:
        thread_id = _extract_identifier(result, "thread", "id") or _extract_identifier(
            result, "thread", "threadId"
        )
        if thread_id is None:
            raise ProtocolError("thread RPC result omitted thread id")
        return thread_id

    @staticmethod
    def _turn_id(result: object) -> str:
        turn_id = _extract_identifier(result, "turn", "id") or _extract_identifier(
            result, "turn", "turnId"
        )
        if turn_id is None:
            raise ProtocolError("turn RPC result omitted turn id")
        return turn_id

    def _record_attestation_failure(self, stage: str, detail: str) -> None:
        detail_digest = hashlib.sha256(detail.encode("utf-8")).hexdigest()
        self.ledger.record_audit_event(
            self.run_id,
            kind="audit_generator_attestation_failed",
            payload={
                "detail": _safe_stderr_line(detail),
                "detail_sha256": detail_digest,
                "stage": stage,
            },
            actor="adapter",
            lease=self._lease(),
        )

    def _attest_model_catalog(self, requested_model: str, effort: str) -> None:
        cursor: str | None = None
        seen_cursors: set[str] = set()
        matches: list[dict[str, Any]] = []
        total = 0
        pages = 0
        try:
            while True:
                pages += 1
                if pages > MAX_MODEL_CATALOG_PAGES:
                    raise ProtocolError("model/list exceeded the catalog page budget")
                params: dict[str, Any] = {"includeHidden": True, "limit": 100}
                if cursor is not None:
                    params["cursor"] = cursor
                result = self.client.call("model/list", params)
                if not isinstance(result, dict) or not isinstance(
                    result.get("data"), list
                ):
                    raise ProtocolError("model/list response omitted its model array")
                for entry in result["data"]:
                    if not isinstance(entry, dict):
                        raise ProtocolError("model/list returned a non-object model")
                    total += 1
                    if total > MAX_MODEL_CATALOG_ENTRIES:
                        raise ProtocolError(
                            "model/list exceeded the model entry budget"
                        )
                    if entry.get("model") == requested_model:
                        matches.append(entry)
                next_cursor = result.get("nextCursor")
                if next_cursor is None:
                    break
                if (
                    not isinstance(next_cursor, str)
                    or not next_cursor
                    or next_cursor in seen_cursors
                ):
                    raise ProtocolError(
                        "model/list returned an invalid pagination cursor"
                    )
                seen_cursors.add(next_cursor)
                cursor = next_cursor
            if len(matches) != 1:
                raise HotJoinError(
                    "requested model was not uniquely present in the app-server catalog"
                )
            supported = matches[0].get("supportedReasoningEfforts")
            if not isinstance(supported, list) or effort not in {
                option.get("reasoningEffort")
                for option in supported
                if isinstance(option, dict)
            }:
                raise HotJoinError(
                    f"requested reasoning effort {effort} is not supported by {requested_model}"
                )
        except (HotJoinError, RpcError) as exc:
            self._record_attestation_failure("model/list", str(exc))
            raise
        matched = matches[0]
        supported_projection = [{"reasoningEffort": effort}]
        self.ledger.record_audit_event(
            self.run_id,
            kind="audit_model_catalog_attested",
            payload={
                "catalog_entries": total,
                "catalog_pages": pages,
                "matched_model": {
                    "model": requested_model,
                    "supportedReasoningEfforts": supported_projection,
                },
                "matched_model_sha256": hashlib.sha256(
                    _canonical_json(matched).encode("utf-8")
                ).hexdigest(),
                "requested_effort": effort,
                "requested_model": requested_model,
            },
            actor="app_server",
            lease=self._lease(),
        )

    def _attest_thread_response(
        self,
        result: object,
        *,
        expected_thread_id: str | None,
        expected: Mapping[str, Any],
        rpc_method: str,
    ) -> str:
        if not isinstance(result, dict):
            self._record_attestation_failure(rpc_method, "response is not an object")
            raise ProtocolError(f"{rpc_method} response is not an object")
        thread_id = self._thread_id(result)
        sandbox = result.get("sandbox")
        thread = result.get("thread")
        runtime_roots = result.get("runtimeWorkspaceRoots")

        def canonical_path(raw: object, *, must_exist: bool) -> Path | None:
            if not isinstance(raw, str) or not raw or not Path(raw).is_absolute():
                return None
            try:
                resolved = Path(raw).resolve(strict=must_exist)
            except OSError:
                return None
            if must_exist and not resolved.is_dir():
                return None
            return resolved

        expected_cwd = canonical_path(expected.get("cwd"), must_exist=True)
        top_cwd = canonical_path(result.get("cwd"), must_exist=True)
        nested_cwd = canonical_path(
            thread.get("cwd") if isinstance(thread, dict) else None,
            must_exist=True,
        )
        writable_roots = (
            sandbox.get("writableRoots") if isinstance(sandbox, dict) else None
        )

        def roots_are_confined(roots: object) -> bool:
            if not isinstance(roots, list) or expected_cwd is None:
                return False
            for raw_root in roots:
                root = canonical_path(raw_root, must_exist=False)
                if root is None or not root.is_relative_to(expected_cwd):
                    return False
            return True

        response_sha256 = hashlib.sha256(
            _canonical_json(result).encode("utf-8")
        ).hexdigest()
        attested = {
            "approvalPolicy": result.get("approvalPolicy"),
            "cwd": result.get("cwd"),
            "model": result.get("model"),
            "reasoningEffort": result.get("reasoningEffort"),
            "response_sha256": response_sha256,
            "runtimeWorkspaceRoots": (
                list(runtime_roots) if isinstance(runtime_roots, list) else None
            ),
            "sandbox": {
                "networkAccess": (
                    sandbox.get("networkAccess") if isinstance(sandbox, dict) else None
                ),
                "type": sandbox.get("type") if isinstance(sandbox, dict) else None,
                "writableRoots": (
                    list(writable_roots) if isinstance(writable_roots, list) else None
                ),
            },
            "threadCwd": thread.get("cwd") if isinstance(thread, dict) else None,
            "threadEphemeral": (
                thread.get("ephemeral") if isinstance(thread, dict) else None
            ),
            "threadId": thread_id,
        }
        mismatch = (
            (expected_thread_id is not None and thread_id != expected_thread_id)
            or attested["model"] != expected["model"]
            or expected_cwd is None
            or top_cwd != expected_cwd
            or nested_cwd != expected_cwd
            or attested["threadEphemeral"] is not False
            or attested["approvalPolicy"] != expected["approvalPolicy"]
            or attested["reasoningEffort"] != expected["effort"]
            or expected.get("sandbox") != "workspace-write"
            or not isinstance(sandbox, dict)
            or sandbox.get("type") != "workspaceWrite"
            or sandbox.get("networkAccess") is not False
            or not roots_are_confined(writable_roots)
            or not roots_are_confined(runtime_roots)
        )
        if mismatch:
            self._record_attestation_failure(
                rpc_method,
                "runtime thread configuration differs from the requested generator "
                f"binding; response_sha256={response_sha256}",
            )
            raise ProtocolError(
                f"{rpc_method} did not attest the exact generator runtime configuration"
            )
        self.ledger.record_audit_event(
            self.run_id,
            kind="audit_thread_runtime_attested",
            payload={"rpc_method": rpc_method, **attested},
            actor="app_server",
            lease=self._lease(),
        )
        return thread_id

    def _ensure_thread(self, thread_params: Mapping[str, Any]) -> str:
        if self.turn_config is None:
            raise HotJoinError("generator thread configuration was not attested")
        status = self.ledger.status(self.run_id)
        persisted = status["thread_id"]
        if isinstance(persisted, str) and persisted:
            resume_params = {
                key: thread_params[key]
                for key in ("approvalPolicy", "config", "cwd", "model", "sandbox")
                if key in thread_params
            }
            if self.resume_supports_provider_model_fallback:
                resume_params["allowProviderModelFallback"] = False
            resume_params["threadId"] = persisted
            self.ledger.assert_lease(self.run_id, self._lease())
            result = self.client.call("thread/resume", resume_params)
            self._attest_thread_response(
                result,
                expected_thread_id=persisted,
                expected=self.turn_config or {},
                rpc_method="thread/resume",
            )
            self.thread_id = persisted
            return persisted
        self.ledger.assert_lease(self.run_id, self._lease())
        result = self.client.call("thread/start", dict(thread_params))
        thread_id = self._attest_thread_response(
            result,
            expected_thread_id=None,
            expected=self.turn_config or {},
            rpc_method="thread/start",
        )
        self.ledger.bind_thread(self.run_id, thread_id, lease=self._lease())
        self.thread_id = thread_id
        return thread_id

    def _start_turn(
        self,
        text: str,
        client_message_id: str,
        *,
        kind: str,
        message_id: str | None = None,
    ) -> str:
        assert self.thread_id is not None
        if self.turn_config is None:
            raise HotJoinError("generator turn configuration was not attested")
        self.ledger.prepare_turn_intent(
            self.run_id,
            client_message_id=client_message_id,
            kind=kind,
            prompt=text,
            config=self.turn_config,
            thread_id=self.thread_id,
            message_id=message_id,
            lease=self._lease(),
        )
        self.ledger.begin_turn_intent_dispatch(
            self.run_id,
            client_message_id=client_message_id,
            lease=self._lease(),
        )
        self.ledger.assert_lease(self.run_id, self._lease())
        try:
            result = self.client.call(
                "turn/start",
                {
                    "approvalPolicy": self.turn_config["approvalPolicy"],
                    "clientUserMessageId": client_message_id,
                    "cwd": self.turn_config["cwd"],
                    "effort": self.turn_config["effort"],
                    "input": [{"type": "text", "text": text}],
                    "model": self.turn_config["model"],
                    "threadId": self.thread_id,
                },
            )
        except RpcError as exc:
            self.ledger.mark_turn_intent_rejected(
                self.run_id,
                client_message_id=client_message_id,
                reason=str(exc),
                lease=self._lease(),
            )
            raise
        turn_id = self._turn_id(result)
        turn = result.get("turn") if isinstance(result, dict) else None
        if not isinstance(turn, dict) or turn.get("status") != "inProgress":
            raise ProtocolError("turn/start result did not attest an in-progress turn")
        self.ledger.bind_turn_intent_applied(
            self.run_id,
            client_message_id=client_message_id,
            turn_id=turn_id,
            source="turn/start response",
            lease=self._lease(),
        )
        self.active_turn_id = turn_id
        return turn_id

    def _stage_terminal(self, turn: Mapping[str, Any]) -> None:
        terminal = _json_loads_strict(_canonical_json(dict(turn)))
        if not isinstance(terminal, dict):
            raise ProtocolError("terminal turn could not be copied safely")
        summary = _terminal_audit(terminal)
        turn_id = str(summary["id"])
        if (
            not isinstance(terminal.get("items"), list)
            or self.ledger.status(self.run_id)["active_turn_id"] != turn_id
        ):
            raise ProtocolError("terminal turn does not match the durable active turn")
        assistant_message = _assistant_text(terminal) or self.latest_assistant_message
        expected_interruption = summary["status"] != "interrupted" or (
            self.ledger.has_expected_interrupt(self.run_id, turn_id)
        )
        candidate = PendingTerminal(
            turn=terminal,
            assistant_message=assistant_message,
            deadline_monotonic=(time.monotonic() + self.post_terminal_settle_seconds),
            expected_interruption=expected_interruption,
        )
        if self.pending_terminal is not None:
            if _canonical_json(self.pending_terminal.turn) != _canonical_json(
                candidate.turn
            ):
                raise ProtocolError("conflicting terminal payloads for one turn")
            return
        self.pending_terminal = candidate

    def _finalize_pending_terminal(self) -> bool:
        pending = self.pending_terminal
        if pending is None:
            return False
        if time.monotonic() < pending.deadline_monotonic:
            return False
        turn = pending.turn
        turn_id = str(turn["id"])
        status = str(turn["status"])
        error = turn.get("error")
        token_usage = self.latest_token_usage_by_turn.get(turn_id)
        token_observed = token_usage is not None
        terminal_audit = _terminal_audit(turn)
        terminal_audit.update(
            {
                "post_terminal_settle_bound_ms": round(
                    self.post_terminal_settle_seconds * 1000
                ),
                "tokenUsage": token_usage,
                "token_usage_finality": (
                    "observed_not_schema_attested_final"
                    if token_observed
                    else "not_observed_after_bounded_post_terminal_settle"
                ),
                "token_usage_notification_count": (
                    self.token_usage_notification_counts.get(turn_id, 0)
                ),
                "token_usage_cumulative_growth_sample_count": (
                    self.token_usage_cumulative_growth_counts.get(turn_id, 0)
                ),
                "token_usage_cumulative_growth_sample_totals": (
                    self.token_usage_cumulative_growth_totals.get(turn_id)
                ),
                "token_usage_duplicate_notification_count": (
                    self.token_usage_duplicate_notification_counts.get(turn_id, 0)
                ),
                "token_usage_count_finality": (
                    "observed_not_schema_attested_inference_count"
                    if token_observed
                    else "not_observed_after_bounded_post_terminal_settle"
                ),
                "token_usage_observed": token_observed,
            }
        )
        if status == "interrupted" and not pending.expected_interruption:
            quarantine_terminal = dict(terminal_audit)
            quarantine_terminal["error"] = _redact_sensitive_object(error)
            quarantine_terminal["error_sha256"] = hashlib.sha256(
                _canonical_json(error).encode("utf-8")
            ).hexdigest()
            self.ledger.quarantine_run(
                self.run_id,
                kind="unexpected_turn_interruption",
                reason="turn ended interrupted without an exact durable owner interrupt",
                thread_id=self.thread_id,
                turn_id=turn_id,
                payload={"thread_id": self.thread_id, "turn": quarantine_terminal},
                audit_kind="audit_unexpected_turn_interruption",
                lease=self._lease(),
            )
        assistant_message = _assistant_text(turn) or self.latest_assistant_message
        if not assistant_message:
            assistant_message = pending.assistant_message
        self.ledger.finalize_turn(
            self.run_id,
            turn_id=turn_id,
            status=status,
            assistant_message=assistant_message,
            error=error,
            terminal_audit=terminal_audit,
            lease=self._lease(),
        )
        if self.active_turn_id == turn_id:
            self.active_turn_id = None
        self.pending_terminal = None
        self.latest_assistant_message = ""
        self.latest_token_usage_by_turn.pop(turn_id, None)
        self.token_usage_notification_counts.pop(turn_id, None)
        self.token_usage_cumulative_growth_counts.pop(turn_id, None)
        self.token_usage_duplicate_notification_counts.pop(turn_id, None)
        self.token_usage_cumulative_growth_totals.pop(turn_id, None)
        if status == "failed":
            detail = _safe_error_text(error) if error is not None else "unknown"
            self.terminal_failure = f"app-server turn {turn_id} failed: {detail}"
        elif status == "interrupted" and not pending.expected_interruption:
            self.terminal_failure = (
                f"app-server turn {turn_id} was interrupted without owner authorization"
            )
        return True

    def _process_notification(self, notification: Mapping[str, Any]) -> None:
        method = notification.get("method")
        params = notification.get("params")
        relevant = {
            "item/completed",
            "model/rerouted",
            "thread/tokenUsage/updated",
            "turn/completed",
            "turn/started",
        }
        if method not in relevant:
            return
        if not isinstance(params, dict):
            raise ProtocolError(f"{method} notification params must be an object")
        notification_thread = _extract_identifier(
            params, "threadId"
        ) or _extract_identifier(params, "thread", "id")
        if (
            self.thread_id is not None
            and notification_thread is not None
            and notification_thread != self.thread_id
        ):
            return
        if self.thread_id is None or notification_thread != self.thread_id:
            raise ProtocolError(f"{method} notification omitted the exact thread id")
        if method == "turn/started":
            turn_id = _extract_identifier(params, "turn", "id") or _extract_identifier(
                params, "turn", "turnId"
            )
            turn = params.get("turn")
            if (
                turn_id is None
                or not isinstance(turn, dict)
                or turn.get("status") != "inProgress"
                or not isinstance(turn.get("items"), list)
            ):
                raise ProtocolError("turn/started omitted a valid in-progress turn")
            durable_active = self.ledger.status(self.run_id)["active_turn_id"]
            if durable_active != turn_id:
                raise ProtocolError(
                    "turn/started does not match the durable active turn intent"
                )
            self.active_turn_id = turn_id
        elif method == "item/completed":
            turn_id = _extract_identifier(params, "turnId")
            if (
                turn_id is None
                or turn_id != self.active_turn_id
                or not isinstance(params.get("item"), dict)
                or isinstance(params.get("completedAtMs"), bool)
                or not isinstance(params.get("completedAtMs"), int)
            ):
                raise ProtocolError(
                    "item/completed does not match the exact active generator turn"
                )
            text = _assistant_text(params)
            if text:
                self.latest_assistant_message = text
        elif method == "thread/tokenUsage/updated":
            turn_id = _extract_identifier(params, "turnId")
            if (
                set(params) != {"threadId", "tokenUsage", "turnId"}
                or params.get("threadId") != self.thread_id
                or turn_id is None
                or turn_id != self.active_turn_id
                or not isinstance(params.get("tokenUsage"), dict)
            ):
                raise ProtocolError(
                    "thread/tokenUsage/updated does not match the exact active turn"
                )
            token_usage = _canonical_token_usage(params["tokenUsage"])
            previous_usage = self.latest_token_usage_by_thread.get(str(self.thread_id))
            cumulative_total_changed = _token_usage_cumulative_total_changed(
                previous_usage, token_usage
            )
            notification_count = (
                self.token_usage_notification_counts.get(turn_id, 0) + 1
            )
            cumulative_growth_count = self.token_usage_cumulative_growth_counts.get(
                turn_id, 0
            ) + int(cumulative_total_changed)
            duplicate_notification_count = (
                self.token_usage_duplicate_notification_counts.get(turn_id, 0)
                + int(not cumulative_total_changed)
            )
            audited_update = {
                "threadId": self.thread_id,
                "tokenUsage": token_usage,
                "turnId": turn_id,
            }
            self.ledger.record_audit_event(
                self.run_id,
                kind="audit_thread_token_usage_updated",
                payload={
                    **audited_update,
                    "cumulative_growth_sample_index": cumulative_growth_count,
                    "cumulative_total_changed": cumulative_total_changed,
                    "duplicate_notification_count": duplicate_notification_count,
                    "notification_index": notification_count,
                    "raw_sha256": hashlib.sha256(
                        _canonical_json(params).encode("utf-8")
                    ).hexdigest(),
                },
                actor="app_server",
                lease=self._lease(),
            )
            self.latest_token_usage_by_turn[turn_id] = token_usage
            self.latest_token_usage_by_thread[str(self.thread_id)] = token_usage
            self.token_usage_notification_counts[turn_id] = notification_count
            self.token_usage_cumulative_growth_counts[turn_id] = cumulative_growth_count
            self.token_usage_duplicate_notification_counts[turn_id] = (
                duplicate_notification_count
            )
            if cumulative_total_changed:
                self.token_usage_cumulative_growth_totals[turn_id] = (
                    _add_token_usage_breakdown(
                        self.token_usage_cumulative_growth_totals.get(turn_id),
                        token_usage["last"],
                    )
                )
        elif method == "model/rerouted":
            turn_id = _extract_identifier(params, "turnId")
            reroute_fields = {
                "fromModel",
                "reason",
                "threadId",
                "toModel",
                "turnId",
            }
            required_values = {key: params.get(key) for key in reroute_fields}
            if (
                set(params) != reroute_fields
                or any(
                    not isinstance(value, str)
                    or not value
                    or len(value.encode("utf-8")) > MAX_REROUTE_FIELD_BYTES
                    for value in required_values.values()
                )
                or params.get("reason") not in SUPPORTED_MODEL_REROUTE_REASONS
            ):
                raise ProtocolError("model/rerouted has an invalid audited field")
            if turn_id is None or turn_id != self.active_turn_id:
                raise ProtocolError(
                    "model/rerouted does not match the exact active generator turn"
                )
            self.ledger.quarantine_run(
                self.run_id,
                kind="model_rerouted",
                reason="app-server reported a model reroute for the paid generator turn",
                thread_id=self.thread_id,
                turn_id=turn_id,
                payload=required_values,
                audit_kind="audit_model_rerouted",
                lease=self._lease(),
            )
            raise HotJoinError(
                f"generator model was rerouted from {params['fromModel']} "
                f"to {params['toModel']}"
            )
        elif method == "turn/completed":
            turn_id = _extract_identifier(params, "turn", "id") or _extract_identifier(
                params, "turn", "turnId"
            )
            status = _extract_identifier(params, "turn", "status")
            if turn_id is None or status not in {"completed", "failed", "interrupted"}:
                raise ProtocolError(
                    "turn/completed notification omitted a valid terminal turn"
                )
            turn = params.get("turn")
            if (
                not isinstance(turn, dict)
                or not isinstance(turn.get("items"), list)
                or self.ledger.status(self.run_id)["active_turn_id"] != turn_id
            ):
                raise ProtocolError(
                    "turn/completed does not match the durable active turn"
                )
            self._stage_terminal(turn)
            self._finalize_pending_terminal()

    def _reconcile_uncertain_messages(self) -> None:
        assert self.thread_id is not None
        prepared_message_ids = {
            intent.message_id
            for intent in self.ledger.turn_intents(self.run_id, states={"prepared"})
            if intent.message_id is not None
        }
        uncertain = [
            message
            for message in self.ledger.pending_messages(self.run_id)
            if message.state in {"dispatching", "interrupting"}
            and message.message_id not in prepared_message_ids
        ]
        self.ledger.assert_lease(self.run_id, self._lease())
        history_result = self.client.call(
            "thread/read", {"threadId": self.thread_id, "includeTurns": True}
        )
        history = _validated_thread_read(history_result, self.thread_id)

        dispatching_intents = self.ledger.turn_intents(
            self.run_id, states={"dispatching"}
        )
        for intent in dispatching_intents:
            matches = _turn_records_for_client_message(
                history, intent.client_message_id
            )
            if len(matches) != 1:
                self.ledger.mark_turn_intent_unknown(
                    self.run_id,
                    client_message_id=intent.client_message_id,
                    reason=(
                        "turn/start was not uniquely located in the exact thread history; "
                        "owner must explicitly authorize retry"
                    ),
                    lease=self._lease(),
                )
                continue
            recovered = matches[0]
            self.ledger.bind_turn_intent_applied(
                self.run_id,
                client_message_id=intent.client_message_id,
                turn_id=str(recovered["id"]),
                source="recovered/thread/read",
                lease=self._lease(),
            )

        recovered_active = self.ledger.status(self.run_id)["active_turn_id"]
        if isinstance(recovered_active, str) and recovered_active:
            recovered_record = _turn_record(history, recovered_active)
            self.ledger.quarantine_run(
                self.run_id,
                kind="reroute_observation_unknown_after_adapter_interruption",
                reason=(
                    "an active paid turn crossed an adapter interruption; thread/read "
                    "cannot replay model/rerouted observations"
                ),
                thread_id=self.thread_id,
                turn_id=recovered_active,
                payload={
                    "history_status": (
                        recovered_record.get("status")
                        if recovered_record is not None
                        else "missing"
                    ),
                    "thread_id": self.thread_id,
                    "token_usage_finality": "unknown_after_adapter_interruption",
                    "token_usage_observed": None,
                    "turn_id": recovered_active,
                },
                audit_kind="audit_reroute_observation_unknown",
                lease=self._lease(),
            )
            raise HotJoinError(
                "active paid turn recovery is quarantined because reroute observation "
                "is unknown after adapter interruption"
            )

        for message in uncertain:
            if message.state == "interrupting":
                interrupted_record = (
                    _turn_record(history, message.turn_id) if message.turn_id else None
                )
                if (
                    interrupted_record is not None
                    and interrupted_record["status"] != "inProgress"
                ):
                    interrupted_status = str(interrupted_record["status"])
                    self.ledger.finalize_turn(
                        self.run_id,
                        turn_id=str(interrupted_record["id"]),
                        status=interrupted_status,
                        assistant_message=_assistant_text(interrupted_record),
                        error=interrupted_record.get("error"),
                        terminal_audit=_terminal_audit(interrupted_record),
                        lease=self._lease(),
                    )
                    if interrupted_status == "failed":
                        raise HotJoinError(
                            f"recovered app-server turn {interrupted_record['id']} failed"
                        )
                else:
                    self.ledger.mark_delivery_unknown(
                        self.run_id,
                        message.message_id,
                        reason=(
                            "turn/interrupt acceptance is ambiguous after recovery; "
                            "owner must explicitly authorize retry"
                        ),
                        lease=self._lease(),
                    )
                continue
            matches = _turn_records_for_client_message(
                history, message.client_message_id
            )
            exact_match = len(matches) == 1 and (
                message.turn_id is None or matches[0].get("id") == message.turn_id
            )
            if exact_match and message.attempt_id:
                recovered_turn = matches[0]
                recovered_turn_id = str(recovered_turn["id"])
                self.ledger.mark_delivered(
                    self.run_id,
                    message.message_id,
                    attempt_id=message.attempt_id,
                    thread_id=self.thread_id,
                    turn_id=recovered_turn_id,
                    rpc_method="recovered/thread/read",
                    lease=self._lease(),
                )
                recovered_status = str(recovered_turn["status"])
                durable_active = self.ledger.status(self.run_id)["active_turn_id"]
                if durable_active != recovered_turn_id:
                    raise ProtocolError(
                        "recovered message does not belong to the durable active turn"
                    )
                if recovered_status != "inProgress":
                    self.ledger.finalize_turn(
                        self.run_id,
                        turn_id=recovered_turn_id,
                        status=recovered_status,
                        assistant_message=_assistant_text(recovered_turn),
                        error=recovered_turn.get("error"),
                        terminal_audit=_terminal_audit(recovered_turn),
                        lease=self._lease(),
                    )
                    if recovered_status == "failed":
                        raise HotJoinError(
                            f"recovered app-server turn {recovered_turn_id} failed"
                        )
            else:
                self.ledger.mark_delivery_unknown(
                    self.run_id,
                    message.message_id,
                    reason=(
                        "delivery outcome was not visible after adapter recovery; "
                        "owner must explicitly authorize retry"
                    ),
                    lease=self._lease(),
                )

        durable_active = self.ledger.status(self.run_id)["active_turn_id"]
        if isinstance(durable_active, str) and durable_active:
            active_record = _turn_record(history, durable_active)
            if active_record is None:
                raise ProtocolError(
                    "durable active turn is absent from authenticated thread history"
                )
            active_status = str(active_record["status"])
            if active_status != "inProgress":
                self.ledger.finalize_turn(
                    self.run_id,
                    turn_id=durable_active,
                    status=active_status,
                    assistant_message=_assistant_text(active_record),
                    error=active_record.get("error"),
                    terminal_audit=_terminal_audit(active_record),
                    lease=self._lease(),
                )
                if active_status == "failed":
                    raise HotJoinError(
                        f"recovered app-server turn {durable_active} failed"
                    )

    def _deliver_message(self, message: MessageRecord) -> bool:
        assert self.thread_id is not None
        if any(
            pending.state == "interrupting"
            for pending in self.ledger.pending_messages(self.run_id)
        ):
            # turn/interrupt has no idempotency key and the old turn is still
            # resolving. Do not race a second interrupt, steer, or fresh turn
            # against that unresolved control operation.
            return False
        if message.mode == "queue" and self.active_turn_id is not None:
            return False
        if message.mode == "interrupt" and self.active_turn_id is not None:
            interrupted_turn = self.active_turn_id
            self.ledger.begin_delivery(
                self.run_id,
                message.message_id,
                thread_id=self.thread_id,
                turn_id=interrupted_turn,
                action="turn/interrupt",
                lease=self._lease(),
            )
            self.ledger.assert_lease(self.run_id, self._lease())
            try:
                self.client.call(
                    "turn/interrupt",
                    {"threadId": self.thread_id, "turnId": interrupted_turn},
                )
            except RpcError as exc:
                self.ledger.requeue_message(
                    self.run_id,
                    message.message_id,
                    reason=str(exc),
                    lease=self._lease(),
                )
                raise HotJoinError(
                    "turn/interrupt was rejected; owner message remains queued"
                ) from exc
            # The owner's text is not part of turn/interrupt.  Keep it in the
            # interrupting state until the matching terminal notification moves
            # it back to queued for a fresh turn.
            return False

        if self.active_turn_id is None:
            attempt = self.ledger.begin_delivery(
                self.run_id,
                message.message_id,
                thread_id=self.thread_id,
                turn_id=None,
                action="turn/start",
                lease=self._lease(),
            )
            try:
                turn_id = self._start_turn(
                    message.text,
                    message.client_message_id,
                    kind="owner",
                    message_id=message.message_id,
                )
            except RpcError as exc:
                self.ledger.requeue_message(
                    self.run_id,
                    message.message_id,
                    reason=str(exc),
                    lease=self._lease(),
                )
                raise HotJoinError(
                    "turn/start was rejected; owner message remains queued"
                ) from exc
            self.ledger.mark_delivered(
                self.run_id,
                message.message_id,
                attempt_id=attempt,
                thread_id=self.thread_id,
                turn_id=turn_id,
                rpc_method="turn/start",
                lease=self._lease(),
            )
            return True

        turn_id = self.active_turn_id
        attempt = self.ledger.begin_delivery(
            self.run_id,
            message.message_id,
            thread_id=self.thread_id,
            turn_id=turn_id,
            action="turn/steer",
            lease=self._lease(),
        )
        self.ledger.assert_lease(self.run_id, self._lease())
        try:
            result = self.client.call(
                "turn/steer",
                {
                    "clientUserMessageId": message.client_message_id,
                    "expectedTurnId": turn_id,
                    "input": [{"type": "text", "text": message.text}],
                    "threadId": self.thread_id,
                },
            )
        except RpcError as exc:
            if _is_nonsteerable_turn_error(exc.error):
                self.ledger.defer_message_until_turn_ends(
                    self.run_id,
                    message.message_id,
                    reason=str(exc),
                    lease=self._lease(),
                )
            else:
                self.ledger.requeue_message(
                    self.run_id,
                    message.message_id,
                    reason=str(exc),
                    lease=self._lease(),
                )
                raise HotJoinError(
                    "turn/steer was rejected; owner message remains queued"
                ) from exc
            return False
        accepted_turn = _extract_identifier(result, "turnId")
        if accepted_turn != turn_id:
            self.ledger.mark_delivery_unknown(
                self.run_id,
                message.message_id,
                reason="turn/steer did not atomically confirm the expected active turn",
                lease=self._lease(),
            )
            return False
        self.ledger.mark_delivered(
            self.run_id,
            message.message_id,
            attempt_id=attempt,
            thread_id=self.thread_id,
            turn_id=turn_id,
            rpc_method="turn/steer",
            lease=self._lease(),
        )
        return True

    def run(
        self,
        *,
        initial_prompt: str,
        thread_params: Mapping[str, Any],
        max_runtime_seconds: float,
    ) -> dict[str, Any]:
        self.lease = self.ledger.acquire_lease(self.run_id, self.owner_id)
        started = time.monotonic()
        last_activity = started
        try:
            self.ledger.assert_not_quarantined(self.run_id)
            config = thread_params.get("config")
            model = thread_params.get("model")
            cwd = thread_params.get("cwd")
            approval = thread_params.get("approvalPolicy")
            effort = (
                config.get("model_reasoning_effort")
                if isinstance(config, dict)
                else None
            )
            if (
                not isinstance(model, str)
                or not isinstance(cwd, str)
                or approval != "never"
                or not isinstance(effort, str)
            ):
                raise HotJoinError("thread params lack the exact generator binding")
            self.requested_model = model
            self.requested_effort = effort
            self.turn_config = {
                "approvalPolicy": "never",
                "cwd": cwd,
                "effort": effort,
                "model": model,
                "sandbox": thread_params.get("sandbox"),
            }
            self._attest_model_catalog(model, effort)
            self._ensure_thread(thread_params)
            pre_status = self.ledger.status(self.run_id)
            pre_intents = self.ledger.turn_intents(self.run_id)
            prepared_message_ids = {
                intent.message_id
                for intent in pre_intents
                if intent.state == "prepared" and intent.message_id is not None
            }
            pre_messages = self.ledger.pending_messages(self.run_id)
            unsafe_pending = [
                message
                for message in pre_messages
                if message.state != "queued"
                and not (
                    message.state == "dispatching"
                    and message.message_id in prepared_message_ids
                )
            ]
            nonterminal_intents = [
                intent
                for intent in pre_intents
                if intent.state
                in {
                    "active",
                    "delivery_unknown",
                    "dispatching",
                    "prepared",
                    "retry_authorized",
                }
            ]
            if (
                pre_status["active_turn_id"] is None
                and not nonterminal_intents
                and not unsafe_pending
            ):
                bootstrap_id = f"bootstrap:{self.run_id}:{pre_status['generation'] + 1}"
                assert self.thread_id is not None
                self.ledger.prepare_turn_intent(
                    self.run_id,
                    client_message_id=bootstrap_id,
                    kind="bootstrap",
                    prompt=initial_prompt,
                    config=self.turn_config,
                    thread_id=self.thread_id,
                    message_id=None,
                    lease=self._lease(),
                )

            current_intents = self.ledger.turn_intents(self.run_id)
            prepared_intents = [
                intent for intent in current_intents if intent.state == "prepared"
            ]
            prepared_message_ids = {
                intent.message_id
                for intent in prepared_intents
                if intent.message_id is not None
            }
            recovery_intents = [
                intent
                for intent in current_intents
                if intent.state
                in {"active", "delivery_unknown", "dispatching", "retry_authorized"}
            ]
            recovery_messages = [
                message
                for message in self.ledger.pending_messages(self.run_id)
                if message.state != "queued"
                and not (
                    message.state == "dispatching"
                    and message.message_id in prepared_message_ids
                )
            ]
            if (
                pre_status["active_turn_id"] is not None
                or recovery_intents
                or recovery_messages
            ):
                self._reconcile_uncertain_messages()
            elif len(prepared_intents) != 1:
                raise HotJoinError(
                    "fresh turn materialization requires exactly one prepared intent"
                )
            unknown_turns = self.ledger.turn_intents(
                self.run_id, states={"delivery_unknown"}
            )
            if unknown_turns:
                raise HotJoinError(
                    "ambiguous turn/start requires explicit retry: "
                    + ", ".join(intent.client_message_id for intent in unknown_turns)
                )
            status = self.ledger.status(self.run_id)
            persisted_active = status["active_turn_id"]
            self.active_turn_id = (
                persisted_active if isinstance(persisted_active, str) else None
            )
            retry_owner_intents = [
                intent
                for intent in self.ledger.turn_intents(
                    self.run_id, states={"retry_authorized"}
                )
                if intent.kind == "owner"
            ]
            if self.active_turn_id is None and retry_owner_intents:
                if len(retry_owner_intents) != 1:
                    raise HotJoinError(
                        "multiple owner turn/start retries require manual reconciliation"
                    )
                retry_intent = retry_owner_intents[0]
                retry_messages = {
                    message.message_id: message
                    for message in self.ledger.pending_messages(self.run_id)
                }
                retry_message = (
                    retry_messages.get(retry_intent.message_id)
                    if retry_intent.message_id is not None
                    else None
                )
                if retry_message is None or retry_message.state != "queued":
                    raise HotJoinError(
                        "owner turn/start retry lacks its exact queued message"
                    )
                self._deliver_message(retry_message)
                last_activity = time.monotonic()
            if self.active_turn_id is None:
                prepared_now = self.ledger.turn_intents(
                    self.run_id, states={"prepared"}
                )
                if len(prepared_now) > 1:
                    raise HotJoinError(
                        "multiple never-dispatched turn intents require reconciliation"
                    )
                if prepared_now:
                    intent = prepared_now[0]
                    if intent.kind == "bootstrap":
                        self._start_turn(
                            initial_prompt,
                            intent.client_message_id,
                            kind="bootstrap",
                        )
                    else:
                        if intent.message_id is None:
                            raise HotJoinError(
                                "prepared owner turn lacks its durable message"
                            )
                        self._start_turn(
                            intent.prompt,
                            intent.client_message_id,
                            kind="owner",
                            message_id=intent.message_id,
                        )
                else:
                    bootstrap_id = f"bootstrap:{self.run_id}:{status['generation'] + 1}"
                    self._start_turn(
                        initial_prompt,
                        bootstrap_id,
                        kind="bootstrap",
                    )
                last_activity = time.monotonic()

            while True:
                if self._finalize_pending_terminal():
                    last_activity = time.monotonic()
                if self.terminal_failure is not None:
                    raise HotJoinError(self.terminal_failure)
                if time.monotonic() - started > max_runtime_seconds:
                    if self.pending_terminal is not None:
                        # A terminal event was already observed.  Permit only
                        # its small configured settle window so delayed token
                        # usage can arrive; never shorten that window while
                        # claiming it was fully observed.
                        pass
                    elif self.active_turn_id is None:
                        break
                    else:
                        raise HotJoinError(
                            "hot-join adapter runtime elapsed; active turn was not implicitly interrupted"
                        )
                self.ledger.renew_lease(self.run_id, self._lease())
                poll_seconds = self.poll_seconds
                if self.pending_terminal is not None:
                    poll_seconds = min(
                        poll_seconds,
                        max(
                            0.0,
                            self.pending_terminal.deadline_monotonic - time.monotonic(),
                        ),
                    )
                notification = self.client.next_notification(poll_seconds)
                if notification is not None:
                    self._process_notification(notification)
                    last_activity = time.monotonic()
                    self._finalize_pending_terminal()
                    if self.terminal_failure is not None:
                        raise HotJoinError(self.terminal_failure)

                if self.pending_terminal is not None:
                    continue

                progressed = False
                pending_now = self.ledger.pending_messages(self.run_id)
                unknown = [
                    message.message_id
                    for message in pending_now
                    if message.state == "delivery_unknown"
                ]
                if unknown:
                    raise HotJoinError(
                        "ambiguous message delivery requires explicit retry: "
                        + ", ".join(unknown)
                    )
                mode_priority = {"interrupt": 0, "steer": 1, "queue": 2}
                pending_now.sort(
                    key=lambda message: (
                        mode_priority[message.mode],
                        message.accepted_sequence,
                    )
                )
                for message in pending_now:
                    # A durable uncertain attempt is reconciled once, never
                    # blindly sent twice after a crash.
                    if message.state != "queued":
                        continue
                    if self._deliver_message(message):
                        progressed = True
                        last_activity = time.monotonic()
                    if message.mode == "interrupt":
                        break

                if progressed:
                    continue
                pending = self.ledger.pending_messages(self.run_id)
                if (
                    self.active_turn_id is None
                    and not pending
                    and time.monotonic() - last_activity >= self.idle_grace_seconds
                ):
                    break
            return self.ledger.status(self.run_id)
        finally:
            if self.lease is not None:
                self.ledger.release_lease(self.run_id, self.lease)


def _parse_toml_value(raw: str, label: str) -> Any:
    try:
        return tomllib.loads("value=" + raw)["value"]
    except (tomllib.TOMLDecodeError, KeyError) as exc:
        raise ValueError(f"{label} is not a valid TOML value") from exc


def _read_message(args: argparse.Namespace) -> str:
    if args.text is not None:
        return args.text
    if args.stdin:
        return sys.stdin.read()
    raise ValueError("provide --text or --stdin")


def _validate_generator_config(
    *,
    mcp: object,
    shell_policy: object,
    prompt: str,
    model: str,
    effort: str,
    max_runtime_seconds: float,
    idle_grace_seconds: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("--prompt must be non-empty")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("--model must be non-empty")
    if not isinstance(effort, str) or not effort.strip():
        raise ValueError("--effort must be non-empty")
    if max_runtime_seconds <= 0 or idle_grace_seconds < 0:
        raise ValueError("runtime must be positive and idle grace must be non-negative")
    if not isinstance(mcp, dict):
        raise ValueError("--mcp-config-toml must be an object")
    missing = {
        "command",
        "args",
        "cwd",
        "env",
        "required",
        "tool_timeout_sec",
        "default_tools_approval_mode",
    } - set(mcp)
    if missing:
        raise ValueError("MCP object is incomplete: " + ", ".join(sorted(missing)))
    command = mcp["command"]
    if (
        not isinstance(command, str)
        or not Path(command).is_absolute()
        or not Path(command).is_file()
        or not os.access(command, os.X_OK)
    ):
        raise ValueError("MCP command must be an existing absolute executable")
    if not isinstance(mcp["args"], list) or not all(
        isinstance(value, str) for value in mcp["args"]
    ):
        raise ValueError("MCP args must be an array of strings")
    mcp_cwd = mcp["cwd"]
    if (
        not isinstance(mcp_cwd, str)
        or not Path(mcp_cwd).is_absolute()
        or not Path(mcp_cwd).is_dir()
    ):
        raise ValueError("MCP cwd must be an existing absolute directory")
    if not isinstance(mcp["env"], dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in mcp["env"].items()
    ):
        raise ValueError("MCP env must map strings to strings")
    timeout = mcp["tool_timeout_sec"]
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or timeout <= 0
    ):
        raise ValueError("MCP tool_timeout_sec must be positive")
    if mcp["default_tools_approval_mode"] != "approve":
        raise ValueError("reasoning MCP tools must remain noninteractive")
    if mcp["required"] is not True:
        raise ValueError("reasoning MCP server must be required=true")

    if not isinstance(shell_policy, dict):
        raise ValueError("--shell-policy-toml must be an object")
    shell_set = shell_policy.get("set")
    if (
        shell_policy.get("inherit") != "none"
        or not isinstance(shell_set, dict)
        or not isinstance(shell_set.get("PATH"), str)
        or not shell_set["PATH"]
    ):
        raise ValueError("shell policy must use inherit=none and an explicit PATH")
    return dict(mcp), dict(shell_policy)


def _print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Rethlas generator human hot-join adapter"
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_STATE_DB)
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser(
        "init", help="create or inspect one generator run"
    )
    init_parser.add_argument("--run-id", required=True)
    init_parser.add_argument("--problem-id", required=True)

    send_parser = subparsers.add_parser(
        "send", help="durably enqueue one owner message"
    )
    send_parser.add_argument("--run-id", required=True)
    send_parser.add_argument("--mode", choices=sorted(MESSAGE_MODES), default="steer")
    send_parser.add_argument("--client-message-id")
    message_source = send_parser.add_mutually_exclusive_group(required=True)
    message_source.add_argument("--text")
    message_source.add_argument("--stdin", action="store_true")

    status_parser = subparsers.add_parser(
        "status", help="show durable run/message state"
    )
    status_parser.add_argument("--run-id", required=True)

    tail_parser = subparsers.add_parser(
        "tail", help="read ordered message/reply receipts"
    )
    tail_parser.add_argument("--run-id", required=True)
    tail_parser.add_argument("--after-sequence", type=int, default=0)
    tail_parser.add_argument("--limit", type=int, default=100)

    verify_parser = subparsers.add_parser(
        "verify-ledger", help="verify the event hash chain"
    )
    verify_parser.add_argument("--run-id", required=True)

    retry_parser = subparsers.add_parser(
        "retry-unknown", help="explicitly retry one ambiguous app-server delivery"
    )
    retry_parser.add_argument("--run-id", required=True)
    retry_parser.add_argument("--message-id", required=True)

    retry_turn_parser = subparsers.add_parser(
        "retry-unknown-turn",
        help="explicitly retry one ambiguous bootstrap turn/start",
    )
    retry_turn_parser.add_argument("--run-id", required=True)
    retry_turn_parser.add_argument("--client-message-id", required=True)

    run_parser = subparsers.add_parser(
        "run-generator",
        help="run/resume the existing generation agent with hot-join transport",
    )
    run_parser.add_argument("--run-id", required=True)
    run_parser.add_argument("--problem-id", required=True)
    run_parser.add_argument("--cwd", type=Path, required=True)
    run_parser.add_argument("--prompt", required=True)
    run_parser.add_argument("--model", default="gpt-5.6-sol")
    run_parser.add_argument("--effort", default="max")
    run_parser.add_argument("--web-mode", choices=("live", "disabled"), default="live")
    run_parser.add_argument("--mcp-config-toml", required=True)
    run_parser.add_argument("--shell-policy-toml", required=True)
    run_parser.add_argument("--codex-bin", default="codex")
    run_parser.add_argument("--max-runtime-seconds", type=float, default=14_400.0)
    run_parser.add_argument("--idle-grace-seconds", type=float, default=1.0)
    return parser


def _run_generator_command(args: argparse.Namespace) -> dict[str, Any]:
    # Capability and config validation deliberately precede creating the run or
    # starting app-server. A known incompatible CLI therefore costs no model
    # tokens and leaves no misleading durable run receipt.
    _validate_run_id(args.run_id)
    if not isinstance(args.problem_id, str) or not args.problem_id.strip():
        raise ValueError("--problem-id must be non-empty")
    mcp = _parse_toml_value(args.mcp_config_toml, "--mcp-config-toml")
    shell_policy = _parse_toml_value(args.shell_policy_toml, "--shell-policy-toml")
    mcp, shell_policy = _validate_generator_config(
        mcp=mcp,
        shell_policy=shell_policy,
        prompt=args.prompt,
        model=args.model,
        effort=args.effort,
        max_runtime_seconds=args.max_runtime_seconds,
        idle_grace_seconds=args.idle_grace_seconds,
    )
    cwd = args.cwd.resolve(strict=True)
    if not cwd.is_dir():
        raise ValueError("--cwd must resolve to a directory")
    capability = preflight_app_server(args.codex_bin)
    adapter_commitment = _adapter_code_commitment()
    mcp_env_commitment, rotatable_secret_env_keys = _mcp_env_commitment(mcp["env"])
    committed_mcp = dict(mcp)
    committed_mcp["args"] = _mcp_args_commitment(mcp["args"], mcp["cwd"])
    committed_mcp["env"] = mcp_env_commitment
    committed_mcp["role"] = "reasoning_agent"
    fingerprint_material = {
        "app_server_schema_digest": capability.schema_digest,
        "hotjoin_adapter": adapter_commitment,
        "codex_version": capability.codex_version,
        "cwd": str(cwd),
        "effort": args.effort,
        "mcp": committed_mcp,
        "model": args.model,
        "sandbox": "workspace-write",
        "shell_policy": shell_policy,
        "resume_supports_provider_model_fallback": (
            capability.resume_supports_provider_model_fallback
        ),
    }
    fingerprint = hashlib.sha256(
        _canonical_json(fingerprint_material).encode("utf-8")
    ).hexdigest()

    # All fallible capability, filesystem, and fingerprint checks above happen
    # before the first durable run receipt is created.
    ledger = ConversationLedger(args.db)
    ledger.create_run(args.run_id, args.problem_id)
    ledger.bind_generator_fingerprint(
        args.run_id,
        fingerprint=fingerprint,
        descriptor={
            "app_server_schema_digest": capability.schema_digest,
            "hotjoin_adapter": adapter_commitment,
            "codex_version": capability.codex_version,
            "cwd": str(cwd),
            "effort": args.effort,
            "mcp_command": mcp["command"],
            "mcp_cwd": mcp["cwd"],
            "mcp_role": "reasoning_agent",
            "model": args.model,
            "rotatable_secret_env_keys": rotatable_secret_env_keys,
            "sandbox": "workspace-write",
            "resume_supports_provider_model_fallback": (
                capability.resume_supports_provider_model_fallback
            ),
        },
    )
    ledger.assert_not_quarantined(args.run_id)
    thread_params = {
        "allowProviderModelFallback": False,
        "approvalPolicy": "never",
        "config": {
            "mcp_servers": {"reasoning_agent": mcp},
            "model_reasoning_effort": args.effort,
            "shell_environment_policy": shell_policy,
            "web_search": args.web_mode,
        },
        "cwd": str(cwd),
        "ephemeral": False,
        "model": args.model,
        "sandbox": "workspace-write",
    }
    command = [
        args.codex_bin,
        "app-server",
        "--listen",
        "stdio://",
        "--strict-config",
    ]
    with AppServerClient(command) as client:
        adapter = GeneratorHotJoin(
            ledger,
            args.run_id,
            client,
            idle_grace_seconds=args.idle_grace_seconds,
            resume_supports_provider_model_fallback=(
                capability.resume_supports_provider_model_fallback
            ),
        )
        result = adapter.run(
            initial_prompt=args.prompt,
            thread_params=thread_params,
            max_runtime_seconds=args.max_runtime_seconds,
        )
    result["capability_preflight"] = {
        "codex_version": capability.codex_version,
        "required_methods": list(capability.required_methods),
        "resume_supports_provider_model_fallback": (
            capability.resume_supports_provider_model_fallback
        ),
        "schema_digest": capability.schema_digest,
    }
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "run-generator":
            _print_json(_run_generator_command(args))
            return 0
        ledger = ConversationLedger(args.db)
        if args.command == "init":
            _print_json(ledger.create_run(args.run_id, args.problem_id))
            return 0
        if args.command == "send":
            _print_json(
                ledger.enqueue_message(
                    args.run_id,
                    text=_read_message(args),
                    mode=args.mode,
                    client_message_id=args.client_message_id,
                )
            )
            return 0
        if args.command == "status":
            _print_json(ledger.status(args.run_id))
            return 0
        if args.command == "tail":
            _print_json(
                {
                    "events": ledger.events(
                        args.run_id,
                        after_sequence=args.after_sequence,
                        limit=args.limit,
                    ),
                    "run_id": args.run_id,
                }
            )
            return 0
        if args.command == "verify-ledger":
            _print_json(ledger.verify_chain(args.run_id))
            return 0
        if args.command == "retry-unknown":
            ledger.retry_unknown(args.run_id, args.message_id)
            _print_json(ledger.status(args.run_id))
            return 0
        if args.command == "retry-unknown-turn":
            ledger.retry_unknown_turn(args.run_id, args.client_message_id)
            _print_json(ledger.status(args.run_id))
            return 0
    except (HotJoinError, OSError, ValueError, sqlite3.Error) as exc:
        print(f"rethlas hot-join error: {exc}", file=sys.stderr)
        return 2
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())

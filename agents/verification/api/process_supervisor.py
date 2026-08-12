#!/usr/bin/env python3
"""Minimal crash-surviving supervisor for one verifier model process tree."""

from __future__ import annotations

import os
import hashlib
import json
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_stop_requested = False
_MODEL_RELEASE_PREFIX = b"RETHLAS_VERIFIER_MODEL_RELEASE_V1\x00"


def _request_stop(_signum: int, _frame: object) -> None:
    global _stop_requested
    _stop_requested = True


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, allow_nan=False, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _process_start_identity(pid: int) -> str:
    completed = subprocess.run(
        ["ps", "-o", "lstart=", "-p", str(pid)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
        text=True,
    )
    identity = completed.stdout.strip()
    if completed.returncode != 0 or not identity:
        raise RuntimeError("cannot bind verifier process start identity")
    return identity


def _write_child_guard(
    path: Path,
    *,
    parent_pid: int,
    deadline_epoch: float,
    child: Any,
    command: list[str],
) -> dict[str, object]:
    if not path.is_absolute() or path.parent.is_symlink():
        raise RuntimeError("verifier child guard path is not trusted")
    path.parent.mkdir(parents=True, exist_ok=True)
    guard = {
        "schema_version": "rethlas_verifier_child_process_guard_v1",
        "service_pid": parent_pid,
        "wrapper_pid": os.getpid(),
        "wrapper_pgid": os.getpgrp(),
        "child_pid": child.pid,
        "child_pgid": os.getpgid(child.pid),
        "child_start_identity": _process_start_identity(child.pid),
        "deadline_utc": datetime.fromtimestamp(
            deadline_epoch, tz=timezone.utc
        ).isoformat(),
        "command_sha256": hashlib.sha256(_canonical_json(command)).hexdigest(),
        # This is a durable dispatch intent: recovery must assume the release
        # may have crossed the pipe boundary and must never redispatch.
        "state": "release_intent_durable",
    }
    encoded = _canonical_json(guard) + b"\n"
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise RuntimeError("verifier child guard write was short")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return guard


def _replace_child_guard(
    path: Path, guard: dict[str, object], *, state: str
) -> dict[str, object]:
    updated = {**guard, "state": state}
    encoded = _canonical_json(updated) + b"\n"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise RuntimeError("verifier child guard update was short")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    parent_descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(parent_descriptor)
    finally:
        os.close(parent_descriptor)
    return updated


class _ForkedChild:
    def __init__(self, pid: int, release_fd: int) -> None:
        self.pid = pid
        self.release_fd = release_fd
        self.returncode: int | None = None

    def poll(self) -> int | None:
        if self.returncode is not None:
            return self.returncode
        waited, status = os.waitpid(self.pid, os.WNOHANG)
        if waited == self.pid:
            self.returncode = os.waitstatus_to_exitcode(status)
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.poll() is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(["forked-verifier-model"], timeout)
            time.sleep(0.01)
        assert self.returncode is not None
        return self.returncode


def _spawn_blocked_model(command: list[str]) -> _ForkedChild:
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - assertions observe the parent/supervisor
        try:
            os.close(write_fd)
            os.setsid()
            released = bytearray()
            while True:
                chunk = os.read(read_fd, 65_536)
                if not chunk:
                    break
                released.extend(chunk)
            os.close(read_fd)
            if not bytes(released).startswith(_MODEL_RELEASE_PREFIX):
                os._exit(124)
            prompt = bytes(released)[len(_MODEL_RELEASE_PREFIX) :]
            with tempfile.TemporaryFile(mode="w+b") as prompt_file:
                prompt_file.write(prompt)
                prompt_file.seek(0)
                os.dup2(prompt_file.fileno(), sys.stdin.fileno())
                # No second open/import of this supervisor occurs.  The child
                # executes only the command bytes already held by the pinned
                # trusted wrapper process.
                os.execvpe(command[0], command, os.environ.copy())
        except BaseException:
            os._exit(127)
    os.close(read_fd)
    child = _ForkedChild(pid, write_fd)
    deadline = time.monotonic() + 1.0
    while True:
        try:
            if os.getpgid(pid) == pid:
                return child
        except ProcessLookupError as exc:
            os.close(write_fd)
            raise RuntimeError("blocked verifier child exited before binding") from exc
        if time.monotonic() >= deadline:
            os.close(write_fd)
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            raise RuntimeError("blocked verifier child did not form its process group")
        time.sleep(0.005)


def _kill_child_group(child: Any) -> None:
    group_id = child.pid
    try:
        os.killpg(group_id, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        child.wait(timeout=0.2)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(group_id, signal.SIGKILL)
    except ProcessLookupError:
        return


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) < 5 or arguments[3] != "--":
        print(
            "usage: process_supervisor.py PARENT_PID DEADLINE_EPOCH CHILD_GUARD -- COMMAND...",
            file=sys.stderr,
        )
        return 2
    try:
        parent_pid = int(arguments[0])
        deadline_epoch = float(arguments[1])
    except ValueError:
        return 2
    child_guard_path = Path(arguments[2])
    command = arguments[4:]
    if parent_pid <= 1 or not command or not child_guard_path.is_absolute():
        return 2

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    # The service persists the wrapper pid/pgid/deadline before closing this
    # stdin pipe. If it dies before that release, EOF plus the parent check
    # exits with zero model dispatch.
    prompt = sys.stdin.buffer.read()
    if _stop_requested or os.getppid() != parent_pid or time.time() >= deadline_epoch:
        return 124

    try:
        child = _spawn_blocked_model(command)
    except OSError:
        return 127
    guard: dict[str, object] | None = None
    try:
        guard = _write_child_guard(
            child_guard_path,
            parent_pid=parent_pid,
            deadline_epoch=deadline_epoch,
            child=child,
            command=command,
        )
        release = memoryview(_MODEL_RELEASE_PREFIX + prompt)
        while release:
            written = os.write(child.release_fd, release)
            if written <= 0:
                raise RuntimeError("verifier model release pipe was short")
            release = release[written:]
        os.close(child.release_fd)
        child.release_fd = -1
        guard = _replace_child_guard(
            child_guard_path, guard, state="released"
        )
    except BaseException:
        if child.release_fd >= 0:
            os.close(child.release_fd)
            child.release_fd = -1
        _kill_child_group(child)
        raise
    while True:
        returncode = child.poll()
        if returncode is not None:
            # A successful direct model may still have left tool children.
            _kill_child_group(child)
            _replace_child_guard(
                child_guard_path, guard, state="completed"
            )
            return returncode
        if _stop_requested or os.getppid() != parent_pid or time.time() >= deadline_epoch:
            terminal_state = (
                "timed_out"
                if time.time() >= deadline_epoch
                else "execution_unknown"
            )
            guard = _replace_child_guard(
                child_guard_path, guard, state=terminal_state
            )
            _kill_child_group(child)
            return 124
        time.sleep(0.05)


if __name__ == "__main__":
    raise SystemExit(main())

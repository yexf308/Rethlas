from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping

import pytest

from agents import hotjoin_adapter as hotjoin


_CODEX_OVERRIDE_ENV = "RETHLAS_TEST_CODEX_BIN"
_DESKTOP_CODEX = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
_REQUIRED_ZERO_MODEL_METHODS = frozenset(
    {
        "initialize",
        "thread/start",
        "mcpServerStatus/list",
        "mcpServer/tool/call",
    }
)

_PROBE_SERVER_SOURCE = r'''
import hashlib
import json
import os
import pathlib
import sys
import time


publication = pathlib.Path(os.environ["RETHLAS_PROBE_PUBLICATION"])
call_log = pathlib.Path(os.environ["RETHLAS_PROBE_CALL_LOG"])
completion_root = pathlib.Path(os.environ["RETHLAS_PROBE_COMPLETION_ROOT"])


def send(payload):
    sys.stdout.write(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    )
    sys.stdout.flush()


def result(request_id, payload):
    send({"jsonrpc": "2.0", "id": request_id, "result": payload})


def tool_result(request_id, payload, *, is_error=False):
    response = {
        "content": [
            {
                "type": "text",
                "text": json.dumps(
                    payload, sort_keys=True, separators=(",", ":")
                ),
            }
        ]
    }
    if is_error:
        response["isError"] = True
    result(request_id, response)


def append_call(name, arguments):
    record = json.dumps(
        {"arguments": arguments, "name": name, "pid": os.getpid()},
        sort_keys=True,
        separators=(",", ":"),
    )
    with call_log.open("a", encoding="utf-8") as stream:
        stream.write(record + "\n")


def checkpoint(request_id, arguments):
    canonical_arguments = json.dumps(
        arguments, sort_keys=True, separators=(",", ":")
    )
    receipt = {
        "batch_id": hashlib.sha256(canonical_arguments.encode("utf-8")).hexdigest(),
        "publication": "single",
    }
    candidate = publication.with_name(
        "." + publication.name + "." + str(os.getpid()) + ".candidate"
    )
    candidate.write_text(
        json.dumps(
            {
                "arguments": arguments,
                "receipt": receipt,
                "winner_pid": os.getpid(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    won_publication = False
    try:
        os.link(candidate, publication)
        won_publication = True
    except FileExistsError:
        prior = json.loads(publication.read_text(encoding="utf-8"))
        if prior["arguments"] != arguments:
            tool_result(
                request_id,
                {"error": "checkpoint arguments differ from the published batch"},
                is_error=True,
            )
            return
        receipt = prior["receipt"]
    finally:
        candidate.unlink(missing_ok=True)

    if won_publication:
        # This deliberately exceeds the primary server's one-second timeout.
        # The durable receipt exists first, so an independent exact replay can
        # recover it while this original handler is still running.
        time.sleep(1.35)
        completion_root.mkdir(parents=True, exist_ok=True)
        (completion_root / (str(os.getpid()) + ".json")).write_text(
            json.dumps(receipt, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
    tool_result(request_id, receipt)


for raw_line in sys.stdin:
    message = json.loads(raw_line)
    method = message.get("method")
    if method == "initialize":
        result(
            message["id"],
            {
                "capabilities": {"tools": {}},
                "protocolVersion": "2025-11-25",
                "serverInfo": {
                    "name": "rethlas-installed-codex-split-probe",
                    "version": "1",
                },
            },
        )
    elif method == "notifications/initialized":
        continue
    elif method == "tools/list":
        result(
            message["id"],
            {
                "tools": [
                    {
                        "description": "content-addressed checkpoint probe",
                        "inputSchema": {"type": "object"},
                        "name": "memory_append_batch",
                    },
                    {
                        "description": "long reasoning-lane timeout probe",
                        "inputSchema": {"type": "object"},
                        "name": "long_probe",
                    },
                ]
            },
        )
    elif method == "tools/call":
        tool_name = message["params"]["name"]
        arguments = message["params"].get("arguments", {})
        append_call(tool_name, arguments)
        if tool_name == "memory_append_batch":
            checkpoint(message["id"], arguments)
        elif tool_name == "long_probe":
            time.sleep(1.15)
            tool_result(message["id"], {"completed": True, "pid": os.getpid()})
        else:
            tool_result(
                message["id"],
                {"error": "unknown probe tool"},
                is_error=True,
            )
'''


class _RecordingAppServerClient(hotjoin.AppServerClient):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.methods: list[str] = []

    def call(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> Any:
        self.methods.append(method)
        return super().call(method, params, timeout_seconds=timeout_seconds)


def _installed_codex() -> tuple[Path, bool]:
    configured = os.environ.get(_CODEX_OVERRIDE_ENV)
    if configured:
        try:
            candidate = Path(configured).expanduser().resolve(strict=True)
        except OSError as exc:
            pytest.fail(f"{_CODEX_OVERRIDE_ENV} does not resolve: {exc}")
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            pytest.fail(f"{_CODEX_OVERRIDE_ENV} is not an executable file: {candidate}")
        return candidate, True

    candidates = [_DESKTOP_CODEX]
    discovered = shutil.which("codex")
    if discovered:
        candidates.append(Path(discovered))
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve(strict=True), False
    pytest.skip(
        "installed Codex binary unavailable; set " + _CODEX_OVERRIDE_ENV
    )


def _schema_methods(codex: Path, schema_root: Path, *, explicit: bool) -> set[str]:
    try:
        completed = subprocess.run(
            [
                str(codex),
                "app-server",
                "generate-json-schema",
                "--experimental",
                "--out",
                str(schema_root),
            ],
            text=True,
            capture_output=True,
            check=False,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        if explicit:
            pytest.fail(f"explicit Codex app-server schema probe failed: {exc}")
        pytest.skip(f"auto-discovered Codex cannot generate app-server schema: {exc}")
    schema_path = schema_root / "codex_app_server_protocol.v2.schemas.json"
    if completed.returncode != 0 or not schema_path.is_file():
        detail = completed.stderr.strip() or completed.stdout.strip() or "no schema"
        if explicit:
            pytest.fail(f"explicit Codex lacks a usable app-server schema: {detail}")
        pytest.skip(f"auto-discovered Codex lacks a usable app-server schema: {detail}")
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        if explicit:
            pytest.fail(f"explicit Codex emitted an invalid app-server schema: {exc}")
        pytest.skip(f"auto-discovered Codex emitted an invalid app-server schema: {exc}")

    methods: set[str] = set()
    pending: list[object] = [schema]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            method = value.get("method")
            if isinstance(method, dict) and isinstance(method.get("enum"), list):
                methods.update(item for item in method["enum"] if isinstance(item, str))
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    missing = sorted(_REQUIRED_ZERO_MODEL_METHODS - methods)
    if missing:
        detail = "installed Codex app-server schema lacks: " + ", ".join(missing)
        if explicit:
            pytest.fail(detail)
        pytest.skip(detail)
    return methods


def _isolated_process_env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "isolated-home"
    codex_home = tmp_path / "isolated-codex-home"
    temp_root = tmp_path / "isolated-tmp"
    for directory in (home, codex_home, temp_root):
        directory.mkdir()
    # Deliberately do not inherit API keys, login state, proxy settings, or any
    # other user configuration.  No turn is started, so this probe needs no
    # authentication, model request, or network access.
    return {
        "CODEX_HOME": str(codex_home),
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": os.defpath,
        "TMPDIR": str(temp_root),
    }


def _tool_json(response: object) -> dict[str, Any]:
    assert isinstance(response, dict)
    assert response.get("isError") in (None, False)
    content = response.get("content")
    assert isinstance(content, list) and len(content) == 1
    item = content[0]
    assert isinstance(item, dict) and item.get("type") == "text"
    decoded = json.loads(item["text"])
    assert isinstance(decoded, dict)
    return decoded


def _pid_is_live(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_installed_codex_three_server_checkpoint_split_is_zero_model(
    tmp_path: Path,
) -> None:
    started_at = time.monotonic()
    codex, explicit = _installed_codex()
    _schema_methods(codex, tmp_path / "schema", explicit=explicit)

    probe_server = tmp_path / "probe_mcp_server.py"
    probe_server.write_text(_PROBE_SERVER_SOURCE, encoding="utf-8")
    publication = tmp_path / "checkpoint-publication.json"
    call_log = tmp_path / "mcp-calls.jsonl"
    completion_root = tmp_path / "handler-completions"
    shared_environment = {
        "RETHLAS_PROBE_CALL_LOG": str(call_log),
        "RETHLAS_PROBE_COMPLETION_ROOT": str(completion_root),
        "RETHLAS_PROBE_PUBLICATION": str(publication),
    }
    base = {
        "args": ["-u", str(probe_server)],
        "command": sys.executable,
        "cwd": str(tmp_path),
        "default_tools_approval_mode": "approve",
        "env": shared_environment,
        "required": True,
        "tool_timeout_sec": 3600,
    }
    servers = hotjoin._derive_reasoning_mcp_server_map(base)
    assert servers["reasoning_agent"]["tool_timeout_sec"] == 3600
    assert servers["reasoning_checkpoint_primary"]["tool_timeout_sec"] == 60
    assert servers["reasoning_checkpoint_recovery"]["tool_timeout_sec"] == 60

    # Preserve the production role split while shortening wall-clock time for
    # this installed-binary integration probe.
    servers["reasoning_agent"]["tool_timeout_sec"] = 3
    servers["reasoning_checkpoint_primary"]["tool_timeout_sec"] = 1
    servers["reasoning_checkpoint_recovery"]["tool_timeout_sec"] = 1

    client = _RecordingAppServerClient(
        [str(codex), "app-server", "--listen", "stdio://", "--strict-config"],
        process_env=_isolated_process_env(tmp_path),
        rpc_timeout_seconds=5,
        close_grace_seconds=0.5,
    )
    app_server_pid: int | None = None
    mcp_pids: set[int] = set()
    with client:
        assert client.process is not None
        app_server_pid = int(client.process.pid)
        thread = client.call(
            "thread/start",
            {
                "allowProviderModelFallback": False,
                "approvalPolicy": "never",
                "config": {
                    "mcp_servers": servers,
                    "web_search": "disabled",
                },
                "cwd": str(tmp_path),
                "ephemeral": True,
                "model": "gpt-5.6-sol",
                "sandbox": "read-only",
            },
            timeout_seconds=5,
        )
        assert isinstance(thread, dict)
        thread_id = thread["thread"]["id"]

        status = client.call(
            "mcpServerStatus/list",
            {
                "detail": "toolsAndAuthOnly",
                "limit": 10,
                "threadId": thread_id,
            },
            timeout_seconds=3,
        )
        assert isinstance(status, dict)
        assert status.get("nextCursor") is None
        inventories = {
            entry["name"]: sorted(entry["tools"])
            for entry in status["data"]
            if entry["name"] in hotjoin.REASONING_MCP_SERVER_IDS
        }
        assert inventories == {
            "reasoning_agent": ["long_probe"],
            "reasoning_checkpoint_primary": ["memory_append_batch"],
            "reasoning_checkpoint_recovery": ["memory_append_batch"],
        }

        frozen_arguments = {
            "items": [
                {
                    "kind": "route_atomization",
                    "text": "installed Codex exact-replay probe",
                }
            ],
            "problem_id": "zero-model/checkpoint-split",
        }
        primary_started_at = time.monotonic()
        with pytest.raises(hotjoin.RpcError) as primary_failure:
            client.call(
                "mcpServer/tool/call",
                {
                    "arguments": frozen_arguments,
                    "server": "reasoning_checkpoint_primary",
                    "threadId": thread_id,
                    "tool": "memory_append_batch",
                },
                timeout_seconds=3,
            )
        primary_elapsed = time.monotonic() - primary_started_at
        assert 0.8 <= primary_elapsed < 2.5
        assert primary_failure.value.error["code"] == -32603
        timeout_message = primary_failure.value.error["message"]
        assert timeout_message in {
            "tool call failed for "
            "`reasoning_checkpoint_primary/memory_append_batch`: "
            "timed out awaiting tools/call after 1s",
            "tool call failed for "
            "`reasoning_checkpoint_primary/memory_append_batch`: "
            "timed out awaiting tools/call after 1000ms",
        }
        assert publication.is_file()
        published = json.loads(publication.read_text(encoding="utf-8"))
        assert published["arguments"] == frozen_arguments

        recovered = _tool_json(
            client.call(
                "mcpServer/tool/call",
                {
                    "arguments": frozen_arguments,
                    "server": "reasoning_checkpoint_recovery",
                    "threadId": thread_id,
                    "tool": "memory_append_batch",
                },
                timeout_seconds=3,
            )
        )
        assert recovered == published["receipt"]

        with pytest.raises(hotjoin.RpcError) as disabled_failure:
            client.call(
                "mcpServer/tool/call",
                {
                    "arguments": frozen_arguments,
                    "server": "reasoning_agent",
                    "threadId": thread_id,
                    "tool": "memory_append_batch",
                },
                timeout_seconds=3,
            )
        assert disabled_failure.value.error["code"] == -32603
        assert "is disabled for MCP server 'reasoning_agent'" in (
            disabled_failure.value.error["message"]
        )

        long_started_at = time.monotonic()
        long_result = _tool_json(
            client.call(
                "mcpServer/tool/call",
                {
                    "arguments": {},
                    "server": "reasoning_agent",
                    "threadId": thread_id,
                    "tool": "long_probe",
                },
                timeout_seconds=4,
            )
        )
        long_elapsed = time.monotonic() - long_started_at
        assert 1.0 <= long_elapsed < 3
        assert long_result["completed"] is True

        calls = [
            json.loads(line)
            for line in call_log.read_text(encoding="utf-8").splitlines()
        ]
        batch_calls = [
            call for call in calls if call["name"] == "memory_append_batch"
        ]
        assert len(batch_calls) == 2
        assert batch_calls[0]["arguments"] == frozen_arguments
        assert batch_calls[1]["arguments"] == frozen_arguments
        assert batch_calls[0]["pid"] != batch_calls[1]["pid"]
        assert [call["name"] for call in calls].count("long_probe") == 1
        mcp_pids = {int(call["pid"]) for call in calls}
        assert len(mcp_pids) == 3
        assert int(published["winner_pid"]) == int(batch_calls[0]["pid"])
        assert (
            completion_root / (str(published["winner_pid"]) + ".json")
        ).is_file(), "the primary handler did not continue after Codex timed out"
        assert list(tmp_path.glob("checkpoint-publication.json")) == [publication]

    expected_methods = [
        "initialize",
        "thread/start",
        "mcpServerStatus/list",
        "mcpServer/tool/call",
        "mcpServer/tool/call",
        "mcpServer/tool/call",
        "mcpServer/tool/call",
    ]
    assert client.methods == expected_methods
    assert "turn/start" not in client.methods
    assert app_server_pid is not None
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline and any(
        _pid_is_live(pid) for pid in {app_server_pid, *mcp_pids}
    ):
        time.sleep(0.01)
    assert not any(_pid_is_live(pid) for pid in {app_server_pid, *mcp_pids})
    assert time.monotonic() - started_at < 9.5

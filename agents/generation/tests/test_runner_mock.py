from __future__ import annotations

import hashlib
import json
import os
import py_compile
import selectors
import shutil
import sqlite3
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from agents.generation.guardian_launcher import LAUNCH_MANIFEST_SCHEMA_SHA256
from agents.hotjoin_adapter import GUARDIAN_CONTROL_SCHEMA_SHA256


RUNNER = Path(__file__).with_name("run_example.sh")
GENERATION_ROOT = RUNNER.parents[1]
REQUIRED_MODULES = (
    "mcp",
    "requests",
    "numpy",
    "scipy",
    "sympy",
    "mpmath",
    "gmpy2",
)

TRUSTED_MCP_LOGICAL_MODULES = (
    "review.contracts",
    "review.critic",
    "mcp.proof_context",
    "mcp.advisor_client",
    "mcp.review_client",
    "mcp.verification_client",
    "mcp.server",
)


def _trusted_mcp_loader_source() -> str:
    runner_source = RUNNER.read_text(encoding="utf-8")
    opening = "TRUSTED_MCP_SECURE_LOADER=\"$(cat <<'PY'\n"
    start = runner_source.index(opening) + len(opening)
    end = runner_source.index("\nPY\n)\"", start)
    return runner_source[start:end]


def _trusted_mcp_snapshot(tmp_path: Path) -> tuple[Path, list[str]]:
    snapshot = tmp_path / "trusted-runtime"
    arguments: list[str] = []
    for logical_name in TRUSTED_MCP_LOGICAL_MODULES:
        relative = Path(*logical_name.split(".")).with_suffix(".py")
        source = (
            GENERATION_ROOT.parent / relative
            if logical_name.startswith("review.")
            else GENERATION_ROOT / relative
        )
        target = snapshot / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        target.chmod(0o400)
        arguments.extend(
            [
                logical_name,
                str(target),
                hashlib.sha256(target.read_bytes()).hexdigest(),
            ]
        )
    return snapshot, arguments


def _mcp_stdio_probe(
    command: list[str],
    *,
    cwd: Path,
    generation_root: Path,
    python_executable: Path,
) -> subprocess.CompletedProcess[str]:
    home = cwd / "home"
    home.mkdir(exist_ok=True)
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "rethlas-zero-model-probe", "version": "1"},
        },
    }
    initialized = {
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
        "params": {},
    }
    tools_list = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/list",
        "params": {},
    }
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env={
            "HOME": str(home),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": f"{python_executable.parent}:/usr/bin:/bin",
            "PYTHONDONTWRITEBYTECODE": "1",
            "RETHLAS_GENERATION_ROOT": str(generation_root),
        },
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    output_lines: list[str] = []
    try:
        process.stdin.write(json.dumps(initialize, separators=(",", ":")) + "\n")
        process.stdin.flush()
        if not selector.select(timeout=20):
            raise AssertionError("timed out waiting for MCP initialize response")
        first_line = process.stdout.readline()
        output_lines.append(first_line)
        if not first_line:
            process.stdin.close()
            process.stdin = None
            stdout_tail, stderr = process.communicate(timeout=20)
            output_lines.append(stdout_tail)
            return subprocess.CompletedProcess(
                command,
                process.returncode,
                "".join(output_lines),
                stderr,
            )

        for request in (initialized, tools_list):
            process.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
        process.stdin.flush()
        if not selector.select(timeout=20):
            raise AssertionError("timed out waiting for MCP tools/list response")
        output_lines.append(process.stdout.readline())

        process.stdin.close()
        process.stdin = None
        stdout_tail, stderr = process.communicate(timeout=20)
        output_lines.append(stdout_tail)
    except BaseException:
        process.kill()
        process.wait(timeout=5)
        raise
    finally:
        selector.close()
    return subprocess.CompletedProcess(
        command,
        process.returncode,
        "".join(output_lines),
        stderr,
    )


def _real_mcp_python() -> Path:
    configured = os.environ.get("RETHLAS_TEST_MCP_PYTHON")
    executable = (
        Path(configured).resolve(strict=True) if configured else Path(sys.executable)
    )
    probe = subprocess.run(
        [
            str(executable),
            "-I",
            "-B",
            "-c",
            (
                "try:\n"
                " from mcp.server.fastmcp import FastMCP\n"
                "except ImportError:\n"
                " from mcp.server.mcpserver import MCPServer as FastMCP\n"
                "import mcp.types\n"
                "assert callable(FastMCP)\n"
            ),
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    if probe.returncode != 0:
        if configured:
            pytest.fail(
                "RETHLAS_TEST_MCP_PYTHON lacks a compatible official MCP SDK: "
                + probe.stderr
            )
        pytest.skip("official MCP SDK unavailable; set RETHLAS_TEST_MCP_PYTHON")
    return executable


@pytest.mark.parametrize("entry", ["secure-loader", "direct-snapshot"])
def test_trusted_reasoning_mcp_completes_real_stdio_handshake(
    tmp_path: Path,
    entry: str,
) -> None:
    mcp_python = _real_mcp_python()
    snapshot, module_arguments = _trusted_mcp_snapshot(tmp_path)
    generation_root = tmp_path / "generation"
    generation_root.mkdir()
    command = (
        [
            str(mcp_python),
            "-I",
            "-B",
            "-c",
            _trusted_mcp_loader_source(),
            *module_arguments,
        ]
        if entry == "secure-loader"
        else [str(mcp_python), "-I", "-B", str(snapshot / "mcp" / "server.py")]
    )

    completed = _mcp_stdio_probe(
        command,
        cwd=generation_root,
        generation_root=generation_root,
        python_executable=mcp_python,
    )

    assert completed.returncode == 0, completed.stderr
    responses = [json.loads(line) for line in completed.stdout.splitlines()]
    assert [item["id"] for item in responses] == [1, 2]
    assert responses[0]["result"]["serverInfo"]["name"] == "reasoning-agent"
    tools = responses[1]["result"]["tools"]
    assert {item["name"] for item in tools} >= {
        "memory_search",
        "context_handoff_get",
        "route_review_status",
    }


def test_trusted_reasoning_mcp_loader_rejects_changed_module_before_stdio(
    tmp_path: Path,
) -> None:
    snapshot, module_arguments = _trusted_mcp_snapshot(tmp_path)
    server_path = snapshot / "mcp" / "server.py"
    server_path.chmod(0o600)
    server_path.write_bytes(b"# changed after commitment\n")
    server_path.chmod(0o400)
    generation_root = tmp_path / "generation"
    generation_root.mkdir()

    completed = _mcp_stdio_probe(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            _trusted_mcp_loader_source(),
            *module_arguments,
        ],
        cwd=generation_root,
        generation_root=generation_root,
        python_executable=Path(sys.executable),
    )

    assert completed.returncode == 70
    assert "module SHA-256 mismatch" in completed.stderr
    assert completed.stdout == ""


def test_trusted_reasoning_mcp_loader_rejects_preloaded_private_alias(
    tmp_path: Path,
) -> None:
    snapshot, module_arguments = _trusted_mcp_snapshot(tmp_path)
    generation_root = tmp_path / "generation"
    generation_root.mkdir()
    loader_source = (
        "import sys, types\n"
        "sys.modules['_rethlas_generation_mcp'] = "
        "types.ModuleType('_rethlas_generation_mcp')\n"
        + _trusted_mcp_loader_source()
    )

    completed = _mcp_stdio_probe(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            loader_source,
            *module_arguments,
        ],
        cwd=generation_root,
        generation_root=generation_root,
        python_executable=Path(sys.executable),
    )

    assert completed.returncode == 70
    assert "trusted runtime package alias is already loaded" in completed.stderr
    assert completed.stdout == ""


_MOCK_GUARDIAN_LAUNCHER = r"""from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import secrets
import subprocess
import sys


PRIVILEGED_TOKEN_ENV_NAMES = (
    "RETHLAS_REVIEW_CONTROL_TOKEN",
    "RETHLAS_GUARDIAN_CYCLE_TOKEN",
    "RETHLAS_RUNNER_CYCLE_TOKEN",
    "RETHLAS_STALE_RECOVERY_TOKEN",
)


def canonical(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def consume_token_fd(descriptor: int, *, label: str) -> str:
    assert descriptor >= 3
    raw = b""
    try:
        while len(raw) <= 64:
            chunk = os.read(descriptor, 65 - len(raw))
            if not chunk:
                break
            raw += chunk
    finally:
        os.close(descriptor)
    assert len(raw) == 64, f"{label} capability length mismatch"
    token = raw.decode("ascii")
    assert re.fullmatch(r"[0-9a-f]{64}", token) is not None
    return token


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser()
    value.add_argument("--owner-token-fd", type=int, required=True)
    value.add_argument("--db", type=pathlib.Path, required=True)
    value.add_argument("--adapter-path", type=pathlib.Path, required=True)
    value.add_argument("--adapter-sha256", required=True)
    value.add_argument("--guardian-path", type=pathlib.Path, required=True)
    value.add_argument("--runner-path", type=pathlib.Path, required=True)
    value.add_argument("--run-id", required=True)
    value.add_argument("--generation-control-instance-id", required=True)
    value.add_argument("--watchdog-id", required=True)
    value.add_argument(
        "--admission-mode",
        choices=("initial_new_cycle", "next_new_cycle", "same_cycle_resume"),
        required=True,
    )
    value.add_argument("--expected-cycle-id", required=True)
    value.add_argument("--expected-generation", type=int, required=True)
    value.add_argument("--expected-clock-sha256")
    value.add_argument("--capability-revision", type=int, required=True)
    value.add_argument("--policy-contract-sha256", required=True)
    value.add_argument("--policy-digest", required=True)
    value.add_argument("--worker-cwd", type=pathlib.Path, required=True)
    value.add_argument("--problem-path", type=pathlib.Path, required=True)
    value.add_argument("--problem-relative-path", required=True)
    value.add_argument("--handoff-candidate-path", type=pathlib.Path)
    value.add_argument(
        "--worker-mode",
        choices=("runner_control", "opaque_guarded_command"),
        default="runner_control",
    )
    value.add_argument("worker_command", nargs=argparse.REMAINDER)
    return value


def main() -> int:
    arguments = sys.argv[1:]
    args = parser().parse_args(arguments)
    owner_token = consume_token_fd(args.owner_token_fd, label="owner")
    assert owner_token not in canonical(arguments)
    assert all(owner_token not in value for value in os.environ.values())
    assert all(name not in os.environ for name in PRIVILEGED_TOKEN_ENV_NAMES)
    assert args.worker_mode == "runner_control"
    assert args.expected_generation >= 1
    assert args.capability_revision >= 1
    assert re.fullmatch(r"cycle_[0-9a-f]{32}", args.expected_cycle_id) is not None
    assert re.fullmatch(r"[0-9a-f]{64}", args.policy_contract_sha256) is not None
    assert re.fullmatch(r"[0-9a-f]{64}", args.policy_digest) is not None
    if args.admission_mode == "same_cycle_resume":
        assert re.fullmatch(r"[0-9a-f]{64}", args.expected_clock_sha256 or "")
    else:
        assert args.expected_clock_sha256 is None

    for source in (
        args.adapter_path,
        args.guardian_path,
        args.runner_path,
        args.problem_path,
    ):
        assert source.is_absolute() and source.is_file() and not source.is_symlink()
    assert hashlib.sha256(args.adapter_path.read_bytes()).hexdigest() == (
        args.adapter_sha256
    )
    worker = list(args.worker_command)
    if worker and worker[0] == "--":
        worker.pop(0)
    assert len(worker) >= 2
    assert pathlib.Path(worker[0]).is_absolute()
    assert not pathlib.Path(worker[0]).is_symlink()
    assert pathlib.Path(worker[1]).resolve() == args.adapter_path.resolve()
    assert "--runner-token-fd" not in worker
    assert "--control-token-fd" not in worker

    runner_token = secrets.token_hex(32)
    runner_read, runner_write = os.pipe()
    try:
        assert os.write(runner_write, runner_token.encode("ascii")) == 64
    finally:
        os.close(runner_write)
    child_environment = dict(os.environ)
    for name in PRIVILEGED_TOKEN_ENV_NAMES:
        child_environment.pop(name, None)

    calls_file = os.environ.get("MOCK_GUARDIAN_LAUNCHER_CALLS_FILE")
    if calls_file:
        record = {
            "admission_mode": args.admission_mode,
            "argv": arguments,
            "capability_revision": args.capability_revision,
            "expected_clock_sha256": args.expected_clock_sha256,
            "expected_cycle_id": args.expected_cycle_id,
            "expected_generation": args.expected_generation,
            "owner_token_sha256": hashlib.sha256(
                owner_token.encode("ascii")
            ).hexdigest(),
            "capability_env_present": any(
                name in os.environ for name in PRIVILEGED_TOKEN_ENV_NAMES
            ),
            "runner_token_sha256": hashlib.sha256(
                runner_token.encode("ascii")
            ).hexdigest(),
            "worker_command": worker,
        }
        with pathlib.Path(calls_file).open("a", encoding="utf-8") as handle:
            handle.write(canonical(record) + "\n")

    if os.environ.get("MOCK_GUARDIAN_LAUNCHER_FAIL_BEFORE_DISPATCH"):
        print("mock guardian pre-dispatch failure", file=sys.stderr)
        return 70

    runtime_command = [worker[0], "-I", "-B", *worker[1:]]
    runtime_command.extend(("--runner-token-fd", str(runner_read)))
    try:
        completed = subprocess.run(
            runtime_command,
            cwd=args.worker_cwd,
            env=child_environment,
            pass_fds=(runner_read,),
            check=False,
        )
    finally:
        os.close(runner_read)
    result = {
        "report": {"direct_returncode": completed.returncode},
        "state": "completed" if completed.returncode == 0 else "failed",
    }
    print(canonical(result))
    return 0 if completed.returncode == 0 else 70


if __name__ == "__main__":
    raise SystemExit(main())
"""


def _site_packages(runtime_bin: Path) -> Path:
    return Path(
        subprocess.run(
            [
                str(runtime_bin / "python3"),
                "-I",
                "-B",
                "-c",
                "import sysconfig; print(sysconfig.get_paths()['purelib'])",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )


def _make_math_runtime(agents_dir: Path) -> Path:
    runtime = agents_dir / ".generation-venv"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "venv",
            "--copies",
            "--without-pip",
            str(runtime),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    runtime_bin = runtime / "bin"
    site_packages = _site_packages(runtime_bin)
    for module_name in REQUIRED_MODULES:
        package = site_packages / module_name
        package.mkdir()
        module_source = ""
        if module_name == "mcp":
            # Most runner tests exercise transport/control behavior without a
            # real MCP session.  This structural stub lets the trusted server
            # register its decorators; the dedicated stdio tests above use the
            # production official MCP SDK and would catch namespace shadowing.
            server_package = package / "server"
            server_package.mkdir()
            (server_package / "__init__.py").write_text("", encoding="utf-8")
            (server_package / "fastmcp.py").write_text("""class FastMCP:
    def __init__(self, name):
        self.name = name

    def tool(self, *, name):
        def register(function):
            return function
        return register

    def run(self):
        return None
""", encoding="utf-8")
            (package / "types.py").write_text("", encoding="utf-8")
        elif module_name == "requests":
            # The trusted verification client subclasses the public requests
            # base exception at import time; network calls remain outside this
            # runner-only mock suite.
            module_source = "class RequestException(Exception):\n    pass\n"
        (package / "__init__.py").write_text(module_source, encoding="utf-8")
    return runtime_bin


def _module_stub(fake_bin: Path, module_name: str) -> Path:
    return _site_packages(fake_bin) / module_name


def _make_runner_tree(tmp_path: Path) -> tuple[Path, Path]:
    generation = tmp_path / "agents" / "generation"
    tests_dir = generation / "tests"
    data_dir = generation / "data"
    tests_dir.mkdir(parents=True)
    data_dir.mkdir()
    shutil.copy2(RUNNER, tests_dir / "run_example.sh")
    shutil.copy2(GENERATION_ROOT / "AGENTS.md", generation / "AGENTS.md")
    shutil.copy2(GENERATION_ROOT / "guardian.py", generation / "guardian.py")
    (generation / "guardian_launcher.py").write_text(
        _MOCK_GUARDIAN_LAUNCHER,
        encoding="utf-8",
    )
    shutil.copy2(
        GENERATION_ROOT.parent / "advisor_bridge.py",
        generation.parent / "advisor_bridge.py",
    )
    shutil.copy2(
        GENERATION_ROOT / "requirements-math-research.txt",
        generation / "requirements-math-research.txt",
    )
    shutil.copytree(GENERATION_ROOT / ".codex", generation / ".codex")
    shutil.copytree(GENERATION_ROOT / ".agents", generation / ".agents")
    shutil.copytree(
        GENERATION_ROOT / "mcp",
        generation / "mcp",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    shutil.copytree(
        GENERATION_ROOT.parent / "review",
        generation.parent / "review",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    (data_dir / "example.md").write_text("S", encoding="utf-8")

    fake_bin = _make_math_runtime(generation.parent)
    fake_codex = fake_bin / "codex"
    fake_codex.write_text(
        """#!/usr/bin/env python3
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tomllib

calls_file = os.environ.get("MOCK_CODEX_CALLS_FILE")
if calls_file:
    with pathlib.Path(calls_file).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(sys.argv) + "\\n")
if "--version" in sys.argv:
    print("codex-mock 1.0")
    raise SystemExit(0)
assert "--dangerously-bypass-approvals-and-sandbox" not in sys.argv
assert sys.argv[sys.argv.index("--sandbox") + 1] == "workspace-write"
shell_policy_configs = [
    value
    for index, value in enumerate(sys.argv)
    if index > 0
    and sys.argv[index - 1] == "--config"
    and value.startswith("shell_environment_policy=")
]
assert len(shell_policy_configs) == 1
shell_policy = tomllib.loads(
    "value=" + shell_policy_configs[0].split("=", 1)[1]
)["value"]
assert shell_policy == {
    "inherit": "none",
    "set": {
        "PATH": (
            f"{pathlib.Path(sys.executable).parent.resolve()}"
            ":/usr/bin:/bin:/usr/sbin:/sbin"
        )
    },
}
safe_path = shell_policy["set"]["PATH"]
assert pathlib.Path(shutil.which("python", path=safe_path)).resolve() == (
    pathlib.Path(sys.executable).parent / "python"
).resolve()
assert pathlib.Path(shutil.which("python3", path=safe_path)).resolve() == (
    pathlib.Path(sys.executable).parent / "python3"
).resolve()
(pathlib.Path.cwd() / "shell_environment_policy_seen.json").write_text(
    json.dumps(shell_policy), encoding="utf-8"
)
reasoning_mcp_configs = [
    value
    for index, value in enumerate(sys.argv)
    if index > 0
    and sys.argv[index - 1] == "--config"
    and value.startswith("mcp_servers.reasoning_")
]
assert len(reasoning_mcp_configs) == 3
reasoning_mcp_servers = {
    raw.split("=", 1)[0].removeprefix("mcp_servers."): tomllib.loads(
        "value=" + raw.split("=", 1)[1]
    )["value"]
    for raw in reasoning_mcp_configs
}
assert set(reasoning_mcp_servers) == {
    "reasoning_agent",
    "reasoning_checkpoint_primary",
    "reasoning_checkpoint_recovery",
}
reasoning_mcp = reasoning_mcp_servers["reasoning_agent"]
if os.environ.get("MOCK_EXPECT_NO_ADVISOR_ENV"):
    for name in (
        "RETHLAS_ADVISOR_RECEIPTS_ROOT",
        "RETHLAS_EXPECTED_HOTJOIN_RUN_ID",
    ):
        assert name not in os.environ
        assert all(name not in server["env"] for server in reasoning_mcp_servers.values())
assert set(reasoning_mcp) == {
    "command",
    "args",
    "cwd",
    "env",
    "required",
    "tool_timeout_sec",
    "default_tools_approval_mode",
    "disabled_tools",
}
for checkpoint_id in (
    "reasoning_checkpoint_primary",
    "reasoning_checkpoint_recovery",
):
    assert set(reasoning_mcp_servers[checkpoint_id]) == {
        "command",
        "args",
        "cwd",
        "env",
        "required",
        "tool_timeout_sec",
        "default_tools_approval_mode",
        "enabled_tools",
    }
common_keys = {
    "command",
    "args",
    "cwd",
    "env",
    "required",
    "default_tools_approval_mode",
}
assert len(
    {
        json.dumps(
            {key: server[key] for key in common_keys},
            sort_keys=True,
            separators=(",", ":"),
        )
        for server in reasoning_mcp_servers.values()
    }
) == 1
assert reasoning_mcp["disabled_tools"] == ["memory_append_batch"]
for checkpoint_id in (
    "reasoning_checkpoint_primary",
    "reasoning_checkpoint_recovery",
):
    checkpoint = reasoning_mcp_servers[checkpoint_id]
    assert checkpoint["enabled_tools"] == ["memory_append_batch"]
    assert checkpoint["tool_timeout_sec"] == 60
    assert checkpoint["required"] is True
    assert checkpoint["default_tools_approval_mode"] == "approve"
assert pathlib.Path(reasoning_mcp["command"]).is_absolute()
assert pathlib.Path(reasoning_mcp["command"]).resolve() == pathlib.Path(
    sys.executable
).resolve()
loader_args = reasoning_mcp["args"]
assert loader_args[:3] == ["-I", "-B", "-c"]
assert "trusted MCP secure-loader failed" in loader_args[3]
module_arguments = loader_args[4:]
assert len(module_arguments) == 21
trusted_mcp_modules = {
    module_arguments[index]: pathlib.Path(module_arguments[index + 1])
    for index in range(0, len(module_arguments), 3)
}
assert list(trusted_mcp_modules) == [
    "review.contracts",
    "review.critic",
    "mcp.proof_context",
    "mcp.advisor_client",
    "mcp.review_client",
    "mcp.verification_client",
    "mcp.server",
]
for index in range(0, len(module_arguments), 3):
    module_path = pathlib.Path(module_arguments[index + 1])
    module_sha256 = module_arguments[index + 2]
    assert module_path.is_absolute() and module_path.is_file()
    assert hashlib.sha256(module_path.read_bytes()).hexdigest() == module_sha256
assert pathlib.Path(reasoning_mcp["cwd"]).resolve() == pathlib.Path.cwd().resolve()
assert reasoning_mcp["tool_timeout_sec"] == 3600
assert reasoning_mcp["required"] is True
# Every trusted MCP role is noninteractive; approval_policy=never cannot cancel
# a call while waiting for an unavailable prompt.
assert reasoning_mcp["default_tools_approval_mode"] == "approve"
assert "NumPy, SciPy, SymPy, mpmath, and gmpy2" in sys.argv[-1]
(pathlib.Path.cwd() / "reasoning_mcp_config_seen.json").write_text(
    json.dumps(reasoning_mcp), encoding="utf-8"
)
(pathlib.Path.cwd() / "reasoning_mcp_server_map_seen.json").write_text(
    json.dumps(reasoning_mcp_servers), encoding="utf-8"
)
if os.environ.get("MOCK_EXPECT_VERIFY_PROOF_URL"):
    assert os.environ["VERIFY_PROOF_URL"] == os.environ["MOCK_EXPECT_VERIFY_PROOF_URL"]
if os.environ.get("MOCK_EXPECT_VERIFY_API_TOKEN"):
    assert os.environ["VERIFY_API_TOKEN"] == os.environ["MOCK_EXPECT_VERIFY_API_TOKEN"]
root = pathlib.Path.cwd()
problem_id = os.environ["RETHLAS_EXPECTED_PROBLEM_ID"]
generation_control_state = os.environ.get("MOCK_GENERATION_CONTROL_STATE")
if generation_control_state:
    assert generation_control_state in {
        "waiting_cost_gate",
        "waiting_owner_advisor_decision",
    }
    snapshot_mcp = trusted_mcp_modules["mcp.server"].resolve().parent
    sys.path.insert(0, str(snapshot_mcp))
    import server as trusted_generation_server

    if generation_control_state == "waiting_cost_gate":
        event_payload = {
            "event_type": "recursive_proving_round",
            "status": generation_control_state,
        }
    else:
        event_payload = {
            "event_type": "advisor_checkpoint",
            "status": generation_control_state,
            "owner_action_required": True,
            "browser_dispatch_authorized": False,
            "advisor_request_id": None,
        }
    event_receipt = trusted_generation_server.memory_append(
        problem_id, "events", event_payload
    )
    branch_receipt = trusted_generation_server.branch_update(
        problem_id,
        "mock-control-branch",
        {"status": generation_control_state},
    )
    # Cadence-disabled legacy transport has no authenticated host handoff on
    # which the public generation_yield tool can close. Seed the already
    # validated control record directly here; the hot-join tests below exercise
    # the real handoff -> generation_yield_prepare -> cadence-close handshake.
    trusted_generation_server._set_generation_control(
        problem_id,
        instance_id=os.environ["RETHLAS_GENERATION_CONTROL_TOKEN"],
        state=generation_control_state,
        reason="mock evidence-backed unfinished yield",
        evidence_record_ids=[event_receipt["record_id"], branch_receipt["record_id"]],
    )
verified = root / "results" / problem_id / "blueprint_verified.md"
verified.parent.mkdir(parents=True, exist_ok=True)
proof = b"mock verified proof"
verified.write_bytes(proof)
if os.environ.get("MOCK_PUBLICATION") == "trusted":
    sys.path.insert(0, str(root / "mcp"))
    from proof_context import (
        aggregate_adaptive_context_digest,
        aggregate_context_digest,
        build_item_context,
        parse_blueprint,
    )
    manifest = parse_blueprint(proof.decode("utf-8"), target_statement="S")
    attestations = []
    for item_id in manifest.item_ids:
        context = build_item_context(manifest, item_id, max_chars=200000)
        attestations.append({
            "item_id": item_id,
            "disposition": "verified",
            "final_round": 0,
            "expanded_proof_ids": [],
            "max_chars": 200000,
            "context_digest": context["digest"],
            "verdict": "correct",
        })
    receipt = pathlib.Path(os.environ["RETHLAS_RECEIPTS_ROOT"]) / f"{problem_id}.json"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps({
        "schema_version": "rethlas-publication-v2",
        "problem_id": problem_id,
        "statement_digest": os.environ["RETHLAS_EXPECTED_STATEMENT_SHA256"],
        "proof_digest": hashlib.sha256(proof).hexdigest(),
        "context_digest": aggregate_context_digest(manifest),
        "adaptive_context_digest": aggregate_adaptive_context_digest(
            manifest, attestations
        ),
        "item_context_attestations": attestations,
        "checked_item_ids": list(manifest.item_ids),
        "verified_path": str(verified.absolute()),
        "published_bytes": len(proof),
    }), encoding="utf-8")
elif os.environ.get("MOCK_PUBLICATION") == "tamper":
    (root / "mcp" / "server.py").write_text("# tampered publisher\\n", encoding="utf-8")
elif os.environ.get("MOCK_PUBLICATION") == "transient_tamper":
    source_server = root / "mcp" / "server.py"
    original = source_server.read_bytes()
    source_server.write_text("# transient malicious publisher\\n", encoding="utf-8")
    try:
        snapshot_server = trusted_mcp_modules["mcp.server"].resolve()
        assert not snapshot_server.is_relative_to(root.resolve())
        assert snapshot_server.read_bytes() == original
        (root / "snapshot_restart_checked").write_text(
            str(snapshot_server), encoding="utf-8"
        )
    finally:
        source_server.write_bytes(original)
elif os.environ.get("MOCK_PUBLICATION") == "snapshot_restart_tamper":
    snapshot_server = trusted_mcp_modules["mcp.server"].resolve()
    original = snapshot_server.read_bytes()
    original_mode = snapshot_server.stat().st_mode
    executed_marker = root / "snapshot_restart_payload_executed"
    checked_marker = root / "snapshot_restart_loader_checked"
    malicious = (
        "from pathlib import Path\\n"
        f"Path({str(executed_marker)!r}).write_text('executed', encoding='utf-8')\\n"
    ).encode("utf-8") + original
    restart_environment = {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        **reasoning_mcp["env"],
    }
    try:
        snapshot_server.chmod(original_mode | 0o200)
        snapshot_server.write_bytes(malicious)
        snapshot_server.chmod(original_mode & ~0o222)
        rejected = subprocess.run(
            [
                reasoning_mcp["command"],
                *reasoning_mcp["args"],
                "--",
                "--generation-control-state",
                problem_id,
            ],
            cwd=reasoning_mcp["cwd"],
            env=restart_environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert rejected.returncode == 70
        assert "module SHA-256 mismatch" in rejected.stderr
        assert not executed_marker.exists()
    finally:
        snapshot_server.chmod(original_mode | 0o200)
        snapshot_server.write_bytes(original)
        snapshot_server.chmod(original_mode)
    accepted = subprocess.run(
        [
            reasoning_mcp["command"],
            *reasoning_mcp["args"],
            "--",
            "--generation-control-state",
            problem_id,
        ],
        cwd=reasoning_mcp["cwd"],
        env=restart_environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert accepted.returncode == 0, accepted.stderr
    assert accepted.stdout.strip() == "running"
    assert not executed_marker.exists()
    checked_marker.write_text(str(snapshot_server), encoding="utf-8")
""",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    return tests_dir / "run_example.sh", fake_bin


def _mock_environment(
    runner: Path,
    fake_bin: Path,
    *,
    mode: str,
    problem_file: str = "data/example.md",
    extra_environment: dict[str, str] | None = None,
) -> dict[str, str]:
    runner_tmp = runner.parent.parent / ".runner-tmp"
    runner_tmp.mkdir(exist_ok=True)
    environment = dict(os.environ)
    environment.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
            "TMPDIR": str(runner_tmp),
            "MAX_ITERATIONS": "1",
            "TIMER_INTERVAL_SECONDS": "1",
            "LOG_DIR": str(runner.parents[3] / "logs"),
            "VERIFY_HEALTH_URL": "http://127.0.0.1:1/health",
            "MOCK_PUBLICATION": mode,
            "PROBLEM_FILE": problem_file,
        }
    )
    environment.update(extra_environment or {})
    return environment


def _run_mock(
    tmp_path: Path,
    *,
    mode: str,
    problem_file: str = "data/example.md",
    extra_environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    runner, fake_bin = _make_runner_tree(tmp_path)
    environment = _mock_environment(
        runner,
        fake_bin,
        mode=mode,
        problem_file=problem_file,
        extra_environment=extra_environment,
    )
    return subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )


def _install_mock_cadence_adapter(tmp_path: Path) -> tuple[Path, Path, Path]:
    adapter_path = tmp_path / "agents" / "hotjoin_adapter.py"
    state_path = tmp_path / "cadence-state.json"
    calls_path = tmp_path / "cadence-calls.jsonl"
    adapter_source = r"""from __future__ import annotations

import hashlib
import json
import os
import pathlib
import sys
import tomllib


REVIEW = {
    "policy_id": "rethlas_route_review_90m_v1",
    "clock": "earliest_durable_wall_and_same_boot_monotonic",
    "approved_guardian_launcher_sha256": "__APPROVED_GUARDIAN_LAUNCHER_SHA256__",
    "approved_guardian_sha256": "__APPROVED_GUARDIAN_SHA256__",
    "approved_guardian_runner_sha256": "__APPROVED_GUARDIAN_RUNNER_SHA256__",
    "guardian_control_schema_sha256": "__GUARDIAN_CONTROL_SCHEMA_SHA256__",
    "guardian_launch_manifest_schema_sha256": (
        "__GUARDIAN_LAUNCH_MANIFEST_SCHEMA_SHA256__"
    ),
    "cycle_seconds": 5400,
    "review_1_due_seconds": 1800,
    "review_1_deadline_seconds": 2100,
    "review_2_due_seconds": 3600,
    "review_2_deadline_seconds": 3900,
    "close_notice_due_seconds": 5220,
    "hard_stop_due_seconds": 5400,
    "review_verdicts": ["green", "yellow", "red"],
    "two_yellow_without_progress_is_red": True,
    "review_is_independent": True,
    "review_is_not_fact_check": True,
    "hard_stop_interrupt_is_expected": True,
    "guardian_enforcement_ready": True,
    "max_concurrent_proof_lanes": 2,
}
CONTEXT = {
    "policy_id": "rethlas_context_guard_v1",
    "occupancy_numerator": "last.inputTokens",
    "occupancy_denominator": "modelContextWindow",
    "cached_input_tokens_reduce_occupancy": False,
    "observe": {"ratio_gte": 0.60, "headroom_lte": 112000},
    "checkpoint_required": {"ratio_gte": 0.65, "headroom_lte": 96000},
    "fresh_thread_required": {"ratio_gte": 0.70, "headroom_lte": 80000},
    "emergency": {"ratio_gte": 0.82, "headroom_lte": 48000},
    "compaction_forces_fresh_thread": True,
    "max_handoff_utf8_bytes": 32768,
    "fresh_thread_must_not_resume_or_fork": True,
}
def canonical(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def review_driver_commitment(driver: pathlib.Path) -> dict[str, str]:
    assert driver.name == "server_driver.py"
    mcp_root = driver.parent
    review_root = mcp_root.parent / "review"
    logical_paths = (
        "generation/mcp/__init__.py",
        "generation/mcp/advisor_client.py",
        "generation/mcp/proof_context.py",
        "generation/mcp/review_client.py",
        "generation/mcp/server.py",
        "generation/mcp/server_driver.py",
        "generation/mcp/verification_client.py",
        "review/__init__.py",
        "review/contracts.py",
        "review/critic.py",
    )
    entries = []
    driver_sha256 = ""
    for logical_path in logical_paths:
        relative = pathlib.Path(logical_path)
        source = (
            mcp_root / relative.name
            if relative.parts[:2] == ("generation", "mcp")
            else review_root / relative.name
        )
        raw = source.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        entries.append({"path": logical_path, "sha256": digest, "size": len(raw)})
        if logical_path == "generation/mcp/server_driver.py":
            driver_sha256 = digest
    manifest = {
        "schema_version": "rethlas_review_driver_package_v1",
        "files": sorted(entries, key=lambda item: item["path"]),
    }
    return {
        "driver_sha256": driver_sha256,
        "package_sha256": hashlib.sha256(canonical(manifest).encode()).hexdigest(),
    }


review_digest = hashlib.sha256(canonical(REVIEW).encode()).hexdigest()
context_digest = hashlib.sha256(canonical(CONTEXT).encode()).hexdigest()
contract_material = {
    "schema_version": "rethlas-policy-contract-v1",
    "review_cadence_policy": {**REVIEW, "policy_sha256": review_digest},
    "context_guard_policy": {**CONTEXT, "policy_sha256": context_digest},
}
contract = {
    **contract_material,
    "contract_sha256": hashlib.sha256(
        canonical(contract_material).encode()
    ).hexdigest(),
}

arguments = sys.argv[1:]
commands = {
    "policy-contract",
    "init",
    "status",
    "cadence-control-state",
    "control-capability-bind",
    "stale-recovery-capability-prepare",
    "cadence-admit",
    "cadence-close",
    "stale-turn-reconcile",
    "review-drive",
    "guarded-review-drive",
    "context-handoff-prepare",
    "review-status",
    "run-generator",
}
command = next(value for value in arguments if value in commands)

PRIVILEGED_TOKEN_ENV_NAMES = (
    "RETHLAS_REVIEW_CONTROL_TOKEN",
    "RETHLAS_GUARDIAN_CYCLE_TOKEN",
    "RETHLAS_RUNNER_CYCLE_TOKEN",
    "RETHLAS_STALE_RECOVERY_TOKEN",
)
OWNER_CONTROL_COMMANDS = {
    "control-capability-bind",
    "cadence-admit",
    "cadence-close",
    "review-drive",
}


def read_capability_fd(option: str) -> str | None:
    if option not in arguments:
        return None
    assert arguments.count(option) == 1
    descriptor_text = arguments[arguments.index(option) + 1]
    assert descriptor_text.isdecimal()
    descriptor = int(descriptor_text)
    assert descriptor >= 3
    raw = b""
    try:
        while len(raw) <= 64:
            chunk = os.read(descriptor, 65 - len(raw))
            if not chunk:
                break
            raw += chunk
    finally:
        os.close(descriptor)
    assert len(raw) == 64
    token = raw.decode("ascii")
    assert all(character in "0123456789abcdef" for character in token)
    assert token not in canonical(arguments)
    assert all(token not in value for value in os.environ.values())
    return token


control_token = read_capability_fd("--control-token-fd")
runner_token = read_capability_fd("--runner-token-fd")
control_domain = (
    arguments[arguments.index("--control-token-domain") + 1]
    if "--control-token-domain" in arguments
    else None
)
if command in OWNER_CONTROL_COMMANDS:
    assert control_token is not None
    assert control_domain == "owner"
    assert runner_token is None
    assert all(name not in os.environ for name in PRIVILEGED_TOKEN_ENV_NAMES)
control_envelope = None
if command in {
    "control-capability-bind",
    "stale-recovery-capability-prepare",
    "cadence-admit",
    "cadence-close",
    "stale-turn-reconcile",
    "review-drive",
    "context-handoff-prepare",
    "review-status",
}:
    control_envelope = json.loads(sys.stdin.read())
review_db = os.environ.get("RETHLAS_REVIEW_DB")
mock_root = (
    pathlib.Path(review_db).resolve().parents[2]
    if review_db
    else pathlib.Path(__file__).resolve().parents[1]
)
calls_path = pathlib.Path(
    os.environ.get("MOCK_CADENCE_CALLS_FILE", mock_root / "cadence-calls.jsonl")
)
with calls_path.open("a", encoding="utf-8") as handle:
    handle.write(canonical({
        "argv": arguments,
        "command": command,
        "control_capability": (
            {
                "domain": control_domain,
                "sha256": hashlib.sha256(control_token.encode("ascii")).hexdigest(),
            }
            if control_token is not None
            else None
        ),
        "control_envelope": control_envelope,
        "capability_env_present": any(
            name in os.environ for name in PRIVILEGED_TOKEN_ENV_NAMES
        ),
        "runner_capability_sha256": (
            hashlib.sha256(runner_token.encode("ascii")).hexdigest()
            if runner_token is not None
            else None
        ),
    }) + "\n")

if command == "policy-contract":
    reported_review = dict(REVIEW)
    guardian_mode = os.environ.get("MOCK_GUARDIAN_ENFORCEMENT_READY_MODE", "ready")
    if guardian_mode == "false":
        reported_review["guardian_enforcement_ready"] = False
    elif guardian_mode == "missing":
        del reported_review["guardian_enforcement_ready"]
    elif guardian_mode == "non_boolean":
        reported_review["guardian_enforcement_ready"] = "true"
    elif guardian_mode != "ready":
        raise AssertionError("unsupported guardian release-gate mock mode")
    reported_review_digest = hashlib.sha256(canonical(reported_review).encode()).hexdigest()
    reported_material = {
        "schema_version": "rethlas-policy-contract-v1",
        "review_cadence_policy": {
            **reported_review,
            "policy_sha256": reported_review_digest,
        },
        "context_guard_policy": contract_material["context_guard_policy"],
    }
    reported_contract = {
        **reported_material,
        "contract_sha256": hashlib.sha256(
            canonical(reported_material).encode()
        ).hexdigest(),
    }
    if os.environ.get("MOCK_TAMPER_GUARDIAN_POLICY_DIGEST"):
        reported_contract["review_cadence_policy"]["policy_sha256"] = "0" * 64
    print(canonical(reported_contract))
    raise SystemExit(0)

state_path = pathlib.Path(
    os.environ.get("MOCK_CADENCE_STATE_FILE", mock_root / "cadence-state.json")
)
if control_envelope is None:
    run_id = arguments[arguments.index("--run-id") + 1]
elif "run_id" in control_envelope["payload"]:
    run_id = control_envelope["payload"]["run_id"]
elif isinstance(control_envelope["payload"].get("assertions"), dict):
    run_id = control_envelope["payload"]["assertions"]["run_id"]
else:
    run_id = json.loads(state_path.read_text(encoding="utf-8"))["run_id"]
if command == "init":
    problem_id = arguments[arguments.index("--problem-id") + 1]
    if not state_path.exists():
        state_path.write_text(
            canonical({
                "disposition": "initial_start_allowed",
                "cycle_history": [],
                "cycle_id": None,
                "cycle_serial": 0,
                "codex_digests": [],
                "capability_revision": 0,
                "generation_control_instances": [],
                "generation": 0,
                "guardian_clock_sha256": None,
                "helper_digests": [],
                "helper_paths": [],
                "memory_batch_publications": {},
                "review_driver_digests": [],
                "review_driver_package_digests": [],
                "review_driver_paths": [],
                "problem_id": problem_id,
                "paid_root_count": 0,
                "run_count": 0,
                "run_id": run_id,
                "runtime_digests": [],
                "thread_epoch": None,
                "token_digests": [],
            }),
            encoding="utf-8",
        )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["run_id"] == run_id
    assert state["problem_id"] == problem_id
    print(canonical({"run_id": run_id, "problem_id": problem_id}))
    raise SystemExit(0)

state = json.loads(state_path.read_text(encoding="utf-8"))

if command == "status":
    thread_epoch = state.get("thread_epoch")
    thread_id = None
    active_turn_id = None
    if isinstance(thread_epoch, dict):
        thread_id = thread_epoch.get("thread_id")
        active_turn_id = thread_epoch.get("active_turn_id")
    print(canonical({
        "active_turn_id": active_turn_id,
        "generation": state.get("generation", state.get("run_count", 0)),
        "generator_fingerprint": None,
        "head_digest": "0" * 64,
        "last_sequence": 0,
        "message_counts": {},
        "message_source_counts": {},
        "problem_id": state["problem_id"],
        "quarantine": state.get("quarantine"),
        "run_id": run_id,
        "thread_id": thread_id,
        "turn_intent_counts": {},
    }))
    raise SystemExit(0)


def active_cycle_id() -> str:
    cycle_id = state.get("cycle_id")
    assert isinstance(cycle_id, str)
    assert cycle_id.startswith("cycle_") and len(cycle_id) == 38
    assert all(character in "0123456789abcdef" for character in cycle_id[6:])
    return cycle_id


def cadence_projection() -> dict[str, object]:
    disposition = state["disposition"]
    projected_review = dict(REVIEW)
    if os.environ.get("MOCK_GUARDIAN_ENFORCEMENT_READY_MODE") == "false":
        projected_review["guardian_enforcement_ready"] = False
    projected_review_digest = hashlib.sha256(
        canonical(projected_review).encode()
    ).hexdigest()
    adapter_resume_allowed = disposition in {
        "initial_start_allowed",
        "continue_active_cycle",
        "continue_review_only",
        "continue_next_cycle",
        "continue_reviewed_cycle_fresh_epoch",
        "resume_active_cycle",
        "terminal_observed_pending_finalization",
        "review_boundary_recovery_required",
    }
    paid_turn_allowed = disposition in {
        "initial_start_allowed",
        "continue_active_cycle",
        "continue_review_only",
        "continue_next_cycle",
        "continue_reviewed_cycle_fresh_epoch",
    }
    projected_epoch = state["thread_epoch"]
    if (
        disposition == "continue_next_cycle"
        and os.environ.get("MOCK_CORRUPT_CONTINUE_EPOCH")
    ):
        projected_epoch = {**projected_epoch, "handoff_sha256": "f" * 64}
    if (
        isinstance(projected_epoch, dict)
        and projected_epoch.get("state") == "pending"
        and os.environ.get("MOCK_CORRUPT_OWNER_YIELD_HANDOFF")
    ):
        projected_epoch = {**projected_epoch, "handoff_sha256": "e" * 64}
    review_state = "not_started" if state["run_count"] == 0 else "active"
    review_projection: dict[str, object] = {
        "continuation": (
            {
                "authorization_id": "cadauth_" + "a" * 32,
                "expires_at": 9_999_999_999.0,
                "mode": (
                    "active_cycle"
                    if disposition == "continue_active_cycle"
                    else "review_only"
                ),
                "reserved": False,
                "review_action_id": (
                    None
                    if disposition == "continue_active_cycle"
                    else "action_mock_review"
                ),
                "state": "prepared",
                "superseded": False,
            }
            if disposition in {"continue_active_cycle", "continue_review_only"}
            else None
        ),
        "review_boundary": (
            {
                "boundary_id": "reviewbound_" + "b" * 32,
                "no_live_descendants_sha256": (
                    "d" * 64
                    if disposition
                    in {"review_drive_required", "post_review_handoff_required"}
                    else None
                ),
                "review_ordinal": 1,
                "root_terminal_sha256": "c" * 64,
                "root_thread_id": "thread_mock_1",
                "root_turn_id": "turn_mock_1",
                "state": (
                    "descendants_terminal"
                    if disposition
                    in {"review_drive_required", "post_review_handoff_required"}
                    else "root_terminal"
                ),
            }
            if disposition
            in {
                "review_drive_required",
                "post_review_handoff_required",
                "review_boundary_recovery_required",
            }
            else None
        ),
        "policy_digest": projected_review_digest,
        "policy_id": REVIEW["policy_id"],
        "state": review_state,
    }
    if state.get("cycle_id") is not None:
        review_projection["cycle_id"] = active_cycle_id()
        review_projection["generation"] = int(state["generation"])
        review_projection["guardian_clock_sha256"] = state[
            "guardian_clock_sha256"
        ]
        review_projection["allowed_action"] = state.get(
            "allowed_action",
            os.environ.get("MOCK_CADENCE_ALLOWED_ACTION", "free_construction"),
        )
    return {
        "context_guard": {
            "adapter_resume_allowed": adapter_resume_allowed,
            "emergency_marker": None,
            "operational_failures": [],
            "pending_terminal": None,
            "policy_digest": context_digest,
            "policy_id": CONTEXT["policy_id"],
            "state": "not_started" if state["run_count"] == 0 else "active",
        },
        "disposition": disposition,
        "paid_turn_allowed": paid_turn_allowed,
        "quarantine": state.get("quarantine"),
        "review_cadence": review_projection,
        "run_id": run_id,
        "thread_epoch": projected_epoch,
    }


if command == "cadence-control-state":
    if os.environ.get("MOCK_ABSOLUTE_DEADLINE_EXPIRED"):
        state["disposition"] = "hard_stopped_unfinalized"
        state_path.write_text(canonical(state), encoding="utf-8")
    if os.environ.get("MOCK_MUTATE_REVIEW_HELPER_DURING_PREFLIGHT"):
        helper_source = pathlib.Path(os.environ["MOCK_REVIEW_HELPER_SOURCE"])
        if not os.environ.get("MOCK_REVIEW_HELPER_MUTATED_MARKER"):
            with helper_source.open("a", encoding="utf-8") as handle:
                handle.write("\n# mutated before reviewer/root spawn\n")
    if os.environ.get("MOCK_MUTATE_REVIEW_DRIVER_PACKAGE_DURING_PREFLIGHT"):
        driver_source = pathlib.Path(os.environ["MOCK_REVIEW_DRIVER_PACKAGE_SOURCE"])
        if not state.get("review_driver_package_mutated"):
            with driver_source.open("a", encoding="utf-8") as handle:
                handle.write("\n# mutated driver dependency before reviewer/root spawn\n")
            state["review_driver_package_mutated"] = True
            state_path.write_text(canonical(state), encoding="utf-8")
    if os.environ.get("MOCK_MUTATE_RECURSIVE_SKILL_DURING_PREFLIGHT"):
        skill_source = pathlib.Path(os.environ["MOCK_RECURSIVE_SKILL_SOURCE"])
        if not state.get("recursive_skill_mutated"):
            with skill_source.open("a", encoding="utf-8") as handle:
                handle.write("\n# mutated cost policy before reviewer/root spawn\n")
            state["recursive_skill_mutated"] = True
            state_path.write_text(canonical(state), encoding="utf-8")
    if os.environ.get("MOCK_MUTATE_CODEX_DURING_PREFLIGHT"):
        codex_source = pathlib.Path(os.environ["MOCK_CODEX_SOURCE"])
        if not state.get("codex_mutated"):
            with codex_source.open("a", encoding="utf-8") as handle:
                handle.write("\n# mutated before root/reviewer spawn\n")
            state["codex_mutated"] = True
            state_path.write_text(canonical(state), encoding="utf-8")
    print(canonical(cadence_projection()))
    raise SystemExit(0)

if command == "stale-recovery-capability-prepare":
    assert control_envelope is not None
    assert control_envelope["schema_version"] == "rethlas_review_adapter_command_v1"
    assert control_envelope["command"] == "stale_recovery_capability_prepare"
    payload = control_envelope["payload"]
    assert set(payload) == {
        "operation",
        "run_id",
        "expected_thread_id",
        "expected_turn_id",
        "source_database_path",
        "source_database_sha256",
        "source_preimage_manifest_sha256",
        "copy_database_device",
        "copy_database_inode",
        "copy_database_preimage_sha256",
        "owner_uid",
        "database_mode_octal",
        "codex_bin",
        "codex_bin_sha256",
    }
    assert payload["operation"] == "stale_recovery_capability_prepare"
    assert payload["run_id"] == run_id
    assert payload["expected_thread_id"] == state["thread_epoch"]["thread_id"]
    assert payload["expected_turn_id"] == state["thread_epoch"]["active_turn_id"]
    source_path = pathlib.Path(payload["source_database_path"])
    copy_path = pathlib.Path(arguments[arguments.index("--db") + 1])
    assert source_path.is_absolute() and copy_path.is_absolute()
    assert source_path.stat().st_ino != copy_path.stat().st_ino
    assert hashlib.sha256(source_path.read_bytes()).hexdigest() == payload[
        "source_database_sha256"
    ]
    assert len(payload["source_preimage_manifest_sha256"]) == 64
    assert all(
        character in "0123456789abcdef"
        for character in payload["source_preimage_manifest_sha256"]
    )
    assert hashlib.sha256(copy_path.read_bytes()).hexdigest() == payload[
        "copy_database_preimage_sha256"
    ]
    assert copy_path.stat().st_dev == payload["copy_database_device"]
    assert copy_path.stat().st_ino == payload["copy_database_inode"]
    assert payload["owner_uid"] == os.getuid()
    assert payload["database_mode_octal"] == "0600"
    codex_path = pathlib.Path(payload["codex_bin"])
    assert codex_path.is_absolute() and codex_path.is_file()
    assert hashlib.sha256(codex_path.read_bytes()).hexdigest() == payload[
        "codex_bin_sha256"
    ]
    token = os.environ.get("RETHLAS_STALE_RECOVERY_TOKEN", "")
    assert len(token) == 64 and all(character in "0123456789abcdef" for character in token)
    for forbidden in (
        "RETHLAS_REVIEW_CONTROL_TOKEN",
        "RETHLAS_GUARDIAN_CYCLE_TOKEN",
        "RETHLAS_RUNNER_CYCLE_TOKEN",
    ):
        assert not os.environ.get(forbidden)
    state["stale_recovery_token_sha256"] = hashlib.sha256(token.encode()).hexdigest()
    state_path.write_text(canonical(state), encoding="utf-8")
    seed = {
        "schema_version": "rethlas_stale_recovery_capability_v1",
        "operation": "stale_recovery_capability_prepare",
        "capability_id": "stalecap_" + "5" * 32,
        "run_id": run_id,
        "state": "active",
        "scope": "stale_turn_reconcile",
        "expected_thread_id": payload["expected_thread_id"],
        "expected_turn_id": payload["expected_turn_id"],
        "source_database_sha256": payload["source_database_sha256"],
        "source_preimage_manifest_sha256": payload[
            "source_preimage_manifest_sha256"
        ],
        "source_sidecars": {
            "wal_size": pathlib.Path(str(source_path) + "-wal").stat().st_size
            if pathlib.Path(str(source_path) + "-wal").exists()
            else 0,
            "shm_size": pathlib.Path(str(source_path) + "-shm").stat().st_size
            if pathlib.Path(str(source_path) + "-shm").exists()
            else 0,
        },
        "backup_manifest_sha256": "6" * 64,
        "copy_database_device": payload["copy_database_device"],
        "copy_database_inode": payload["copy_database_inode"],
        "copy_database_preimage_sha256": payload["copy_database_preimage_sha256"],
        "codex_bin": payload["codex_bin"],
        "codex_bin_sha256": payload["codex_bin_sha256"],
        "created_sequence": 10,
    }
    seed["receipt_sha256"] = hashlib.sha256(canonical(seed).encode()).hexdigest()
    if os.environ.get("MOCK_TAMPER_STALE_PREPARE_RECEIPT"):
        seed["receipt_sha256"] = "0" * 64
    print(canonical(seed))
    raise SystemExit(0)

if command == "stale-turn-reconcile":
    assert control_envelope is not None
    assert control_envelope["schema_version"] == "rethlas_review_adapter_command_v1"
    assert control_envelope["command"] == "stale_turn_reconcile"
    payload = control_envelope["payload"]
    assert set(payload) == {
        "operation",
        "run_id",
        "expected_thread_id",
        "expected_turn_id",
    }
    assert payload["operation"] == "stale_turn_reconcile"
    assert payload["run_id"] == run_id
    assert payload["expected_thread_id"] == state["thread_epoch"]["thread_id"]
    assert payload["expected_turn_id"] == state["thread_epoch"]["active_turn_id"]
    token = os.environ.get("RETHLAS_STALE_RECOVERY_TOKEN", "")
    assert hashlib.sha256(token.encode()).hexdigest() == state[
        "stale_recovery_token_sha256"
    ]
    for forbidden in (
        "RETHLAS_REVIEW_CONTROL_TOKEN",
        "RETHLAS_GUARDIAN_CYCLE_TOKEN",
        "RETHLAS_RUNNER_CYCLE_TOKEN",
    ):
        assert not os.environ.get(forbidden)
    state["thread_epoch"]["active_turn_id"] = None
    state["disposition"] = "operational_blocked"
    state["quarantine"] = {
        "kind": "adapter_loss_terminal_discontinuity",
        "reason": "mock terminal discontinuity",
        "thread_id": payload["expected_thread_id"],
        "turn_id": payload["expected_turn_id"],
    }
    state_path.write_text(canonical(state), encoding="utf-8")
    result = {
        "schema_version": "rethlas_stale_turn_reconcile_result_v1",
        "operation": "stale_turn_reconcile",
        "run_id": run_id,
        "thread_id": payload["expected_thread_id"],
        "turn_id": payload["expected_turn_id"],
        "state": "terminal_reconciled_quarantined",
        "observed_status": "interrupted",
        "thread_read_response_sha256": "1" * 64,
        "turn_sha256": "2" * 64,
        "terminal_sha256": "3" * 64,
        "settled_message_count": 4,
        "settled_messages_sha256": "4" * 64,
        "committed_sequence": 11,
    }
    result["receipt_sha256"] = hashlib.sha256(canonical(result).encode()).hexdigest()
    print(canonical(result))
    raise SystemExit(0)

if command == "control-capability-bind":
    assert control_envelope is not None
    assert control_envelope["schema_version"] == "rethlas_review_adapter_command_v1"
    assert control_envelope["command"] == "control_capability_bind"
    payload = control_envelope["payload"]
    assert set(payload) == {
        "run_id",
        "contract_cli_path",
        "contract_cli_sha256",
        "trusted_runtime_sha256",
        "review_driver_path",
        "review_driver_sha256",
        "review_driver_package_sha256",
        "expected_model",
        "reasoning_effort",
        "review_policy_sha256",
        "codex_bin",
        "codex_bin_sha256",
        "generation_control_instance_id",
        "expected_statement_sha256",
    }
    helper_path = pathlib.Path(payload["contract_cli_path"])
    driver_path = pathlib.Path(payload["review_driver_path"])
    codex_path = pathlib.Path(payload["codex_bin"])
    assert helper_path.is_absolute() and helper_path.is_file()
    assert hashlib.sha256(helper_path.read_bytes()).hexdigest() == payload[
        "contract_cli_sha256"
    ]
    assert driver_path.is_absolute() and driver_path.is_file()
    driver_commitment = review_driver_commitment(driver_path)
    assert payload["review_driver_sha256"] == driver_commitment["driver_sha256"]
    assert payload["review_driver_package_sha256"] == driver_commitment[
        "package_sha256"
    ]
    assert codex_path.is_absolute() and codex_path.is_file() and not codex_path.is_symlink()
    assert hashlib.sha256(codex_path.read_bytes()).hexdigest() == payload[
        "codex_bin_sha256"
    ]
    assert payload["expected_model"] == "gpt-5.6-sol"
    assert payload["reasoning_effort"] == "max"
    assert payload["review_policy_sha256"] == review_digest
    assert payload["expected_statement_sha256"] == os.environ[
        "RETHLAS_EXPECTED_STATEMENT_SHA256"
    ]
    assert len(payload["generation_control_instance_id"]) == 32
    if state["disposition"] == "owner_yield_close_required" and state[
        "generation_control_instances"
    ]:
        # A restart may rotate the master capability/path, but it must keep the
        # exact prior generation instance until cadence-close consumes the
        # already-written wait receipt.
        assert payload["generation_control_instance_id"] == state[
            "generation_control_instances"
        ][-1]
    assert control_token is not None
    token_sha256 = hashlib.sha256(control_token.encode("ascii")).hexdigest()
    binding_state = "bound" if not state["token_digests"] else "rotated"
    state["capability_revision"] = int(state.get("capability_revision", 0)) + 1
    state["token_digests"].append(token_sha256)
    state["helper_paths"].append(str(helper_path))
    state["helper_digests"].append(payload["contract_cli_sha256"])
    state["runtime_digests"].append(payload["trusted_runtime_sha256"])
    state["review_driver_paths"].append(str(driver_path))
    state["review_driver_digests"].append(payload["review_driver_sha256"])
    state["review_driver_package_digests"].append(
        payload["review_driver_package_sha256"]
    )
    state["codex_digests"].append(payload["codex_bin_sha256"])
    state["generation_control_instances"].append(
        payload["generation_control_instance_id"]
    )
    state_path.write_text(canonical(state), encoding="utf-8")
    print(canonical({
        "schema_version": "rethlas_control_capability_binding_v1",
        "run_id": run_id,
        "state": binding_state,
        "capability_revision": state["capability_revision"],
        "token_sha256": token_sha256,
        "contract_cli_sha256": payload["contract_cli_sha256"],
        "trusted_runtime_sha256": payload["trusted_runtime_sha256"],
        "review_driver_sha256": payload["review_driver_sha256"],
        "review_driver_package_sha256": payload[
            "review_driver_package_sha256"
        ],
        "generation_control_instance_id": payload["generation_control_instance_id"],
    }))
    raise SystemExit(0)

if command == "cadence-admit":
    assert control_envelope is not None
    assert control_envelope["schema_version"] == "rethlas_review_adapter_command_v1"
    assert control_envelope["command"] == "cadence_admit"
    payload = control_envelope["payload"]
    assert set(payload) == {"operation", "run_id", "generation_control_receipt"}
    assert control_token is not None
    assert hashlib.sha256(control_token.encode("ascii")).hexdigest() == state[
        "token_digests"
    ][-1]
    receipt = payload["generation_control_receipt"]
    assert receipt["control"]["state"] == "running"
    assert receipt["control"]["reason"] == "owner_runner_started"
    assert receipt["control"]["instance_id"] == state[
        "generation_control_instances"
    ][-1]
    operation = payload["operation"]
    if operation == "continue_active_cycle":
        assert state["disposition"] == "continuation_authorization_required"
        state["disposition"] = "continue_active_cycle"
    elif operation == "continue_review_only":
        assert state["disposition"] == "review_turn_authorization_required"
        state["disposition"] = "continue_review_only"
    elif operation == "owner_resume":
        assert state["disposition"] in {"owner_wait_cost", "owner_wait_advisor"}
        assert state["thread_epoch"]["state"] == "pending"
        if os.environ.get("MOCK_FAIL_BEFORE_OWNER_RESUME_CAS"):
            raise SystemExit(75)
        state["disposition"] = "continue_next_cycle"
    else:
        raise AssertionError(f"unsupported mock cadence admission {operation}")
    state_path.write_text(canonical(state), encoding="utf-8")
    if (
        operation == "continue_active_cycle"
        and os.environ.get("MOCK_FAIL_WRAPPER_AFTER_ACTIVE_ADMIT")
    ):
        raise SystemExit(75)
    print(canonical(cadence_projection()))
    raise SystemExit(0)

if command == "cadence-close":
    assert control_envelope is not None
    assert control_envelope["schema_version"] == "rethlas_review_adapter_command_v1"
    assert control_envelope["command"] == "cadence_close"
    payload = control_envelope["payload"]
    assert set(payload) == {
        "operation",
        "run_id",
        "cycle_id",
        "handoff_id",
        "content_sha256",
        "to_thread_epoch",
        "generation_control_receipt",
    }
    assert payload["operation"] == "owner_yield"
    assert state["disposition"] == "owner_yield_close_required"
    assert control_token is not None
    assert hashlib.sha256(control_token.encode("ascii")).hexdigest() == state[
        "token_digests"
    ][-1]
    epoch = state["thread_epoch"]
    assert payload["cycle_id"] == active_cycle_id()
    assert payload["handoff_id"] == epoch["handoff_id"]
    assert payload["content_sha256"] == epoch["handoff_sha256"]
    assert payload["to_thread_epoch"] == epoch["thread_epoch"]
    wait_state = payload["generation_control_receipt"]["control"]["state"]
    assert payload["generation_control_receipt"]["control"]["instance_id"] == state[
        "generation_control_instances"
    ][-1]
    state["disposition"] = {
        "waiting_cost_gate": "owner_wait_cost",
        "waiting_owner_advisor_decision": "owner_wait_advisor",
    }[wait_state]
    state_path.write_text(canonical(state), encoding="utf-8")
    print(canonical(cadence_projection()))
    raise SystemExit(0)

if command in {"review-drive", "guarded-review-drive"}:
    if command == "guarded-review-drive":
        assert control_envelope is None
        assert runner_token is not None
        assert control_token is None
        assert all(name not in os.environ for name in PRIVILEGED_TOKEN_ENV_NAMES)
        owner_token_sha256 = state["token_digests"][-1]
        assert hashlib.sha256(runner_token.encode("ascii")).hexdigest() != (
            owner_token_sha256
        )
        assert all(
            hashlib.sha256(argument.encode("utf-8")).hexdigest()
            != owner_token_sha256
            for argument in arguments
        )
        assert all(
            hashlib.sha256(value.encode("utf-8")).hexdigest()
            != owner_token_sha256
            for value in os.environ.values()
        )
        payload = {
            "operation": "drive_due_review",
            "run_id": arguments[arguments.index("--run-id") + 1],
            "boundary_id": arguments[arguments.index("--boundary-id") + 1],
        }
    else:
        assert control_envelope is not None
        assert control_envelope[
            "schema_version"
        ] == "rethlas_review_adapter_command_v1"
        assert control_envelope["command"] == "review_drive"
        payload = control_envelope["payload"]
        assert control_token is not None
        assert hashlib.sha256(control_token.encode("ascii")).hexdigest() == state[
            "token_digests"
        ][-1]
    assert set(payload) == {"operation", "run_id", "boundary_id"}
    assert payload["operation"] == "drive_due_review"
    assert payload["run_id"] == run_id
    assert state["disposition"] == "review_drive_required"
    assert payload["boundary_id"] == "reviewbound_" + "b" * 32
    review_id = "review_" + "e" * 32
    request_sha256 = "1" * 64
    snapshot_sha256 = "2" * 64
    disposition = {
        "schema_version": "rethlas_review_disposition_v1",
        "review_id": review_id,
        "request_sha256": request_sha256,
        "snapshot_sha256": snapshot_sha256,
        "decision": {
            "effective_verdict": "green",
            "route_id": "route_mock_active",
            "critic_confirmed_progress_ids": [],
        },
        "active_route": {
            "route_id": "route_mock_active",
            "core_bridge": "Mock host-reviewed bridge.",
            "obligations": ["Complete the next exact milestone."],
        },
        "frozen_route_id": None,
        "route_transition_publication_receipt": {
            "schema_version": "rethlas_route_transition_receipt_v1"
        },
        "next_milestone": {
            "description": "Complete the next exact milestone.",
            "test": "The milestone is persisted and independently reviewable.",
        },
        "evidence_record_ids": [],
        "requires_targeted_verification": False,
    }
    if os.environ.get("MOCK_REVIEW_DRIVE_RED"):
        disposition["decision"] = {
            "effective_verdict": "red",
            "route_id": "route_mock_active",
            "critic_confirmed_progress_ids": [],
        }
        disposition["active_route"] = None
        disposition["frozen_route_id"] = "route_mock_active"
        disposition["next_milestone"] = None
    disposition_sha256 = hashlib.sha256(canonical(disposition).encode()).hexdigest()
    if os.environ.get("MOCK_REVIEW_DRIVE_RED"):
        state["allowed_action"] = "freeze_route"
        state["disposition"] = "route_frozen"
    else:
        prior_epoch = state["thread_epoch"]
        assert isinstance(prior_epoch, dict) and prior_epoch["state"] == "active"
        handoff_sha256 = hashlib.sha256(
            f"review-handoff-{payload['boundary_id']}".encode()
        ).hexdigest()
        state["allowed_action"] = "post_review_handoff_required"
        state["disposition"] = "continue_reviewed_cycle_fresh_epoch"
        state["thread_epoch"] = {
            "active_turn_id": None,
            "handoff_id": f"handoff_{handoff_sha256}",
            "handoff_sha256": handoff_sha256,
            "predecessor_epoch": prior_epoch["thread_epoch"],
            "state": "pending",
            "thread_epoch": prior_epoch["thread_epoch"] + 1,
            "thread_id": None,
        }
    state["review_drive_count"] = int(state.get("review_drive_count", 0)) + 1
    state_path.write_text(canonical(state), encoding="utf-8")
    projection = cadence_projection()
    print(canonical({
        "schema_version": "rethlas_review_drive_result_v1",
        "run_id": run_id,
        "boundary_id": payload["boundary_id"],
        "cycle_id": active_cycle_id(),
        "review_id": review_id,
        "state": "disposition_ready",
        "disposition_sha256": disposition_sha256,
        "disposition": disposition,
        "review_cadence": projection["review_cadence"],
        "thread_epoch": projection["thread_epoch"],
    }))
    raise SystemExit(0)

if command == "context-handoff-prepare":
    assert control_envelope is not None
    assert control_envelope["schema_version"] == "rethlas_review_adapter_command_v1"
    assert control_envelope["command"] == "context_handoff_prepare"
    payload = control_envelope["payload"]
    assert set(payload) == {"operation", "purpose", "proposal", "assertions"}
    assert payload["operation"] == "context_handoff_prepare"
    assert payload["purpose"] == "owner_yield"
    proposal = payload["proposal"]
    assertions = payload["assertions"]
    assert set(proposal) == {
        "active_route", "new_record_ids", "obligations", "next_action"
    }
    assert set(assertions) == {
        "run_id", "problem_id", "statement_sha256", "blueprint_sha256",
        "last_review", "yellow_streak", "route_frozen",
    }
    content = {
        "schema_version": "rethlas_context_handoff_v2",
        "purpose": "owner_yield",
        "run_id": assertions["run_id"],
        "problem_id": assertions["problem_id"],
        "from_thread_epoch": "1",
        "statement_sha256": assertions["statement_sha256"],
        "blueprint_sha256": assertions["blueprint_sha256"],
        "cadence": {
            "phase": "work_0_30",
            "cycle_started_at_utc": "2026-08-11T00:00:00+00:00",
            "minute30_at_utc": "2026-08-11T00:30:00+00:00",
            "minute60_at_utc": "2026-08-11T01:00:00+00:00",
            "close_at_utc": "2026-08-11T01:27:00+00:00",
            "hard_stop_at_utc": "2026-08-11T01:30:00+00:00",
        },
        "active_route": proposal["active_route"],
        "last_review": assertions["last_review"],
        "new_record_ids": proposal["new_record_ids"],
        "yellow_streak": assertions["yellow_streak"],
        "route_frozen": assertions["route_frozen"],
        "pending": {
            "verification_ticket_id": None,
            "advisor_checkpoint_id": (
                state.get("owner_yield_advisor_record_id")
                if state.get("pending_yield_state")
                == "waiting_owner_advisor_decision"
                else None
            ),
        },
        "obligations": proposal["obligations"],
        "next_action": proposal["next_action"],
    }
    content_sha256 = hashlib.sha256(canonical(content).encode()).hexdigest()
    handoff_id = f"handoff_{content_sha256}"
    state["owner_yield_handoff"] = {
        "handoff_id": handoff_id,
        "content_sha256": content_sha256,
        "to_thread_epoch": 2,
        "root_thread_id": "thread_mock_1",
        "root_turn_id": "turn_mock_1",
    }
    state_path.write_text(canonical(state), encoding="utf-8")
    print(canonical({
        "schema_version": "rethlas_review_adapter_response_v1",
        "operation": "context_handoff_prepare",
        "handoff_id": handoff_id,
        "content_sha256": content_sha256,
        "state": "prepared",
        "idempotent": False,
        "content": content,
        "binding": None,
    }))
    raise SystemExit(0)

if command == "review-status":
    assert control_envelope is not None
    assert control_envelope["schema_version"] == "rethlas_review_adapter_command_v1"
    assert control_envelope["command"] == "review_status"
    payload = control_envelope["payload"]
    operation = payload.get("operation")
    if operation == "memory_batch_publication_commit":
        assert set(payload) == {
            "operation", "problem_id", "batch_id", "checkpoint_sha256",
            "commit_sha256", "publication_class",
        }
        assert payload["problem_id"] == state["problem_id"]
        assert payload["batch_id"].startswith("batch_")
        assert len(payload["batch_id"]) == 70
        assert all(
            character in "0123456789abcdef"
            for character in payload["batch_id"][6:]
        )
        for digest_name in ("checkpoint_sha256", "commit_sha256"):
            assert len(payload[digest_name]) == 64
            assert all(
                character in "0123456789abcdef"
                for character in payload[digest_name]
            )
        assert payload["publication_class"] in {
            "reasoning_checkpoint", "control_only"
        }
        publications = state.setdefault("memory_batch_publications", {})
        existing = publications.get(payload["batch_id"])
        request_bindings = {
            key: payload[key]
            for key in (
                "problem_id", "batch_id", "checkpoint_sha256",
                "commit_sha256", "publication_class",
            )
        }
        if existing is not None:
            assert all(
                existing[key] == value
                for key, value in request_bindings.items()
            )
            receipt = existing
        else:
            receipt_seed = {
                "schema_version":
                    "rethlas_memory_batch_publication_receipt_v1",
                "state": "accepted",
                "run_id": run_id,
                **request_bindings,
                "cycle_id": active_cycle_id(),
                "cutoff_action_id": "cadact_" + "a" * 32,
                "cutoff_kind": "review_1",
                "cutoff_at_utc": "2030-01-01T00:30:00+00:00",
                "cutoff_monotonic": 2.0e18,
                "accepted_at_utc": "2030-01-01T00:00:00+00:00",
                "accepted_at_monotonic": 1.0,
                "boot_identity": "mock-cadence-boot",
            }
            receipt = {
                **receipt_seed,
                "receipt_sha256": hashlib.sha256(
                    canonical(receipt_seed).encode("utf-8")
                ).hexdigest(),
            }
            # The fake adapter is invoked concurrently by three independent
            # MCP processes.  Merge under a sidecar lock so a later process
            # cannot overwrite a receipt committed by an earlier snapshot.
            lock_path = state_path.with_suffix(state_path.suffix + ".lock")
            lock_path.touch(exist_ok=True)
            with lock_path.open("r+") as lock_handle:
                import fcntl

                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
                latest = json.loads(state_path.read_text(encoding="utf-8"))
                latest_publications = latest.setdefault(
                    "memory_batch_publications", {}
                )
                winner = latest_publications.get(payload["batch_id"])
                if winner is not None:
                    assert all(
                        winner[key] == value
                        for key, value in request_bindings.items()
                    )
                    receipt = winner
                else:
                    latest_publications[payload["batch_id"]] = receipt
                    state_path.write_text(canonical(latest), encoding="utf-8")
        print(canonical(receipt))
        raise SystemExit(0)
    if operation == "memory_batch_publication_status":
        assert set(payload) == {"operation", "problem_id"}
        assert payload["problem_id"] == state["problem_id"]
        latest = json.loads(state_path.read_text(encoding="utf-8"))
        publications = latest.setdefault("memory_batch_publications", {})
        receipts = [
            publications[batch_id]
            for batch_id in sorted(publications)
            if publications[batch_id]["state"] == "accepted"
        ]
        print(canonical({
            "schema_version": "rethlas_memory_batch_publication_status_v1",
            "run_id": run_id,
            "problem_id": payload["problem_id"],
            "receipts": receipts,
        }))
        raise SystemExit(0)
    assert set(payload) == {
        "operation", "state", "reason_sha256", "evidence_record_ids"
    }
    assert payload["operation"] == "generation_yield_prepare"
    handoff = state["owner_yield_handoff"]
    print(canonical({
        "schema_version": "rethlas_generation_yield_admission_v1",
        "operation": "generation_yield_prepare",
        "admission_id": "yieldadmit_mock_1",
        "run_id": run_id,
        "cycle_id": active_cycle_id(),
        "handoff_id": handoff["handoff_id"],
        "content_sha256": handoff["content_sha256"],
        "to_thread_epoch": handoff["to_thread_epoch"],
        "root_thread_id": handoff["root_thread_id"],
        "root_turn_id": handoff["root_turn_id"],
        "state": payload["state"],
        "reason_sha256": payload["reason_sha256"],
        "evidence_record_ids": payload["evidence_record_ids"],
    }))
    raise SystemExit(0)

starting_disposition = state["disposition"]
if starting_disposition in {"initial_start_allowed", "continue_next_cycle"}:
    prior_cycle_id = state.get("cycle_id")
    state["cycle_serial"] = int(state.get("cycle_serial", 0)) + 1
    state["generation"] = int(state.get("generation", 0)) + 1
    state["cycle_id"] = f"cycle_{state['cycle_serial']:032x}"
    state["guardian_clock_sha256"] = hashlib.sha256(
        f"guardian-clock-{state['cycle_serial']}".encode("ascii")
    ).hexdigest()
    assert state["cycle_id"] != prior_cycle_id
    state.setdefault("cycle_history", []).append(state["cycle_id"])
    state_path.write_text(canonical(state), encoding="utf-8")
if starting_disposition == "continue_reviewed_cycle_fresh_epoch":
    reviewed_epoch = state["thread_epoch"]
    assert reviewed_epoch["state"] == "pending"
    assert reviewed_epoch["thread_id"] is None
    prompt = arguments[arguments.index("--prompt") + 1]
    assert prompt.startswith("[TRUSTED HOST REHYDRATION REQUIRED]")
    state["reviewed_handoff_consumed_count"] = int(
        state.get("reviewed_handoff_consumed_count", 0)
    ) + 1
    state_path.write_text(canonical(state), encoding="utf-8")
if starting_disposition in {
    "initial_start_allowed",
    "continue_active_cycle",
    "continue_next_cycle",
    "continue_reviewed_cycle_fresh_epoch",
}:
    state["paid_root_count"] = int(state.get("paid_root_count", 0)) + 1
    state_path.write_text(canonical(state), encoding="utf-8")
if (
    starting_disposition == "continue_reviewed_cycle_fresh_epoch"
    and os.environ.get("MOCK_FAIL_AFTER_REVIEWED_EPOCH_DISPATCH")
):
    state["run_count"] = int(state["run_count"]) + 1
    state["disposition"] = "terminal_observed_pending_finalization"
    state_path.write_text(canonical(state), encoding="utf-8")
    raise SystemExit(75)

assert arguments[arguments.index("--review-cadence-policy") + 1] == REVIEW["policy_id"]
assert arguments[arguments.index("--context-guard-policy") + 1] == CONTEXT["policy_id"]
assert arguments[arguments.index("--policy-contract-sha256") + 1] == contract["contract_sha256"]
assert runner_token is not None
assert control_token is None
assert all(name not in os.environ for name in PRIVILEGED_TOKEN_ENV_NAMES)
owner_token_sha256 = state["token_digests"][-1]
assert all(
    hashlib.sha256(argument.encode("utf-8")).hexdigest() != owner_token_sha256
    for argument in arguments
)
assert all(
    hashlib.sha256(value.encode("utf-8")).hexdigest() != owner_token_sha256
    for value in os.environ.values()
)
helper_path = pathlib.Path(
    arguments[arguments.index("--review-contract-cli-path") + 1]
)
helper_sha256 = arguments[arguments.index("--review-contract-cli-sha256") + 1]
driver_path = pathlib.Path(arguments[arguments.index("--review-driver-path") + 1])
driver_sha256 = arguments[arguments.index("--review-driver-sha256") + 1]
driver_package_sha256 = arguments[
    arguments.index("--review-driver-package-sha256") + 1
]
runtime_sha256 = arguments[arguments.index("--trusted-runtime-sha256") + 1]
codex_path = pathlib.Path(arguments[arguments.index("--codex-bin") + 1])
codex_sha256 = arguments[arguments.index("--codex-bin-sha256") + 1]
assert helper_path.is_absolute() and helper_path.is_file() and not helper_path.is_symlink()
assert hashlib.sha256(helper_path.read_bytes()).hexdigest() == helper_sha256
assert driver_path.is_absolute() and driver_path.is_file() and not driver_path.is_symlink()
driver_commitment = review_driver_commitment(driver_path)
assert driver_sha256 == driver_commitment["driver_sha256"]
assert driver_package_sha256 == driver_commitment["package_sha256"]
assert codex_path.is_absolute() and codex_path.is_file() and not codex_path.is_symlink()
assert hashlib.sha256(codex_path.read_bytes()).hexdigest() == codex_sha256
assert hashlib.sha256(runner_token.encode("ascii")).hexdigest() != owner_token_sha256
mcp = tomllib.loads(
    "value=" + arguments[arguments.index("--mcp-config-toml") + 1]
)["value"]
assert set(mcp) == {
    "command",
    "args",
    "cwd",
    "env",
    "required",
    "tool_timeout_sec",
    "default_tools_approval_mode",
}
assert mcp["tool_timeout_sec"] == 3600
assert mcp["required"] is True
assert mcp["default_tools_approval_mode"] == "approve"
for key, expected in (
    ("RETHLAS_REVIEW_CADENCE_POLICY", REVIEW["policy_id"]),
    ("RETHLAS_CONTEXT_GUARD_POLICY", CONTEXT["policy_id"]),
    ("RETHLAS_POLICY_CONTRACT_SHA256", contract["contract_sha256"]),
    ("RETHLAS_REVIEW_CONTRACT_CLI_PATH", str(helper_path)),
    ("RETHLAS_REVIEW_CONTRACT_CLI_SHA256", helper_sha256),
    ("RETHLAS_TRUSTED_RUNTIME_SHA256", runtime_sha256),
    ("RETHLAS_REVIEW_ADAPTER_PATH", str(pathlib.Path(__file__).resolve())),
    (
        "RETHLAS_REVIEW_ADAPTER_SHA256",
        hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest(),
    ),
    ("RETHLAS_REVIEW_DB", os.environ["MOCK_CADENCE_EXPECTED_DB"]),
    ("RETHLAS_REVIEW_EXPECTED_MODEL", "gpt-5.6-sol"),
    ("RETHLAS_REVIEW_EXPECTED_REASONING_EFFORT", "max"),
    ("RETHLAS_REVIEW_POLICY_SHA256", review_digest),
):
    assert mcp["env"][key] == expected
assert "RETHLAS_REVIEW_CONTROL_TOKEN" not in mcp["env"]
scoped_epoch_token = hashlib.sha256(
    f"mock-scoped-epoch:{runner_token}:{state['generation']}".encode("utf-8")
).hexdigest()
assert scoped_epoch_token != runner_token
assert hashlib.sha256(scoped_epoch_token.encode("ascii")).hexdigest() != (
    owner_token_sha256
)
mcp["env"]["RETHLAS_REVIEW_CONTROL_TOKEN"] = scoped_epoch_token
# The real adapter injects this derived epoch capability into the reasoning MCP
# process.  This mock executes the trusted server in-process, so mirror only
# that derived capability here; neither the owner nor runner master token is
# placed in the environment.
os.environ["RETHLAS_REVIEW_CONTROL_TOKEN"] = scoped_epoch_token
mcp_loader_arguments = mcp["args"]
assert mcp_loader_arguments[:3] == ["-I", "-B", "-c"]
mcp_module_arguments = mcp_loader_arguments[4:]
assert len(mcp_module_arguments) == 21
mcp_module_paths = {
    mcp_module_arguments[index]: pathlib.Path(mcp_module_arguments[index + 1])
    for index in range(0, len(mcp_module_arguments), 3)
}

if state["disposition"] == "review_boundary_recovery_required":
    recovery_prompt = arguments[arguments.index("--prompt") + 1]
    assert "Recover only the already-authorized durable scheduler operation" in (
        recovery_prompt
    )
    assert "Do not start a new paid turn" in recovery_prompt
    state["disposition"] = "review_drive_required"
    state_path.write_text(canonical(state), encoding="utf-8")
    print(canonical({"run_id": run_id, "disposition": "review_drive_required"}))
    raise SystemExit(0)

# Simulate the runtime's final pre-turn CAS after wall time crosses a prepared
# authorization boundary. The adapter invocation is recorded, but no root or
# reviewer process (run_count) is started under the stale authorization.
if (
    state["disposition"] == "continue_active_cycle"
    and os.environ.get("MOCK_ACTIVE_AUTH_EXPIRED_AT_REVIEW_DUE")
):
    state["disposition"] = "review_turn_authorization_required"
    state_path.write_text(canonical(state), encoding="utf-8")
    raise SystemExit(70)
if (
    state["disposition"] == "continue_review_only"
    and os.environ.get("MOCK_REVIEW_AUTH_EXPIRED_AT_DEADLINE")
):
    state["disposition"] = "operational_blocked"
    state_path.write_text(canonical(state), encoding="utf-8")
    raise SystemExit(70)

if os.environ.get("MOCK_HOTJOIN_LEGAL_YIELD"):
    snapshot_mcp = mcp_module_paths["mcp.server"].resolve().parent
    sys.path.insert(0, str(snapshot_mcp))
    import server as trusted_generation_server

    problem_id = os.environ["RETHLAS_EXPECTED_PROBLEM_ID"]
    yield_state = os.environ["MOCK_HOTJOIN_LEGAL_YIELD"]
    if yield_state == "1":
        yield_state = "waiting_cost_gate"
    assert yield_state in {"waiting_cost_gate", "waiting_owner_advisor_decision"}
    event_payload = (
        {"event_type": "recursive_proving_round", "status": yield_state}
        if yield_state == "waiting_cost_gate"
        else {
            "event_type": "advisor_checkpoint",
            "status": yield_state,
            "owner_action_required": True,
            "browser_dispatch_authorized": False,
            "advisor_request_id": None,
        }
    )
    checkpoint = trusted_generation_server.memory_append_batch(
        problem_id,
        [
            {"channel": "events", "record": event_payload},
            {
                "channel": "branch_states",
                "record": {
                    "branch_id": "route_mock_owner_yield",
                    "state": {
                        "schema_version": "rethlas_active_route_commitment_v1",
                        "route_id": "route_mock_owner_yield",
                        "status": "active",
                        "core_bridge": (
                            "Preserve the exact unfinished frontier for owner action."
                        ),
                        "obligations": [
                            "Preserve the evidence-bound unfinished route."
                        ],
                    },
                },
            },
            {
                "channel": "branch_states",
                "record": {
                    "branch_id": "mock-cadence-branch",
                    "state": {"status": yield_state},
                },
            },
        ],
    )
    event, active_route, branch = checkpoint["records"]
    state["pending_yield_state"] = yield_state
    state["owner_yield_advisor_record_id"] = (
        event["record_id"]
        if yield_state == "waiting_owner_advisor_decision"
        else None
    )
    latest_publications = json.loads(
        state_path.read_text(encoding="utf-8")
    ).get("memory_batch_publications", {})
    state["memory_batch_publications"] = latest_publications
    state_path.write_text(canonical(state), encoding="utf-8")
    trusted_generation_server.context_handoff_prepare(
        purpose="owner_yield",
        active_route={
            "route_id": "route_mock_owner_yield",
            "core_bridge": "Preserve the exact unfinished frontier for owner action.",
        },
        # Control/advisor events are deliberately excluded from the
        # mathematical handoff frontier.  The host derives any pending owner
        # checkpoint separately from durable control memory.
        # The active-route commitment is host control input, not mathematical
        # frontier evidence, so the handoff cites only the separate durable
        # branch evidence record.
        new_record_ids=[branch["record_id"]],
        obligations=["Do not restart mathematical work before explicit owner action."],
        next_action={
            "description": "Wait for the repository owner to resume the run.",
            "test": "A fresh wrapper records the authenticated owner resume.",
        },
    )
    trusted_generation_server.generation_yield(
        problem_id,
        yield_state,
        "mock cadence legal yield",
        [event["record_id"], branch["record_id"]],
    )

sequence = json.loads(os.environ.get(
    "MOCK_CADENCE_DISPOSITIONS", '["hard_stopped_unfinalized"]'
))
run_count = int(state["run_count"]) + 1
next_disposition = sequence[min(run_count - 1, len(sequence) - 1)]
if os.environ.get("MOCK_POST_TURN_ROUTE_FROZEN"):
    next_disposition = "route_frozen"
    state["allowed_action"] = "freeze_route"
owner_yield_handoff = state.get("owner_yield_handoff")
handoff_sha256 = (
    owner_yield_handoff["content_sha256"]
    if owner_yield_handoff is not None
    else hashlib.sha256(f"handoff-{run_count}".encode()).hexdigest()
)
prior_epoch = state.get("thread_epoch")
active_epoch_number = (
    prior_epoch["thread_epoch"]
    if isinstance(prior_epoch, dict) and prior_epoch.get("state") == "pending"
    else max(run_count, 1)
)
pending_handoff = (
    next_disposition == "continue_next_cycle"
    or bool(os.environ.get("MOCK_HOTJOIN_LEGAL_YIELD"))
)
if os.environ.get("MOCK_HOTJOIN_LEGAL_YIELD"):
    # generation_yield has committed its wait record and authenticated handoff,
    # but only owner cadence-close may turn that into a resumable owner_wait.
    next_disposition = "owner_yield_close_required"
state.update({
    "disposition": next_disposition,
    "run_count": run_count,
    "thread_epoch": (
        {
            "active_turn_id": None,
            "handoff_id": f"handoff_{handoff_sha256}",
            "handoff_sha256": handoff_sha256,
            "predecessor_epoch": run_count,
            "state": "pending",
            "thread_epoch": run_count + 1,
            "thread_id": None,
        }
        if pending_handoff
        else {
            "active_turn_id": None,
            "handoff_id": None,
            "handoff_sha256": None,
            "predecessor_epoch": (
                active_epoch_number - 1 if active_epoch_number > 1 else None
            ),
            "state": "active",
            "thread_epoch": active_epoch_number,
            "thread_id": f"thread_mock_{active_epoch_number}",
        }
    ),
})
state_path.write_text(canonical(state), encoding="utf-8")
if os.environ.get("MOCK_FAIL_AFTER_YIELD_BEFORE_CADENCE_CLOSE"):
    assert next_disposition == "owner_yield_close_required"
    raise SystemExit(75)
if os.environ.get("MOCK_MUTATE_HOTJOIN_SOURCE"):
    with pathlib.Path(__file__).open("a", encoding="utf-8") as handle:
        handle.write("\n# mutated during scheduler operation\n")
print(canonical({"run_id": run_id, "disposition": next_disposition}))
"""
    adapter_source = adapter_source.replace(
        "__APPROVED_GUARDIAN_LAUNCHER_SHA256__",
        hashlib.sha256(
            (tmp_path / "agents" / "generation" / "guardian_launcher.py").read_bytes()
        ).hexdigest(),
    ).replace(
        "__APPROVED_GUARDIAN_SHA256__",
        hashlib.sha256(
            (tmp_path / "agents" / "generation" / "guardian.py").read_bytes()
        ).hexdigest(),
    ).replace(
        "__APPROVED_GUARDIAN_RUNNER_SHA256__",
        hashlib.sha256(
            (
                tmp_path
                / "agents"
                / "generation"
                / "tests"
                / "run_example.sh"
            ).read_bytes()
        ).hexdigest(),
    ).replace(
        "__GUARDIAN_CONTROL_SCHEMA_SHA256__",
        GUARDIAN_CONTROL_SCHEMA_SHA256,
    ).replace(
        "__GUARDIAN_LAUNCH_MANIFEST_SCHEMA_SHA256__",
        LAUNCH_MANIFEST_SCHEMA_SHA256,
    )
    adapter_path.write_text(
        adapter_source,
        encoding="utf-8",
    )
    return adapter_path, state_path, calls_path


def _cadence_environment(
    runner: Path,
    fake_bin: Path,
    state_path: Path,
    calls_path: Path,
    *,
    dispositions: list[str],
    max_iterations: int = 2,
    extra_environment: dict[str, str] | None = None,
) -> dict[str, str]:
    return _mock_environment(
        runner,
        fake_bin,
        mode="forged",
        extra_environment={
            "MAX_ITERATIONS": str(max_iterations),
            "MOCK_CADENCE_CALLS_FILE": str(calls_path),
            "MOCK_CADENCE_DISPOSITIONS": json.dumps(dispositions),
            "MOCK_CADENCE_EXPECTED_DB": str(
                runner.parents[2] / ".rethlas_hotjoin" / "messages.sqlite3"
            ),
            "MOCK_CADENCE_STATE_FILE": str(state_path),
            "MOCK_GUARDIAN_CONTROL_SCHEMA_SHA256": GUARDIAN_CONTROL_SCHEMA_SHA256,
            "MOCK_GUARDIAN_LAUNCHER_CALLS_FILE": str(
                calls_path.with_name("guardian-launcher-calls.jsonl")
            ),
            "MOCK_GUARDIAN_LAUNCH_MANIFEST_SCHEMA_SHA256": (
                LAUNCH_MANIFEST_SCHEMA_SHA256
            ),
            "RETHLAS_HOTJOIN_RUN_ID": "mock-cadence-live",
            **(extra_environment or {}),
        },
    )


def _cadence_calls(calls_path: Path, command: str) -> list[dict[str, object]]:
    return [
        value
        for value in map(
            json.loads, calls_path.read_text(encoding="utf-8").splitlines()
        )
        if value["command"] == command
    ]


def _assert_cadence_capabilities_are_fd_only(
    calls_path: Path,
    state_path: Path,
) -> None:
    calls = [
        json.loads(line)
        for line in calls_path.read_text(encoding="utf-8").splitlines()
    ]
    state = json.loads(state_path.read_text(encoding="utf-8"))
    owner_digests = set(state["token_digests"])
    assert state["capability_revision"] == len(state["token_digests"])
    assert state["capability_revision"] >= 1
    owner_commands = {
        "control-capability-bind",
        "cadence-admit",
        "cadence-close",
        "review-drive",
    }
    owner_calls = [call for call in calls if call["command"] in owner_commands]
    assert owner_calls
    for call in owner_calls:
        arguments = call["argv"]
        capability = call["control_capability"]
        assert isinstance(arguments, list)
        assert isinstance(capability, dict)
        assert "--control-token-fd" in arguments
        assert arguments[arguments.index("--control-token-domain") + 1] == "owner"
        assert capability["domain"] == "owner"
        assert capability["sha256"] in owner_digests
        assert call["runner_capability_sha256"] is None
        assert call["capability_env_present"] is False

    owner_manifest_calls = [
        call
        for call in calls
        if call["command"] == "review-status"
        and isinstance(call.get("control_envelope"), dict)
        and call["control_envelope"].get("payload", {}).get("operation")
        == "memory_batch_publication_status"
        and call["control_capability"] is not None
    ]
    assert owner_manifest_calls
    for call in owner_manifest_calls:
        arguments = call["argv"]
        capability = call["control_capability"]
        assert "--control-token-fd" in arguments
        assert arguments[arguments.index("--control-token-domain") + 1] == "owner"
        assert capability["domain"] == "owner"
        assert capability["sha256"] in owner_digests
        assert call["runner_capability_sha256"] is None
        assert call["capability_env_present"] is False

    runner_calls = [call for call in calls if call["command"] == "run-generator"]
    assert runner_calls
    for call in runner_calls:
        arguments = call["argv"]
        runner_digest = call["runner_capability_sha256"]
        assert isinstance(arguments, list)
        assert "--runner-token-fd" in arguments
        assert call["control_capability"] is None
        assert isinstance(runner_digest, str) and len(runner_digest) == 64
        assert runner_digest not in owner_digests
        assert call["capability_env_present"] is False

    launcher_calls_path = calls_path.with_name("guardian-launcher-calls.jsonl")
    launcher_calls = [
        json.loads(line)
        for line in launcher_calls_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(launcher_calls) == len(runner_calls)
    for launch, run_call in zip(launcher_calls, runner_calls, strict=True):
        arguments = launch["argv"]
        worker_command = launch["worker_command"]
        assert isinstance(arguments, list)
        assert isinstance(worker_command, list)
        assert "--owner-token-fd" in arguments
        assert "--runner-token-fd" not in worker_command
        assert "--control-token-fd" not in worker_command
        assert launch["owner_token_sha256"] in owner_digests
        assert launch["runner_token_sha256"] == run_call[
            "runner_capability_sha256"
        ]
        assert launch["capability_env_present"] is False


def _assert_guarded_review_drive_is_fd_only(
    calls_path: Path,
    state_path: Path,
    *,
    expected_count: int = 1,
) -> None:
    drives = _cadence_calls(calls_path, "guarded-review-drive")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    owner_digests = set(state["token_digests"])
    assert len(drives) == expected_count
    assert not _cadence_calls(calls_path, "review-drive")
    for drive in drives:
        arguments = drive["argv"]
        runner_digest = drive["runner_capability_sha256"]
        assert isinstance(arguments, list)
        assert arguments.count("--runner-token-fd") == 1
        assert "--control-token-fd" not in arguments
        assert drive["control_capability"] is None
        assert drive["control_envelope"] is None
        assert drive["capability_env_present"] is False
        assert isinstance(runner_digest, str) and len(runner_digest) == 64
        assert runner_digest not in owner_digests

    launcher_calls_path = calls_path.with_name("guardian-launcher-calls.jsonl")
    launcher_calls = [
        json.loads(line)
        for line in launcher_calls_path.read_text(encoding="utf-8").splitlines()
    ]
    guarded_launches = [
        launch
        for launch in launcher_calls
        if "guarded-review-drive" in launch["worker_command"]
    ]
    assert len(guarded_launches) == expected_count
    for launch, drive in zip(guarded_launches, drives, strict=True):
        worker_command = launch["worker_command"]
        assert launch["admission_mode"] == "same_cycle_resume"
        assert launch["owner_token_sha256"] in owner_digests
        assert launch["runner_token_sha256"] == drive[
            "runner_capability_sha256"
        ]
        assert launch["capability_env_present"] is False
        assert "--runner-token-fd" not in worker_command
        assert "--control-token-fd" not in worker_command
        guarded_index = worker_command.index("guarded-review-drive")
        assert worker_command[guarded_index + 1 :] == [
            "--run-id",
            "mock-cadence-live",
            "--boundary-id",
            "reviewbound_" + "b" * 32,
        ]


def _seed_mock_cadence_projection(
    adapter_path: Path,
    state_path: Path,
    environment: dict[str, str],
    *,
    disposition: str,
) -> None:
    subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            str(adapter_path),
            "--db",
            environment["MOCK_CADENCE_EXPECTED_DB"],
            "init",
            "--run-id",
            environment["RETHLAS_HOTJOIN_RUN_ID"],
            "--problem-id",
            "example",
        ],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.update(
        {
            "cycle_history": ["cycle_" + f"{1:032x}"],
            "cycle_id": "cycle_" + f"{1:032x}",
            "cycle_serial": 1,
            "disposition": disposition,
            "generation": 1,
            "guardian_clock_sha256": hashlib.sha256(
                b"guardian-clock-1"
            ).hexdigest(),
            "run_count": 1,
            "thread_epoch": {
                "active_turn_id": None,
                "handoff_id": None,
                "handoff_sha256": None,
                "predecessor_epoch": None,
                "state": "active",
                "thread_epoch": 1,
                "thread_id": "thread_mock_1",
            },
        }
    )
    if disposition in {
        "post_review_handoff_required",
        "continue_reviewed_cycle_fresh_epoch",
    }:
        state["allowed_action"] = "post_review_handoff_required"
    elif disposition == "route_frozen":
        state["allowed_action"] = "freeze_route"
    state_path.write_text(
        json.dumps(state, allow_nan=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )


def _make_nonfresh_ledger_copy(
    runner: Path,
    tmp_path: Path,
) -> tuple[Path, Path]:
    source = runner.parents[2] / ".rethlas_hotjoin" / "messages.sqlite3"
    source.parent.mkdir(mode=0o700)
    with sqlite3.connect(source) as connection:
        connection.execute(
            "CREATE TABLE legacy_probe (run_id TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO legacy_probe(run_id, value) VALUES (?, ?)",
            ("mock-cadence-live", "copied legacy ledger"),
        )
    source.chmod(0o600)
    copy = (tmp_path / "nonfresh-copy" / "messages.copy.sqlite3").resolve()
    copy.parent.mkdir(mode=0o700)
    shutil.copy2(source, copy)
    copy.chmod(0o600)
    assert source.read_bytes() == copy.read_bytes()
    assert source.stat().st_ino != copy.stat().st_ino
    return source, copy


def test_cadence_policy_without_hotjoin_starts_zero_codex_processes(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    codex_calls = tmp_path / "codex-calls.jsonl"
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="forged",
        extra_environment={
            "MOCK_CODEX_CALLS_FILE": str(codex_calls),
            "RETHLAS_REVIEW_CADENCE_POLICY": "rethlas_route_review_90m_v1",
            "RETHLAS_CONTEXT_GUARD_POLICY": "rethlas_context_guard_v1",
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 1
    assert "require RETHLAS_HOTJOIN_RUN_ID" in completed.stderr
    assert not codex_calls.exists()


@pytest.mark.parametrize(
    ("guardian_mode", "diagnostic"),
    [
        ("false", "guardian enforcement is not released"),
        ("missing", "guardian_enforcement_ready must be an immutable boolean"),
        ("non_boolean", "guardian_enforcement_ready must be an immutable boolean"),
    ],
)
def test_unreleased_or_malformed_guardian_policy_starts_zero_control_or_paid_work(
    tmp_path: Path,
    guardian_mode: str,
    diagnostic: str,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    codex_calls = tmp_path / "guardian-hold-codex-calls.jsonl"
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["hard_stopped"],
        max_iterations=1,
        extra_environment={
            "MOCK_CODEX_CALLS_FILE": str(codex_calls),
            "MOCK_GUARDIAN_ENFORCEMENT_READY_MODE": guardian_mode,
            # An inherited wrapper value is not an authority and cannot
            # override the immutable host policy object.
            "RETHLAS_GUARDIAN_ENFORCEMENT_READY": "true",
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 70, completed.stdout + completed.stderr
    assert diagnostic in completed.stderr
    calls = [
        json.loads(line) for line in calls_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [call["command"] for call in calls] == ["policy-contract"]
    assert not state_path.exists()
    assert not codex_calls.exists()


def test_unreleased_guardian_nonfresh_resume_dry_run_reports_migration_with_zero_codex(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    source_db, copy_db = _make_nonfresh_ledger_copy(runner, tmp_path)
    source_wal = Path(str(source_db) + "-wal")
    source_shm = Path(str(source_db) + "-shm")
    source_wal.write_bytes(b"")
    source_shm.write_bytes(b"\0" * 32768)
    source_wal.chmod(0o600)
    source_shm.chmod(0o600)
    codex_calls = tmp_path / "nonfresh-dry-run-codex-calls.jsonl"
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["hard_stopped"],
        max_iterations=1,
        extra_environment={
            "MOCK_CODEX_CALLS_FILE": str(codex_calls),
            "MOCK_GUARDIAN_ENFORCEMENT_READY_MODE": "false",
            "RETHLAS_NONFRESH_RESUME_DRY_RUN": "1",
            "RETHLAS_NONFRESH_RESUME_DB_COPY": str(copy_db),
        },
    )
    _seed_mock_cadence_projection(
        adapter,
        state_path,
        environment,
        disposition="stale_active",
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["generation"] = 2
    state["thread_epoch"]["active_turn_id"] = "turn_mock_stale"
    state_path.write_text(
        json.dumps(state, allow_nan=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    calls_path.write_text("", encoding="utf-8")
    source_before = hashlib.sha256(source_db.read_bytes()).hexdigest()

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    report = json.loads(completed.stdout)
    assert report["schema_version"] == "rethlas_nonfresh_resume_dry_run_v1"
    assert report["diagnostic"] == "copied_legacy_ledger_nonfresh_resume"
    assert report["policy"]["guardian_enforcement_ready"] is False
    assert report["observed"] == {
        "active_turn_id": "turn_mock_stale",
        "cadence_disposition": "stale_active",
        "generation": 2,
        "paid_turn_allowed": False,
        "quarantine": None,
        "thread_id": "thread_mock_1",
    }
    assert report["decision"]["requested_topology"] == "reuse_existing_thread"
    assert report["decision"]["existing_thread_preserved"] is True
    assert report["decision"]["fresh_thread_forced_by_dry_run"] is False
    assert report["decision"]["resume_admitted"] is False
    assert report["decision"]["paid_processes_started"] is False
    assert (
        report["decision"]["recovery_migration_disposition"]
        == "legacy_stale_active_offline_reconciliation_required"
    )
    assert report["source_db"]["sha256_before"] == source_before
    assert report["source_db"]["sha256_after"] == source_before
    assert report["source_db"]["unchanged"] is True
    assert report["copy_db"]["schema_or_scheduler_migrated"] is False
    assert hashlib.sha256(source_db.read_bytes()).hexdigest() == source_before
    assert source_wal.read_bytes() == b""
    assert source_shm.stat().st_size == 32768
    calls = [
        json.loads(line) for line in calls_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [call["command"] for call in calls] == [
        "policy-contract",
        "status",
        "cadence-control-state",
    ]
    assert all(str(copy_db) in call["argv"] for call in calls[1:])
    assert not _cadence_calls(calls_path, "init")
    assert not _cadence_calls(calls_path, "control-capability-bind")
    assert not _cadence_calls(calls_path, "review-drive")
    assert not _cadence_calls(calls_path, "guarded-review-drive")
    assert not _cadence_calls(calls_path, "run-generator")
    assert not codex_calls.exists()
    assert not (runner.parents[2] / ".trusted_generation_runtime").exists()
    assert not (runner.parents[2] / ".verification_receipts").exists()
    assert not (runner.parents[2] / ".rethlas_advisor").exists()
    assert "no Codex, reviewer, recovery, or paid control action" in completed.stderr
    assert "schema projection mutation was confined to the copy" in completed.stderr
    assert "resume_admitted" not in completed.stderr


def test_nonfresh_resume_dry_run_rejects_source_inode_alias_before_any_codex(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    source_db, _copy_db = _make_nonfresh_ledger_copy(runner, tmp_path)
    codex_calls = tmp_path / "nonfresh-alias-codex-calls.jsonl"
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["hard_stopped"],
        max_iterations=1,
        extra_environment={
            "MOCK_CODEX_CALLS_FILE": str(codex_calls),
            "MOCK_GUARDIAN_ENFORCEMENT_READY_MODE": "false",
            "RETHLAS_NONFRESH_RESUME_DRY_RUN": "1",
            "RETHLAS_NONFRESH_RESUME_DB_COPY": str(source_db),
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 70, completed.stdout + completed.stderr
    assert "copied-ledger DB copy aliases the source DB inode" in completed.stderr
    assert not calls_path.exists()
    assert not state_path.exists()
    assert not codex_calls.exists()


def test_nonfresh_resume_dry_run_rejects_active_source_sidecar_before_adapter(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    source_db, copy_db = _make_nonfresh_ledger_copy(runner, tmp_path)
    source_wal = Path(str(source_db) + "-wal")
    source_wal.write_bytes(b"active source sentinel")
    source_wal.chmod(0o600)
    codex_calls = tmp_path / "nonfresh-sidecar-codex-calls.jsonl"
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["hard_stopped"],
        max_iterations=1,
        extra_environment={
            "MOCK_CODEX_CALLS_FILE": str(codex_calls),
            "MOCK_GUARDIAN_ENFORCEMENT_READY_MODE": "false",
            "RETHLAS_NONFRESH_RESUME_DRY_RUN": "1",
            "RETHLAS_NONFRESH_RESUME_DB_COPY": str(copy_db),
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 70, completed.stdout + completed.stderr
    assert "source DB has a non-empty SQLite WAL" in completed.stderr
    assert not calls_path.exists()
    assert not state_path.exists()
    assert not codex_calls.exists()


def test_unreleased_guardian_stale_reconcile_is_zero_model_and_never_resumes(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    source_db, copy_db = _make_nonfresh_ledger_copy(runner, tmp_path)
    source_wal = Path(str(source_db) + "-wal")
    source_shm = Path(str(source_db) + "-shm")
    source_wal.write_bytes(b"")
    source_shm.write_bytes(b"\0" * 32768)
    source_wal.chmod(0o600)
    source_shm.chmod(0o600)
    codex_calls = tmp_path / "stale-reconcile-codex-calls.jsonl"
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["hard_stopped"],
        max_iterations=1,
        extra_environment={
            "MOCK_CODEX_CALLS_FILE": str(codex_calls),
            "MOCK_GUARDIAN_ENFORCEMENT_READY_MODE": "false",
            "RETHLAS_NONFRESH_STALE_RECONCILE": "1",
            "RETHLAS_NONFRESH_RESUME_DB_COPY": str(copy_db),
            "RETHLAS_NONFRESH_EXPECTED_THREAD_ID": "thread_mock_1",
            "RETHLAS_NONFRESH_EXPECTED_TURN_ID": "turn_mock_stale",
        },
    )
    _seed_mock_cadence_projection(
        adapter,
        state_path,
        environment,
        disposition="stale_active",
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["generation"] = 2
    state["thread_epoch"]["active_turn_id"] = "turn_mock_stale"
    state_path.write_text(
        json.dumps(state, allow_nan=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    calls_path.write_text("", encoding="utf-8")
    source_before = hashlib.sha256(source_db.read_bytes()).hexdigest()

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 70, completed.stdout + completed.stderr
    assert completed.stdout, completed.stderr
    report = json.loads(completed.stdout)
    assert report["schema_version"] == "rethlas_nonfresh_stale_reconcile_report_v1"
    assert report["run_id"] == "mock-cadence-live"
    assert report["thread_id"] == "thread_mock_1"
    assert report["turn_id"] == "turn_mock_stale"
    assert report["initial_disposition"] == "stale_active"
    assert report["post_disposition"] == "operational_blocked"
    assert report["reconcile_result"]["state"] == ("terminal_reconciled_quarantined")
    assert report["handoff_candidate"] == {
        "eligible": True,
        "resume_authority": False,
        "source_terminal_sha256": "3" * 64,
        "source_thread_read_response_sha256": "1" * 64,
        "use": (
            "host_may_extract_one_bounded_handoff_candidate_from_quarantined_thread_read"
        ),
    }
    assert report["decision"] == {
        "fresh_thread_started": False,
        "model_calls_started": 0,
        "next_action": (
            "host_may_extract_one_bounded_handoff_candidate_from_quarantined_thread_read"
        ),
        "paid_turns_started": 0,
        "read_only_app_server_calls": ["initialize", "thread/read"],
        "read_only_app_server_processes_started": 1,
        "resume_admitted": False,
    }
    assert report["source_db"]["sha256_before"] == source_before
    assert report["source_db"]["sha256_after"] == source_before
    assert hashlib.sha256(source_db.read_bytes()).hexdigest() == source_before
    assert source_wal.read_bytes() == b""
    assert source_shm.stat().st_size == 32768
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["disposition"] == "operational_blocked"
    assert state["thread_epoch"]["active_turn_id"] is None
    assert state["quarantine"]["kind"] == "adapter_loss_terminal_discontinuity"
    commands = [
        json.loads(line)["command"]
        for line in calls_path.read_text(encoding="utf-8").splitlines()
    ]
    assert commands == [
        "policy-contract",
        "stale-recovery-capability-prepare",
        "status",
        "cadence-control-state",
        "stale-turn-reconcile",
        "status",
        "cadence-control-state",
    ]
    assert "init" not in commands
    assert "control-capability-bind" not in commands
    assert "run-generator" not in commands
    assert "review-drive" not in commands
    assert "guarded-review-drive" not in commands
    assert not codex_calls.exists()
    assert "zero model/paid turns/reviewers/verifiers" in completed.stderr
    calls_text = calls_path.read_text(encoding="utf-8")
    assert "RETHLAS_STALE_RECOVERY_TOKEN" not in calls_text
    assert "RETHLAS_REVIEW_CONTROL_TOKEN" not in calls_text


def test_stale_reconcile_rejects_tampered_capability_receipt_before_app_server(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    source_db, copy_db = _make_nonfresh_ledger_copy(runner, tmp_path)
    codex_calls = tmp_path / "stale-tamper-codex-calls.jsonl"
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["hard_stopped"],
        max_iterations=1,
        extra_environment={
            "MOCK_CODEX_CALLS_FILE": str(codex_calls),
            "MOCK_GUARDIAN_ENFORCEMENT_READY_MODE": "false",
            "MOCK_TAMPER_STALE_PREPARE_RECEIPT": "1",
            "RETHLAS_NONFRESH_STALE_RECONCILE": "1",
            "RETHLAS_NONFRESH_RESUME_DB_COPY": str(copy_db),
            "RETHLAS_NONFRESH_EXPECTED_THREAD_ID": "thread_mock_1",
            "RETHLAS_NONFRESH_EXPECTED_TURN_ID": "turn_mock_stale",
        },
    )
    _seed_mock_cadence_projection(
        adapter,
        state_path,
        environment,
        disposition="stale_active",
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["generation"] = 2
    state["thread_epoch"]["active_turn_id"] = "turn_mock_stale"
    state_path.write_text(
        json.dumps(state, allow_nan=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    calls_path.write_text("", encoding="utf-8")
    source_before = hashlib.sha256(source_db.read_bytes()).hexdigest()

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 70, completed.stdout + completed.stderr
    assert "receipt SHA-256 mismatch" in completed.stderr
    commands = [
        json.loads(line)["command"]
        for line in calls_path.read_text(encoding="utf-8").splitlines()
    ]
    assert commands == [
        "policy-contract",
        "stale-recovery-capability-prepare",
    ]
    assert hashlib.sha256(source_db.read_bytes()).hexdigest() == source_before
    assert "stale-turn-reconcile" not in commands
    assert "run-generator" not in commands
    assert not codex_calls.exists()


def test_guardian_release_policy_digest_tamper_starts_zero_control_or_paid_work(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    codex_calls = tmp_path / "guardian-tamper-codex-calls.jsonl"
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["hard_stopped"],
        max_iterations=1,
        extra_environment={
            "MOCK_CODEX_CALLS_FILE": str(codex_calls),
            "MOCK_TAMPER_GUARDIAN_POLICY_DIGEST": "1",
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 70, completed.stdout + completed.stderr
    assert "review policy_sha256 mismatch" in completed.stderr
    calls = [
        json.loads(line) for line in calls_path.read_text(encoding="utf-8").splitlines()
    ]
    assert [call["command"] for call in calls] == ["policy-contract"]
    assert not state_path.exists()
    assert not codex_calls.exists()


def test_cadence_rejects_ninety_minute_prompt_clock_before_codex(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    codex_calls = tmp_path / "codex-calls.jsonl"
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="forged",
        extra_environment={
            "MOCK_CODEX_CALLS_FILE": str(codex_calls),
            "RETHLAS_DEEP_WORK_MINUTES": "90",
            "RETHLAS_HOTJOIN_RUN_ID": "mock-cadence-live",
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 1
    assert "must be 30 under rethlas_route_review_90m_v1" in completed.stderr
    assert not codex_calls.exists()


def test_cadence_rejects_non_owner_running_receipt_before_paid_turn(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    generation_server = runner.parent.parent / "mcp" / "server.py"
    source = generation_server.read_text(encoding="utf-8")
    trusted_reason = 'reason="owner_runner_started",'
    assert source.count(trusted_reason) == 1
    generation_server.write_text(
        source.replace(
            trusted_reason,
            'reason="untrusted_running_reason",',
            1,
        ),
        encoding="utf-8",
    )
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["hard_stopped"],
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 70, completed.stdout + completed.stderr
    assert not _cadence_calls(calls_path, "run-generator")
    assert "running control reason is not owner_runner_started" in completed.stderr


def test_cadence_legal_generation_yield_stops_before_another_paid_cycle(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["hard_stopped_unfinalized"],
        extra_environment={"MOCK_HOTJOIN_LEGAL_YIELD": "1"},
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert len(_cadence_calls(calls_path, "run-generator")) == 1
    assert "state=waiting_cost_gate" in completed.stdout
    assert "owner action is required before another paid turn" in completed.stdout


def test_guardian_predispatch_failure_preserves_original_error_before_cycle_check(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["hard_stopped"],
        extra_environment={"MOCK_GUARDIAN_LAUNCHER_FAIL_BEFORE_DISPATCH": "1"},
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 70, completed.stdout + completed.stderr
    assert "distinct authenticated cycle_id" not in completed.stderr
    assert "generator exited with code 70" in completed.stderr
    assert not _cadence_calls(calls_path, "run-generator")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["cycle_id"] is None
    guarded_logs = list(
        (runner.parents[2] / ".rethlas_hotjoin" / "logs").rglob("*_iter_0.md")
    )
    assert len(guarded_logs) == 1
    assert "mock guardian pre-dispatch failure" in guarded_logs[0].read_text(
        encoding="utf-8"
    )


def test_clean_early_terminal_gets_one_same_cycle_authorization_without_reset(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["continuation_authorization_required", "hard_stopped"],
        max_iterations=1,
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 1, completed.stdout + completed.stderr
    run_calls = _cadence_calls(calls_path, "run-generator")
    assert len(run_calls) == 2
    admits = _cadence_calls(calls_path, "cadence-admit")
    assert [call["control_envelope"]["payload"]["operation"] for call in admits] == [
        "continue_active_cycle"
    ]
    all_calls = [
        json.loads(line) for line in calls_path.read_text(encoding="utf-8").splitlines()
    ]
    command_order = [call["command"] for call in all_calls]
    second_run_index = [
        index
        for index, command in enumerate(command_order)
        if command == "run-generator"
    ][1]
    assert (
        command_order.index("control-capability-bind")
        < command_order.index("cadence-admit")
        < second_run_index
    )
    second_arguments = run_calls[1]["argv"]
    second_prompt = second_arguments[second_arguments.index("--prompt") + 1]
    assert "existing app-server thread epoch" in second_prompt
    assert "original absolute cycle T0" in second_prompt
    assert "T+30m/T+60m review deadlines" in second_prompt
    assert "not a new cycle or a clock reset" in second_prompt
    assert "brand-new app-server thread epoch" not in second_prompt
    _assert_cadence_capabilities_are_fd_only(calls_path, state_path)


def test_reviewer_red_without_generation_yield_freezes_before_any_root_continuation(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["continuation_authorization_required", "hard_stopped"],
        max_iterations=1,
        extra_environment={"MOCK_CADENCE_ALLOWED_ACTION": "freeze_route"},
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 70, completed.stdout + completed.stderr
    assert len(_cadence_calls(calls_path, "run-generator")) == 1
    assert not _cadence_calls(calls_path, "cadence-close")
    assert not _cadence_calls(calls_path, "cadence-admit")
    assert "owner_wait" not in (completed.stdout + completed.stderr)
    assert "allowed action" in completed.stderr


def test_same_cycle_short_turns_are_not_truncated_by_max_iterations(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["continuation_authorization_required"] * 11 + ["hard_stopped"],
        max_iterations=1,
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=90,
        check=False,
    )

    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert len(_cadence_calls(calls_path, "run-generator")) == 12
    assert len(_cadence_calls(calls_path, "cadence-admit")) == 11
    assert "finalized T+90m hard stop" in completed.stdout
    assert "Owner cycle budget" not in completed.stderr


def _set_runner_paid_root_failsafe(runner: Path, value: int) -> None:
    source = runner.read_text(encoding="utf-8")
    needle = "CADENCE_ROOT_INVOCATION_FAILSAFE=128"
    assert source.count(needle) == 1
    runner.write_text(
        source.replace(needle, f"CADENCE_ROOT_INVOCATION_FAILSAFE={value}"),
        encoding="utf-8",
    )


def test_paid_root_failsafe_resets_only_for_distinct_continue_next_cycle(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _set_runner_paid_root_failsafe(runner, 3)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=[
            "continue_active_cycle",
            "continue_active_cycle",
            "continue_next_cycle",
            "continue_active_cycle",
            "continue_active_cycle",
            "hard_stopped",
        ],
        max_iterations=2,
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=45,
        check=False,
    )

    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert len(_cadence_calls(calls_path, "run-generator")) == 6
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert len(state["cycle_history"]) == 2
    assert len(set(state["cycle_history"])) == 2
    assert "3-paid-root operational fail-safe" not in completed.stderr


def test_paid_root_failsafe_does_not_reset_inside_one_cycle(tmp_path: Path) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _set_runner_paid_root_failsafe(runner, 3)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["continue_active_cycle"] * 4,
        max_iterations=1,
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 70, completed.stdout + completed.stderr
    assert len(_cadence_calls(calls_path, "run-generator")) == 3
    assert "3-paid-root operational fail-safe" in completed.stderr
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert len(state["cycle_history"]) == 1


def test_due_review_starts_no_ordinary_full_capability_continuation(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["review_turn_authorization_required", "hard_stopped"],
        max_iterations=1,
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 70, completed.stdout + completed.stderr
    run_calls = _cadence_calls(calls_path, "run-generator")
    assert len(run_calls) == 1
    assert not _cadence_calls(calls_path, "cadence-admit")
    assert "ordinary full-capability generator continuation" in completed.stderr
    assert "trusted host review orchestration" in completed.stderr


@pytest.mark.parametrize(
    "disposition",
    [
        "review_turn_authorization_required",
        "continue_review_only",
    ],
)
def test_due_review_wrapper_restart_starts_zero_root_turns_until_host_drive(
    tmp_path: Path,
    disposition: str,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["hard_stopped"],
        max_iterations=1,
    )
    _seed_mock_cadence_projection(
        adapter,
        state_path,
        environment,
        disposition=disposition,
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 70, completed.stdout + completed.stderr
    assert not _cadence_calls(calls_path, "run-generator")
    assert not _cadence_calls(calls_path, "control-capability-bind")
    assert not _cadence_calls(calls_path, "cadence-admit")
    assert "ordinary full-capability generator turn is forbidden" in completed.stderr
    assert "No root model turn was started" in completed.stderr


def test_due_review_uses_guarded_runner_then_starts_fresh_epoch(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["hard_stopped"],
        max_iterations=1,
    )
    _seed_mock_cadence_projection(
        adapter,
        state_path,
        environment,
        disposition="review_drive_required",
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert len(_cadence_calls(calls_path, "control-capability-bind")) == 1
    drives = _cadence_calls(calls_path, "guarded-review-drive")
    assert len(drives) == 1
    assert drives[0]["control_envelope"] is None
    drive_arguments = drives[0]["argv"]
    assert drive_arguments[drive_arguments.index("--run-id") + 1] == (
        "mock-cadence-live"
    )
    assert drive_arguments[drive_arguments.index("--boundary-id") + 1] == (
        "reviewbound_" + "b" * 32
    )
    run_calls = _cadence_calls(calls_path, "run-generator")
    assert len(run_calls) == 1
    prompt = run_calls[0]["argv"][run_calls[0]["argv"].index("--prompt") + 1]
    assert prompt.startswith("[TRUSTED HOST REHYDRATION REQUIRED]")
    all_calls = [
        json.loads(line) for line in calls_path.read_text(encoding="utf-8").splitlines()
    ]
    command_order = [call["command"] for call in all_calls]
    assert command_order.index("guarded-review-drive") < command_order.index(
        "run-generator"
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["review_drive_count"] == 1
    assert state["reviewed_handoff_consumed_count"] == 1
    assert state["cycle_history"] == ["cycle_" + f"{1:032x}"]
    assert state["disposition"] == "hard_stopped"
    _assert_guarded_review_drive_is_fd_only(calls_path, state_path)
    assert "same-cycle fresh epoch is ready" in completed.stderr


def test_due_review_red_freezes_route_without_owner_yield_or_paid_root(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["hard_stopped"],
        max_iterations=1,
        extra_environment={"MOCK_REVIEW_DRIVE_RED": "1"},
    )
    _seed_mock_cadence_projection(
        adapter,
        state_path,
        environment,
        disposition="review_drive_required",
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert len(_cadence_calls(calls_path, "guarded-review-drive")) == 1
    assert not _cadence_calls(calls_path, "run-generator")
    assert not _cadence_calls(calls_path, "cadence-admit")
    assert not _cadence_calls(calls_path, "cadence-close")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["disposition"] == "route_frozen"
    assert state["allowed_action"] == "freeze_route"
    _assert_guarded_review_drive_is_fd_only(calls_path, state_path)
    assert "owner_wait" not in (completed.stdout + completed.stderr)
    assert "official review froze the active route after red" in completed.stderr
    assert "no authorized fallback" in completed.stderr

    restarted = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert restarted.returncode == 1, restarted.stdout + restarted.stderr
    assert len(_cadence_calls(calls_path, "control-capability-bind")) == 1
    assert len(_cadence_calls(calls_path, "guarded-review-drive")) == 1
    assert not _cadence_calls(calls_path, "run-generator")
    assert "state=route_frozen" in restarted.stderr
    assert "authorizes no additional paid work" in restarted.stderr


def test_initial_route_frozen_is_normal_unsolved_terminal_with_zero_paid_work(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["hard_stopped"],
        max_iterations=1,
    )
    _seed_mock_cadence_projection(
        adapter,
        state_path,
        environment,
        disposition="route_frozen",
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert not _cadence_calls(calls_path, "control-capability-bind")
    assert not _cadence_calls(calls_path, "review-drive")
    assert not _cadence_calls(calls_path, "guarded-review-drive")
    assert not _cadence_calls(calls_path, "run-generator")
    assert not _cadence_calls(calls_path, "cadence-admit")
    assert not _cadence_calls(calls_path, "cadence-close")
    assert "official red verdict with no authorized fallback" in completed.stderr
    assert "not an owner/advisor wait" in completed.stderr


def test_post_turn_route_frozen_stops_normally_before_any_new_paid_turn(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["continue_active_cycle"],
        max_iterations=1,
        extra_environment={"MOCK_POST_TURN_ROUTE_FROZEN": "1"},
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert len(_cadence_calls(calls_path, "run-generator")) == 1
    assert not _cadence_calls(calls_path, "cadence-admit")
    assert not _cadence_calls(calls_path, "cadence-close")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["disposition"] == "route_frozen"
    assert state["allowed_action"] == "freeze_route"
    assert (
        "no owner/advisor wait or paid continuation is authorized" in completed.stderr
    )


def test_post_review_handoff_gate_restarts_zero_root_or_reviewer_processes(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["hard_stopped"],
        max_iterations=1,
    )
    _seed_mock_cadence_projection(
        adapter,
        state_path,
        environment,
        disposition="post_review_handoff_required",
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 70, completed.stdout + completed.stderr
    assert not _cadence_calls(calls_path, "run-generator")
    assert not _cadence_calls(calls_path, "control-capability-bind")
    assert not _cadence_calls(calls_path, "review-drive")
    assert not _cadence_calls(calls_path, "guarded-review-drive")
    assert "host-prepared fresh-epoch handoff is not yet available" in completed.stderr


def test_reviewed_epoch_restart_requires_existing_guardian_settle_without_second_root(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["hard_stopped"],
        max_iterations=1,
        extra_environment={"MOCK_FAIL_AFTER_REVIEWED_EPOCH_DISPATCH": "1"},
    )
    _seed_mock_cadence_projection(
        adapter,
        state_path,
        environment,
        disposition="continue_reviewed_cycle_fresh_epoch",
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    handoff_sha256 = "9" * 64
    state["thread_epoch"] = {
        "active_turn_id": None,
        "handoff_id": f"handoff_{handoff_sha256}",
        "handoff_sha256": handoff_sha256,
        "predecessor_epoch": 1,
        "state": "pending",
        "thread_epoch": 2,
        "thread_id": None,
    }
    state_path.write_text(
        json.dumps(state, allow_nan=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )

    first = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    # The external launcher normalizes every nonzero worker terminal to the
    # fail-closed host code; the durable disposition remains the source of the
    # more specific recovery state.
    assert first.returncode == 70, first.stdout + first.stderr
    failed_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert failed_state["disposition"] == "terminal_observed_pending_finalization"
    assert failed_state["paid_root_count"] == 1

    restarted_environment = dict(environment)
    restarted_environment.pop("MOCK_FAIL_AFTER_REVIEWED_EPOCH_DISPATCH")
    second = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=restarted_environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert second.returncode == 70, second.stdout + second.stderr
    run_calls = _cadence_calls(calls_path, "run-generator")
    assert len(run_calls) == 1
    assert "prior root terminal is still settling under its existing Guardian" in (
        second.stderr
    )
    assert "refusing capability rotation or a second root" in second.stderr
    final_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert final_state["paid_root_count"] == 1
    assert final_state["disposition"] == "terminal_observed_pending_finalization"
    assert final_state["capability_revision"] == failed_state["capability_revision"]
    assert final_state["token_digests"] == failed_state["token_digests"]
    assert final_state["generation_control_instances"] == failed_state[
        "generation_control_instances"
    ]


def test_review_boundary_recovery_reaps_only_existing_root_and_descendants(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["hard_stopped"],
        max_iterations=1,
    )
    _seed_mock_cadence_projection(
        adapter,
        state_path,
        environment,
        disposition="review_boundary_recovery_required",
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 1, completed.stdout + completed.stderr
    run_calls = _cadence_calls(calls_path, "run-generator")
    assert len(run_calls) == 2
    prompt = run_calls[0]["argv"][run_calls[0]["argv"].index("--prompt") + 1]
    assert "Recover only the already-authorized durable scheduler operation" in prompt
    assert "Do not start a new paid turn" in prompt
    fresh_prompt = run_calls[1]["argv"][run_calls[1]["argv"].index("--prompt") + 1]
    assert fresh_prompt.startswith("[TRUSTED HOST REHYDRATION REQUIRED]")
    all_calls = [
        json.loads(line) for line in calls_path.read_text(encoding="utf-8").splitlines()
    ]
    command_order = [call["command"] for call in all_calls]
    recovery_index, fresh_index = [
        index
        for index, command in enumerate(command_order)
        if command == "run-generator"
    ]
    drive_index = command_order.index("guarded-review-drive")
    assert recovery_index < drive_index < fresh_index
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["run_count"] == 2
    assert state["review_drive_count"] == 1
    assert state["reviewed_handoff_consumed_count"] == 1
    assert state["disposition"] == "hard_stopped"
    assert len(_cadence_calls(calls_path, "guarded-review-drive")) == 1
    _assert_guarded_review_drive_is_fd_only(calls_path, state_path)
    assert "same-cycle fresh epoch is ready" in completed.stderr


@pytest.mark.parametrize(
    ("dispositions", "expiry_environment", "authorized_disposition"),
    [
        (
            ["continuation_authorization_required"],
            "MOCK_ACTIVE_AUTH_EXPIRED_AT_REVIEW_DUE",
            "continue_active_cycle",
        ),
    ],
)
def test_pre_rpc_cas_starts_zero_root_turns_under_expired_authorization(
    tmp_path: Path,
    dispositions: list[str],
    expiry_environment: str,
    authorized_disposition: str,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=dispositions,
        max_iterations=1,
        extra_environment={expiry_environment: "1"},
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 70, completed.stdout + completed.stderr
    assert len(_cadence_calls(calls_path, "run-generator")) == 2
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["run_count"] == 1
    assert state["disposition"] != authorized_disposition


def test_active_cycle_authorization_survives_true_wrapper_restart_with_rotation(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["continuation_authorization_required", "hard_stopped"],
        extra_environment={"MOCK_FAIL_WRAPPER_AFTER_ACTIVE_ADMIT": "1"},
    )

    first = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert first.returncode == 70, first.stdout + first.stderr
    assert len(_cadence_calls(calls_path, "run-generator")) == 1
    first_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert first_state["disposition"] == "continue_active_cycle"

    restarted_environment = dict(environment)
    restarted_environment.pop("MOCK_FAIL_WRAPPER_AFTER_ACTIVE_ADMIT")
    second = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=restarted_environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert second.returncode == 1, second.stdout + second.stderr
    assert len(_cadence_calls(calls_path, "run-generator")) == 2
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert len(set(state["token_digests"])) == 2
    assert len(set(state["helper_paths"])) == 2
    assert len(set(state["review_driver_paths"])) == 2
    assert len(set(state["generation_control_instances"])) == 2
    assert len(set(state["helper_digests"])) == 1
    assert len(set(state["review_driver_digests"])) == 1
    assert len(set(state["review_driver_package_digests"])) == 1
    assert len(set(state["runtime_digests"])) == 1
    assert len(set(state["codex_digests"])) == 1


@pytest.mark.parametrize(
    ("yield_state", "owner_wait"),
    [
        ("waiting_cost_gate", "owner_wait_cost"),
        ("waiting_owner_advisor_decision", "owner_wait_advisor"),
    ],
)
def test_authenticated_owner_yield_closes_and_resumes_on_fresh_epoch(
    tmp_path: Path,
    yield_state: str,
    owner_wait: str,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["hard_stopped_unfinalized", "hard_stopped"],
        extra_environment={"MOCK_HOTJOIN_LEGAL_YIELD": yield_state},
    )

    first = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert first.returncode == 0, first.stdout + first.stderr
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["disposition"] == owner_wait
    closes = _cadence_calls(calls_path, "cadence-close")
    assert closes[-1]["control_envelope"]["payload"]["operation"] == "owner_yield"

    restarted_environment = dict(environment)
    restarted_environment.pop("MOCK_HOTJOIN_LEGAL_YIELD")
    second = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=restarted_environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert second.returncode == 1, second.stdout + second.stderr
    run_calls = _cadence_calls(calls_path, "run-generator")
    assert len(run_calls) == 2
    second_arguments = run_calls[1]["argv"]
    second_prompt = second_arguments[second_arguments.index("--prompt") + 1]
    assert "brand-new app-server thread epoch" in second_prompt
    admits = _cadence_calls(calls_path, "cadence-admit")
    assert admits[-1]["control_envelope"]["payload"]["operation"] == "owner_resume"


def test_owner_resume_crash_after_running_receipt_keeps_wait_until_next_invocation(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["hard_stopped_unfinalized", "hard_stopped"],
        extra_environment={"MOCK_HOTJOIN_LEGAL_YIELD": "waiting_cost_gate"},
    )

    yielded = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert yielded.returncode == 0, yielded.stdout + yielded.stderr
    assert json.loads(state_path.read_text(encoding="utf-8"))["disposition"] == (
        "owner_wait_cost"
    )

    crashing_environment = dict(environment)
    crashing_environment.pop("MOCK_HOTJOIN_LEGAL_YIELD")
    crashing_environment["MOCK_FAIL_BEFORE_OWNER_RESUME_CAS"] = "1"
    crashed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=crashing_environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert crashed.returncode == 70, crashed.stdout + crashed.stderr
    state_after_crash = json.loads(state_path.read_text(encoding="utf-8"))
    assert state_after_crash["disposition"] == "owner_wait_cost"
    assert len(_cadence_calls(calls_path, "run-generator")) == 1
    failed_admit = _cadence_calls(calls_path, "cadence-admit")[-1]
    assert failed_admit["control_envelope"]["payload"]["operation"] == "owner_resume"
    assert (
        failed_admit["control_envelope"]["payload"]["generation_control_receipt"][
            "control"
        ]["instance_id"]
        == state_after_crash["generation_control_instances"][-1]
    )

    resumed_environment = dict(crashing_environment)
    resumed_environment.pop("MOCK_FAIL_BEFORE_OWNER_RESUME_CAS")
    resumed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=resumed_environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert resumed.returncode == 1, resumed.stdout + resumed.stderr
    final_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert len(_cadence_calls(calls_path, "run-generator")) == 2
    assert len(set(final_state["generation_control_instances"])) == 3
    successful_admit = _cadence_calls(calls_path, "cadence-admit")[-1]
    assert (
        successful_admit["control_envelope"]["payload"]["generation_control_receipt"][
            "control"
        ]["instance_id"]
        == final_state["generation_control_instances"][-1]
    )
    assert (
        successful_admit["control_envelope"]["payload"]["generation_control_receipt"][
            "control"
        ]["instance_id"]
        != failed_admit["control_envelope"]["payload"]["generation_control_receipt"][
            "control"
        ]["instance_id"]
    )


@pytest.mark.parametrize(
    "yield_state",
    ["waiting_cost_gate", "waiting_owner_advisor_decision"],
)
def test_restart_preserves_prior_owner_yield_until_host_recovery_is_available(
    tmp_path: Path,
    yield_state: str,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["hard_stopped_unfinalized", "hard_stopped"],
        extra_environment={
            "MOCK_HOTJOIN_LEGAL_YIELD": yield_state,
            "MOCK_FAIL_AFTER_YIELD_BEFORE_CADENCE_CLOSE": "1",
        },
    )

    crashed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    # Guardian exposes one uniform fail-closed return code while the durable
    # owner-yield-close disposition preserves the exact interrupted operation.
    assert crashed.returncode == 70, crashed.stdout + crashed.stderr
    crashed_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert crashed_state["disposition"] == "owner_yield_close_required"
    assert len(_cadence_calls(calls_path, "run-generator")) == 1
    assert not _cadence_calls(calls_path, "cadence-close")
    prior_instances = list(crashed_state["generation_control_instances"])
    prior_token_digests = list(crashed_state["token_digests"])

    restarted_environment = dict(environment)
    restarted_environment.pop("MOCK_HOTJOIN_LEGAL_YIELD")
    restarted_environment.pop("MOCK_FAIL_AFTER_YIELD_BEFORE_CADENCE_CLOSE")
    resumed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=restarted_environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert resumed.returncode == 70, resumed.stdout + resumed.stderr
    final_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert final_state["disposition"] == "owner_yield_close_required"
    assert final_state["generation_control_instances"] == prior_instances
    assert final_state["token_digests"] == prior_token_digests
    assert len(_cadence_calls(calls_path, "run-generator")) == 1
    assert not _cadence_calls(calls_path, "cadence-close")
    assert "wait receipt will not be overwritten" in resumed.stderr


def test_owner_yield_without_exact_host_bound_handoff_fails_closed(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["hard_stopped_unfinalized"],
        extra_environment={
            "MOCK_HOTJOIN_LEGAL_YIELD": "waiting_cost_gate",
            "MOCK_CORRUPT_OWNER_YIELD_HANDOFF": "1",
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 70, completed.stdout + completed.stderr
    assert not _cadence_calls(calls_path, "cadence-close")
    assert "handoff binding is invalid" in completed.stderr


def test_continue_next_cycle_is_the_only_new_paid_cycle_disposition(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["continue_next_cycle", "hard_stopped_unfinalized"],
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 70, completed.stdout + completed.stderr
    run_calls = _cadence_calls(calls_path, "run-generator")
    assert len(run_calls) == 2
    second_arguments = run_calls[1]["argv"]
    assert isinstance(second_arguments, list)
    second_prompt = second_arguments[second_arguments.index("--prompt") + 1]
    assert "brand-new app-server thread epoch" in second_prompt
    assert "authenticated context handoff" in second_prompt
    assert "own durable pre-dispatch T0" in second_prompt
    assert "new absolute review/close/hard-stop deadlines" in second_prompt
    assert "never resets or extends the already closed prior cycle" in second_prompt
    assert "unchanged T+90m hard stop" not in second_prompt
    assert "disposition=hard_stopped_unfinalized" in completed.stderr
    assert "RETHLAS_REVIEW_CONTROL_TOKEN" not in calls_path.read_text(encoding="utf-8")
    for run_call in run_calls:
        arguments = run_call["argv"]
        mcp = tomllib.loads(
            "value=" + arguments[arguments.index("--mcp-config-toml") + 1]
        )["value"]
        assert "RETHLAS_REVIEW_CONTROL_TOKEN" not in mcp["env"]
        assert all("MASTER" not in name for name in mcp["env"])
        assert len(arguments[arguments.index("--trusted-runtime-sha256") + 1]) == 64
    for log_path in Path(environment["LOG_DIR"]).glob("*.md"):
        assert "RETHLAS_REVIEW_CONTROL_TOKEN" not in log_path.read_text(
            encoding="utf-8"
        )


def test_continue_next_cycle_requires_authenticated_pending_fresh_epoch(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["continue_next_cycle"],
        extra_environment={"MOCK_CORRUPT_CONTINUE_EPOCH": "1"},
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 70, completed.stdout + completed.stderr
    assert len(_cadence_calls(calls_path, "run-generator")) == 1
    assert "forbids a paid turn" in completed.stderr
    assert "no paid turn was started" in completed.stderr


@pytest.mark.parametrize(
    ("first_disposition", "expected_calls", "expected_text"),
    [
        ("continue_next_cycle", 2, "brand-new app-server thread epoch"),
        ("hard_stopped", 1, "finalized T+90m hard stop"),
    ],
)
def test_t90_continues_only_with_t87_validated_handoff(
    tmp_path: Path,
    first_disposition: str,
    expected_calls: int,
    expected_text: str,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    dispositions = (
        ["continue_next_cycle", "hard_stopped"]
        if first_disposition == "continue_next_cycle"
        else ["hard_stopped"]
    )
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=dispositions,
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 1, completed.stdout + completed.stderr
    run_calls = _cadence_calls(calls_path, "run-generator")
    assert len(run_calls) == expected_calls
    combined = completed.stdout + completed.stderr
    if expected_calls == 2:
        second_arguments = run_calls[1]["argv"]
        second_prompt = second_arguments[second_arguments.index("--prompt") + 1]
        assert expected_text in second_prompt
    else:
        assert expected_text in combined


def test_offline_absolute_t90_preflight_starts_zero_root_or_reviewer_turns(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    reviewer_calls = tmp_path / "reviewer-calls.jsonl"
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["continuation_authorization_required"],
        extra_environment={
            "MOCK_ABSOLUTE_DEADLINE_EXPIRED": "1",
            "MOCK_REVIEWER_CALLS_FILE": str(reviewer_calls),
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 70, completed.stdout + completed.stderr
    assert not _cadence_calls(calls_path, "run-generator")
    assert not _cadence_calls(calls_path, "control-capability-bind")
    assert not reviewer_calls.exists()
    assert "no model or recovery turn is authorized" in completed.stderr


def test_continue_wrapper_restart_rotates_token_and_snapshot_path_not_identity(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["continue_next_cycle"],
        max_iterations=1,
    )

    first = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    second = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert first.returncode == 1, first.stdout + first.stderr
    assert second.returncode == 1, second.stdout + second.stderr
    assert len(_cadence_calls(calls_path, "run-generator")) == 2
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert len(set(state["token_digests"])) == 2
    assert len(set(state["helper_paths"])) == 2
    assert len(set(state["review_driver_paths"])) == 2
    assert len(set(state["generation_control_instances"])) == 2
    assert len(set(state["helper_digests"])) == 1
    assert len(set(state["review_driver_digests"])) == 1
    assert len(set(state["review_driver_package_digests"])) == 1
    assert len(set(state["runtime_digests"])) == 1
    assert len(set(state["codex_digests"])) == 1
    assert all("runtime." in path for path in state["helper_paths"])


def test_hard_stopped_unfinalized_wrapper_restart_starts_no_recovery_or_paid_turn(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["hard_stopped_unfinalized"],
    )

    first = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert first.returncode == 70, first.stdout + first.stderr
    assert len(_cadence_calls(calls_path, "run-generator")) == 1

    second = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert second.returncode == 70, second.stdout + second.stderr
    assert len(_cadence_calls(calls_path, "run-generator")) == 1
    assert "no model or recovery turn is authorized" in second.stderr
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert len(state["token_digests"]) == 1


def test_pending_hard_stop_terminal_requires_existing_guardian_not_a_new_root(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["terminal_observed_pending_finalization", "hard_stopped"],
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 70, completed.stdout + completed.stderr
    run_calls = _cadence_calls(calls_path, "run-generator")
    assert len(run_calls) == 1
    assert "a pending terminal must be finalized by its existing Guardian" in (
        completed.stderr
    )
    assert "Could not derive an exact Guardian admission" in completed.stderr
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["disposition"] == "terminal_observed_pending_finalization"
    assert state["paid_root_count"] == 1


def test_finalized_hard_stop_is_normal_unsolved_terminal_not_operational_error(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["hard_stopped"],
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert len(_cadence_calls(calls_path, "run-generator")) == 1
    assert "finalized T+90m hard stop" in completed.stdout
    assert "no additional paid cycle is authorized" in completed.stdout
    assert "state=hard_stopped" in completed.stderr
    assert "operational" not in (completed.stdout + completed.stderr).lower()

    restarted = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert restarted.returncode == 1, restarted.stdout + restarted.stderr
    assert len(_cadence_calls(calls_path, "run-generator")) == 1
    assert "already at its finalized T+90m hard stop" in restarted.stderr
    assert "No recovery or additional paid cycle is authorized" in restarted.stderr


def test_recovery_that_remains_pending_fails_closed_without_second_recovery(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["terminal_observed_pending_finalization"],
        max_iterations=3,
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 70, completed.stdout + completed.stderr
    assert len(_cadence_calls(calls_path, "run-generator")) == 1
    assert "a pending terminal must be finalized by its existing Guardian" in (
        completed.stderr
    )
    assert "Could not derive an exact Guardian admission" in completed.stderr
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["disposition"] == "terminal_observed_pending_finalization"
    assert state["paid_root_count"] == 1


def test_cadence_source_mutation_fails_after_fingerprint_bound_invocation(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["continue_next_cycle"],
        max_iterations=1,
        extra_environment={"MOCK_MUTATE_HOTJOIN_SOURCE": "1"},
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 70, completed.stdout + completed.stderr
    assert len(_cadence_calls(calls_path, "run-generator")) == 1
    assert (
        "Trusted Guardian/control/helper/Codex sources changed during iter=0"
        in completed.stderr
    )
    assert "refusing to continue" in completed.stderr


def test_review_helper_mutation_before_spawn_starts_zero_reviewer_and_root_turns(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    helper_source = tmp_path / "agents" / "review" / "contract_cli.py"
    reviewer_calls = tmp_path / "reviewer-calls.jsonl"
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["continue_next_cycle"],
        extra_environment={
            "MOCK_MUTATE_REVIEW_HELPER_DURING_PREFLIGHT": "1",
            "MOCK_REVIEW_HELPER_SOURCE": str(helper_source),
            "MOCK_REVIEWER_CALLS_FILE": str(reviewer_calls),
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 70, completed.stdout + completed.stderr
    assert not _cadence_calls(calls_path, "run-generator")
    assert not reviewer_calls.exists()
    assert "Trusted control/helper/Codex sources changed" in completed.stderr


def test_review_driver_dependency_mutation_starts_zero_reviewer_and_root_turns(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    driver_dependency = runner.parent.parent / "mcp" / "review_client.py"
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["hard_stopped"],
        extra_environment={
            "MOCK_MUTATE_REVIEW_DRIVER_PACKAGE_DURING_PREFLIGHT": "1",
            "MOCK_REVIEW_DRIVER_PACKAGE_SOURCE": str(driver_dependency),
        },
    )
    _seed_mock_cadence_projection(
        adapter,
        state_path,
        environment,
        disposition="review_drive_required",
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 70, completed.stdout + completed.stderr
    assert not _cadence_calls(calls_path, "control-capability-bind")
    assert not _cadence_calls(calls_path, "review-drive")
    assert not _cadence_calls(calls_path, "guarded-review-drive")
    assert not _cadence_calls(calls_path, "run-generator")
    assert "Trusted control/helper/Codex sources changed" in completed.stderr


def test_recursive_cost_policy_mutation_starts_zero_reviewer_and_root_turns(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    skill_source = (
        runner.parent.parent / ".agents" / "skills" / "recursive-proving" / "SKILL.md"
    )
    reviewer_calls = tmp_path / "reviewer-calls.jsonl"
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["continue_next_cycle"],
        extra_environment={
            "MOCK_MUTATE_RECURSIVE_SKILL_DURING_PREFLIGHT": "1",
            "MOCK_RECURSIVE_SKILL_SOURCE": str(skill_source),
            "MOCK_REVIEWER_CALLS_FILE": str(reviewer_calls),
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 70, completed.stdout + completed.stderr
    assert not _cadence_calls(calls_path, "run-generator")
    assert not _cadence_calls(calls_path, "control-capability-bind")
    assert not reviewer_calls.exists()
    assert "Trusted control/helper/Codex sources changed" in completed.stderr


def test_codex_mutation_before_spawn_starts_zero_reviewer_and_root_turns(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    _adapter, state_path, calls_path = _install_mock_cadence_adapter(tmp_path)
    reviewer_calls = tmp_path / "reviewer-calls.jsonl"
    environment = _cadence_environment(
        runner,
        fake_bin,
        state_path,
        calls_path,
        dispositions=["continue_next_cycle"],
        extra_environment={
            "MOCK_MUTATE_CODEX_DURING_PREFLIGHT": "1",
            "MOCK_CODEX_SOURCE": str(fake_bin / "codex"),
            "MOCK_REVIEWER_CALLS_FILE": str(reviewer_calls),
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 70, completed.stdout + completed.stderr
    assert not _cadence_calls(calls_path, "run-generator")
    assert not _cadence_calls(calls_path, "control-capability-bind")
    assert not reviewer_calls.exists()
    assert "Trusted control/helper/Codex sources changed" in completed.stderr


def test_group_writable_codex_binary_is_rejected_before_execution(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    codex_calls = tmp_path / "codex-calls.jsonl"
    codex_path = fake_bin / "codex"
    codex_path.chmod(0o775)
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="forged",
        extra_environment={"MOCK_CODEX_CALLS_FILE": str(codex_calls)},
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert "must not be group/world-writable" in completed.stderr
    assert not codex_calls.exists()


def test_runner_accepts_mock_atomic_publication_receipt(tmp_path: Path) -> None:
    completed = _run_mock(tmp_path, mode="trusted")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "Solved problem_id=example" in completed.stdout


def test_runner_prompts_enforce_reasoning_first_phase_sequence(tmp_path: Path) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    calls_file = tmp_path / "codex-calls.jsonl"
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="forged",
        extra_environment={
            "MAX_ITERATIONS": "3",
            "RETHLAS_DEEP_WORK_MINUTES": "90",
            "MOCK_CODEX_CALLS_FILE": str(calls_file),
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 1
    calls = [json.loads(line) for line in calls_file.read_text().splitlines()]
    exec_calls = [call for call in calls if "exec" in call]
    assert len(exec_calls) == 3
    prompts = [call[-1] for call in exec_calls]
    assert all("reasoning_contract=rethlas_reasoning_first_v1" in p for p in prompts)
    assert "protected root deep-work phase" in prompts[0]
    assert "at least 90 minutes" in prompts[0]
    assert "do not initialize or write memory" in prompts[0]
    assert "primary plan plus at most one materially different fallback" in prompts[0]
    assert "single pre-critic write-behind checkpoint" in prompts[0]
    assert "at most one bounded memory_search" in prompts[1]
    assert "Do not use arXiv theorem search or web search" in prompts[1]
    assert "capabilities, not obligations" in prompts[2]
    assert "one named external knowledge gap" in prompts[2]
    assert all("candidate fast lane" in prompt for prompt in prompts)

    web_modes = []
    for call in exec_calls:
        config_values = [
            call[index + 1]
            for index, value in enumerate(call[:-1])
            if value == "--config" and call[index + 1].startswith("web_search=")
        ]
        assert len(config_values) == 1
        web_modes.append(config_values[0])
    assert web_modes == [
        'web_search="disabled"',
        'web_search="disabled"',
        'web_search="live"',
    ]


@pytest.mark.parametrize(
    "waiting_state",
    ("waiting_cost_gate", "waiting_owner_advisor_decision"),
)
def test_runner_stops_before_a_second_paid_turn_for_durable_legal_yield(
    tmp_path: Path, waiting_state: str
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    calls_file = tmp_path / "codex-calls.jsonl"
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="forged",
        extra_environment={
            "MAX_ITERATIONS": "2",
            "MOCK_CODEX_CALLS_FILE": str(calls_file),
            "MOCK_GENERATION_CONTROL_STATE": waiting_state,
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    calls = [json.loads(line) for line in calls_file.read_text().splitlines()]
    assert len([call for call in calls if "exec" in call]) == 1
    assert not (Path(environment["LOG_DIR"]) / "example_iter_1.md").exists()
    assert f"state={waiting_state}" in completed.stdout
    assert "owner action is required before another paid turn" in completed.stdout
    assert "The theorem remains unsolved" in completed.stdout
    assert "Solved problem_id=" not in completed.stdout


def test_runner_ordinary_unfinished_turn_still_advances_to_iteration_limit(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    calls_file = tmp_path / "codex-calls.jsonl"
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="forged",
        extra_environment={
            "MAX_ITERATIONS": "2",
            "MOCK_CODEX_CALLS_FILE": str(calls_file),
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 1
    calls = [json.loads(line) for line in calls_file.read_text().splitlines()]
    assert len([call for call in calls if "exec" in call]) == 2
    assert (Path(environment["LOG_DIR"]) / "example_iter_1.md").exists()
    assert "Reached MAX_ITERATIONS=2" in completed.stderr


@pytest.mark.parametrize("invalid_minutes", ("0", "9", "91", "thirty"))
def test_runner_rejects_invalid_deep_work_window_before_codex(
    tmp_path: Path,
    invalid_minutes: str,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    calls_file = tmp_path / "codex-calls.jsonl"
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="forged",
        extra_environment={
            "RETHLAS_DEEP_WORK_MINUTES": invalid_minutes,
            "MOCK_CODEX_CALLS_FILE": str(calls_file),
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 1
    assert "RETHLAS_DEEP_WORK_MINUTES" in completed.stderr
    assert not calls_file.exists()


def test_legacy_runner_drops_inherited_advisor_and_hotjoin_bindings(
    tmp_path: Path,
) -> None:
    completed = _run_mock(
        tmp_path,
        mode="trusted",
        extra_environment={
            "MOCK_EXPECT_NO_ADVISOR_ENV": "1",
            "MOCK_GUARDIAN_ENFORCEMENT_READY_MODE": "false",
            "RETHLAS_ADVISOR_RECEIPTS_ROOT": "/tmp/inherited-advisor-root",
            "RETHLAS_EXPECTED_HOTJOIN_RUN_ID": "stale-owner-run",
            "RETHLAS_GUARDIAN_ENFORCEMENT_READY": "false",
        },
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_runner_derives_exact_three_server_checkpoint_split_from_one_base(
    tmp_path: Path,
) -> None:
    completed = _run_mock(tmp_path, mode="trusted")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    generation_root = tmp_path / "agents" / "generation"
    servers = json.loads(
        (generation_root / "reasoning_mcp_server_map_seen.json").read_text(
            encoding="utf-8"
        )
    )
    assert list(servers) == [
        "reasoning_agent",
        "reasoning_checkpoint_primary",
        "reasoning_checkpoint_recovery",
    ]
    reasoning = servers["reasoning_agent"]
    assert set(reasoning) == {
        "command",
        "args",
        "cwd",
        "env",
        "required",
        "tool_timeout_sec",
        "default_tools_approval_mode",
        "disabled_tools",
    }
    assert reasoning["default_tools_approval_mode"] == "approve"
    assert reasoning["required"] is True
    assert reasoning["tool_timeout_sec"] == 3600
    assert reasoning["disabled_tools"] == ["memory_append_batch"]
    for checkpoint_id in (
        "reasoning_checkpoint_primary",
        "reasoning_checkpoint_recovery",
    ):
        checkpoint = servers[checkpoint_id]
        assert checkpoint["default_tools_approval_mode"] == "approve"
        assert checkpoint["required"] is True
        assert checkpoint["tool_timeout_sec"] == 60
        assert checkpoint["enabled_tools"] == ["memory_append_batch"]
    common_keys = {
        "command",
        "args",
        "cwd",
        "env",
        "required",
        "default_tools_approval_mode",
    }
    assert len(
        {
            json.dumps(
                {key: server[key] for key in common_keys},
                sort_keys=True,
                separators=(",", ":"),
            )
            for server in servers.values()
        }
    ) == 1
    assert reasoning["args"][:3] == ["-I", "-B", "-c"]
    commitments = reasoning["args"][4:]
    assert len(commitments) == 21
    for offset in range(0, len(commitments), 3):
        module_name, module_path, module_sha256 = commitments[offset : offset + 3]
        assert module_name.startswith(("mcp.", "review."))
        assert Path(module_path).is_absolute()
        assert not Path(module_path).is_relative_to(generation_root.resolve())
        assert len(module_sha256) == 64


def test_runner_injects_minimal_shell_path_with_preflighted_python(
    tmp_path: Path,
) -> None:
    completed = _run_mock(tmp_path, mode="trusted")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    generation_root = tmp_path / "agents" / "generation"
    policy = json.loads(
        (generation_root / "shell_environment_policy_seen.json").read_text(
            encoding="utf-8"
        )
    )
    runtime_bin = tmp_path / "agents" / ".generation-venv" / "bin"
    assert policy == {
        "inherit": "none",
        "set": {
            "PATH": f"{runtime_bin}:/usr/bin:/bin:/usr/sbin:/sbin",
        },
    }


def test_runner_rejects_symlink_python_before_control_or_codex(
    tmp_path: Path,
) -> None:
    runner, runtime_bin = _make_runner_tree(tmp_path)
    python3 = runtime_bin / "python3"
    python3.unlink()
    python3.symlink_to("python")
    calls_file = tmp_path / "codex-calls.jsonl"
    environment = _mock_environment(
        runner,
        runtime_bin,
        mode="forged",
        extra_environment={
            "MOCK_CODEX_CALLS_FILE": str(calls_file),
            "RETHLAS_HOTJOIN_RUN_ID": "symlink-runtime-must-not-start",
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 1
    assert "non-symlink Python interpreter" in completed.stderr
    assert "venv --copies" in completed.stderr
    assert not calls_file.exists()
    assert not (runner.parents[2] / ".rethlas_hotjoin").exists()


def test_runner_rejects_mismatched_python_alias_before_control_or_codex(
    tmp_path: Path,
) -> None:
    runner, runtime_bin = _make_runner_tree(tmp_path)
    python_alias = runtime_bin / "python"
    python_alias.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    python_alias.chmod(0o755)
    calls_file = tmp_path / "codex-calls.jsonl"
    environment = _mock_environment(
        runner,
        runtime_bin,
        mode="forged",
        extra_environment={
            "MOCK_CODEX_CALLS_FILE": str(calls_file),
            "RETHLAS_HOTJOIN_RUN_ID": "mismatched-runtime-must-not-start",
        },
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 1
    assert "does not contain the selected interpreter bytes" in completed.stderr
    assert not calls_file.exists()
    assert not (runner.parents[2] / ".rethlas_hotjoin").exists()


@pytest.mark.parametrize("missing_module", REQUIRED_MODULES)
def test_runner_missing_runtime_module_starts_zero_codex_processes(
    tmp_path: Path,
    missing_module: str,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    shutil.rmtree(_module_stub(fake_bin, missing_module))
    calls_file = tmp_path / "codex-calls.jsonl"
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="forged",
        extra_environment={"MOCK_CODEX_CALLS_FILE": str(calls_file)},
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 1
    assert missing_module in completed.stderr
    assert "module not found" in completed.stderr
    assert not calls_file.exists()


def test_runner_broken_runtime_import_starts_zero_codex_processes(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    (_module_stub(fake_bin, "sympy") / "__init__.py").write_text(
        "raise RuntimeError('mock native import failure')\n",
        encoding="utf-8",
    )
    calls_file = tmp_path / "codex-calls.jsonl"
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="forged",
        extra_environment={"MOCK_CODEX_CALLS_FILE": str(calls_file)},
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 1
    assert (
        "sympy: import raised RuntimeError: mock native import failure"
        in completed.stderr
    )
    assert not calls_file.exists()


def test_runner_workspace_pth_entry_starts_zero_codex_processes(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    generation_root = runner.parent.parent
    workspace_package = generation_root / "sympy"
    workspace_package.mkdir()
    (workspace_package / "__init__.py").write_text("", encoding="utf-8")
    shutil.rmtree(_module_stub(fake_bin, "sympy"))
    (_site_packages(fake_bin) / "workspace-origin.pth").write_text(
        f"{generation_root}\n",
        encoding="utf-8",
    )
    calls_file = tmp_path / "codex-calls.jsonl"
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="forged",
        extra_environment={"MOCK_CODEX_CALLS_FILE": str(calls_file)},
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=generation_root,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 1
    assert ".pth path entry" in completed.stderr
    assert "model-writable generation workspace" in completed.stderr
    assert not calls_file.exists()


def test_runner_executable_pth_starts_zero_codex_processes(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    generation_root = runner.parent.parent
    workspace_package = generation_root / "sympy"
    workspace_package.mkdir()
    (workspace_package / "__init__.py").write_text("", encoding="utf-8")
    shutil.rmtree(_module_stub(fake_bin, "sympy"))
    site_packages = _site_packages(fake_bin)
    (site_packages / "workspace_editable_finder.py").write_text(
        """import importlib.abc
import importlib.util
import sys
from pathlib import Path

TARGET = Path({target!r})


class WorkspaceEditableFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname != "sympy":
            return None
        return importlib.util.spec_from_file_location(
            fullname,
            TARGET / "__init__.py",
            submodule_search_locations=[str(TARGET)],
        )


def install():
    sys.meta_path.insert(0, WorkspaceEditableFinder())
""".format(target=str(workspace_package)),
        encoding="utf-8",
    )
    (site_packages / "workspace-editable.pth").write_text(
        "import workspace_editable_finder; workspace_editable_finder.install()\n",
        encoding="utf-8",
    )
    calls_file = tmp_path / "codex-calls.jsonl"
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="forged",
        extra_environment={"MOCK_CODEX_CALLS_FILE": str(calls_file)},
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=generation_root,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 1
    assert "executable .pth line is forbidden" in completed.stderr
    assert not calls_file.exists()


def test_runner_workspace_editable_origin_starts_zero_codex_processes(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    generation_root = runner.parent.parent
    workspace_package = generation_root / "sympy"
    workspace_package.mkdir()
    (workspace_package / "__init__.py").write_text("", encoding="utf-8")
    shutil.rmtree(_module_stub(fake_bin, "sympy"))
    editable_packages = fake_bin.parent / "editable-packages"
    editable_packages.mkdir()
    (editable_packages / "sympy").symlink_to(
        workspace_package, target_is_directory=True
    )
    (_site_packages(fake_bin) / "workspace-editable-origin.pth").write_text(
        f"{editable_packages}\n",
        encoding="utf-8",
    )
    calls_file = tmp_path / "codex-calls.jsonl"
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="forged",
        extra_environment={"MOCK_CODEX_CALLS_FILE": str(calls_file)},
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=generation_root,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 1
    assert "sympy: unsafe module spec" in completed.stderr
    assert "model-writable generation workspace" in completed.stderr
    assert not calls_file.exists()


@pytest.mark.parametrize("package_kind", ("regular", "namespace"))
def test_runner_accepts_safe_external_required_package_locations(
    tmp_path: Path,
    package_kind: str,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    if package_kind == "namespace":
        (_module_stub(fake_bin, "sympy") / "__init__.py").unlink()
    environment = _mock_environment(runner, fake_bin, mode="trusted")

    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_runner_rejects_model_written_verified_file_without_receipt(
    tmp_path: Path,
) -> None:
    completed = _run_mock(tmp_path, mode="forged")
    assert completed.returncode == 1
    assert "without verified blueprint" in completed.stderr


def test_runner_stops_if_model_tampers_with_publisher_runtime(tmp_path: Path) -> None:
    completed = _run_mock(tmp_path, mode="tamper")
    assert completed.returncode == 70
    assert "runtime was modified" in completed.stderr


def test_runner_pins_mcp_restart_to_external_attested_snapshot(
    tmp_path: Path,
) -> None:
    completed = _run_mock(tmp_path, mode="transient_tamper")
    assert completed.returncode == 1, completed.stdout + completed.stderr
    generation_root = tmp_path / "agents" / "generation"
    snapshot_marker = generation_root / "snapshot_restart_checked"
    assert snapshot_marker.exists()
    snapshot_server = Path(snapshot_marker.read_text(encoding="utf-8"))
    assert snapshot_server.exists()
    assert not snapshot_server.is_relative_to(generation_root.resolve())
    assert "runtime was modified" not in completed.stderr


def test_secure_loader_rejects_mutate_restore_during_mcp_restart(
    tmp_path: Path,
) -> None:
    completed = _run_mock(tmp_path, mode="snapshot_restart_tamper")
    assert completed.returncode == 1, completed.stdout + completed.stderr
    generation_root = tmp_path / "agents" / "generation"
    checked = generation_root / "snapshot_restart_loader_checked"
    executed = generation_root / "snapshot_restart_payload_executed"
    assert checked.exists()
    snapshot_server = Path(checked.read_text(encoding="utf-8"))
    assert snapshot_server.exists()
    assert not snapshot_server.is_relative_to(generation_root.resolve())
    assert not executed.exists()


def test_runner_rejects_unchecked_hash_bytecode_before_codex_starts(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    generation_root = runner.parent.parent
    malicious_source = tmp_path / "malicious_verification_client.py"
    malicious_source.write_text(
        "MARKER = 'malicious bytecode loaded'\n", encoding="utf-8"
    )
    cache_dir = generation_root / "mcp" / "__pycache__"
    cache_dir.mkdir()
    bytecode_path = (
        cache_dir / f"verification_client.{sys.implementation.cache_tag}.pyc"
    )
    py_compile.compile(
        str(malicious_source),
        cfile=str(bytecode_path),
        doraise=True,
        invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
    )
    environment = _mock_environment(runner, fake_bin, mode="forged")

    completed = subprocess.run(
        [str(runner)],
        cwd=generation_root,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 1
    assert "Python bytecode cache directory is forbidden" in completed.stderr
    assert not (generation_root / "results").exists()


def test_runner_rejects_python_environment_inside_generation_workspace(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    generation_root = runner.parent.parent
    writable_venv = generation_root / ".venv"
    subprocess.run(
        [sys.executable, "-m", "venv", str(writable_venv)],
        check=True,
        capture_output=True,
        text=True,
    )
    environment = _mock_environment(runner, fake_bin, mode="forged")
    environment["PATH"] = (
        f"{writable_venv / 'bin'}{os.pathsep}"
        f"{fake_bin}{os.pathsep}{environment['PATH']}"
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=generation_root,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 1
    assert "Python environment must be outside" in completed.stderr
    assert not (generation_root / "results").exists()


def test_runner_rejects_problem_name_that_would_be_normalized(tmp_path: Path) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    (runner.parent.parent / "data" / "foo bar.md").write_text("S", encoding="utf-8")
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="forged",
        problem_file="data/foo bar.md",
    )
    completed = subprocess.run(
        [str(runner)],
        cwd=runner.parent.parent,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == 1
    assert "Unsupported problem path component" in completed.stderr


def test_runner_rejects_symlinked_problem_before_any_codex_call(
    tmp_path: Path,
) -> None:
    runner, fake_bin = _make_runner_tree(tmp_path)
    generation_root = runner.parent.parent
    outside = tmp_path / "external-problem.md"
    outside.write_text("external statement", encoding="utf-8")
    problem = generation_root / "data" / "example.md"
    problem.unlink()
    problem.symlink_to(outside)
    calls_file = tmp_path / "codex-calls.jsonl"
    environment = _mock_environment(
        runner,
        fake_bin,
        mode="forged",
        extra_environment={"MOCK_CODEX_CALLS_FILE": str(calls_file)},
    )

    completed = subprocess.run(
        [str(runner)],
        cwd=generation_root,
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 1
    assert "symlink component is forbidden" in completed.stderr
    assert not calls_file.exists()


def test_runner_forwards_explicit_https_endpoint_and_api_token(tmp_path: Path) -> None:
    completed = _run_mock(
        tmp_path,
        mode="trusted",
        extra_environment={
            "VERIFY_PROOF_URL": "https://verifier.example/verify",
            "VERIFY_API_TOKEN": "mock-secret-token",
            "MOCK_EXPECT_VERIFY_PROOF_URL": "https://verifier.example/verify",
            "MOCK_EXPECT_VERIFY_API_TOKEN": "mock-secret-token",
        },
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr

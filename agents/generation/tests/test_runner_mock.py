from __future__ import annotations

import json
import os
import py_compile
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


RUNNER = Path(__file__).with_name("run_example.sh")
GENERATION_ROOT = RUNNER.parents[1]
REQUIRED_MODULES = (
    "fastmcp",
    "requests",
    "numpy",
    "scipy",
    "sympy",
    "mpmath",
    "gmpy2",
)


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
        [sys.executable, "-m", "venv", "--without-pip", str(runtime)],
        check=True,
        capture_output=True,
        text=True,
    )
    runtime_bin = runtime / "bin"
    site_packages = _site_packages(runtime_bin)
    for module_name in REQUIRED_MODULES:
        package = site_packages / module_name
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
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
assert shutil.which("python", path=safe_path) == str(
    pathlib.Path(sys.executable).parent / "python"
)
assert shutil.which("python3", path=safe_path) == str(
    pathlib.Path(sys.executable).parent / "python3"
)
(pathlib.Path.cwd() / "shell_environment_policy_seen.json").write_text(
    json.dumps(shell_policy), encoding="utf-8"
)
reasoning_mcp_configs = [
    value
    for index, value in enumerate(sys.argv)
    if index > 0
    and sys.argv[index - 1] == "--config"
    and value.startswith("mcp_servers.reasoning_agent")
]
assert len(reasoning_mcp_configs) == 1
assert reasoning_mcp_configs[0].startswith("mcp_servers.reasoning_agent=")
reasoning_mcp = tomllib.loads(
    "value=" + reasoning_mcp_configs[0].split("=", 1)[1]
)["value"]
assert set(reasoning_mcp) == {
    "command",
    "args",
    "cwd",
    "env",
    "required",
    "tool_timeout_sec",
    "default_tools_approval_mode",
}
assert pathlib.Path(reasoning_mcp["command"]).is_absolute()
assert pathlib.Path(reasoning_mcp["command"]).resolve() == pathlib.Path(
    sys.executable
).resolve()
assert reasoning_mcp["args"][0] == "-B"
assert pathlib.Path(reasoning_mcp["args"][1]).is_absolute()
assert pathlib.Path(reasoning_mcp["args"][1]).name == "server.py"
assert pathlib.Path(reasoning_mcp["cwd"]).resolve() == pathlib.Path.cwd().resolve()
assert reasoning_mcp["tool_timeout_sec"] == 3600
assert reasoning_mcp["required"] is True
# The trusted MCP's memory_init/memory_append tools are writes. "approve" makes
# every tool on this server noninteractive; approval_policy=never cannot cancel
# the call while waiting for an unavailable prompt.
assert reasoning_mcp["default_tools_approval_mode"] == "approve"
assert "NumPy, SciPy, SymPy, mpmath, and gmpy2" in sys.argv[-1]
(pathlib.Path.cwd() / "reasoning_mcp_config_seen.json").write_text(
    json.dumps(reasoning_mcp), encoding="utf-8"
)
if os.environ.get("MOCK_EXPECT_VERIFY_PROOF_URL"):
    assert os.environ["VERIFY_PROOF_URL"] == os.environ["MOCK_EXPECT_VERIFY_PROOF_URL"]
if os.environ.get("MOCK_EXPECT_VERIFY_API_TOKEN"):
    assert os.environ["VERIFY_API_TOKEN"] == os.environ["MOCK_EXPECT_VERIFY_API_TOKEN"]
root = pathlib.Path.cwd()
problem_id = os.environ["RETHLAS_EXPECTED_PROBLEM_ID"]
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
        snapshot_args = reasoning_mcp["args"]
        assert snapshot_args[0] == "-B"
        snapshot_server = pathlib.Path(snapshot_args[1]).resolve()
        assert not snapshot_server.is_relative_to(root.resolve())
        assert snapshot_server.read_bytes() == original
        (root / "snapshot_restart_checked").write_text(
            str(snapshot_server), encoding="utf-8"
        )
    finally:
        source_server.write_bytes(original)
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


def test_runner_accepts_mock_atomic_publication_receipt(tmp_path: Path) -> None:
    completed = _run_mock(tmp_path, mode="trusted")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "Solved problem_id=example" in completed.stdout


def test_runner_injects_complete_mcp_and_auto_approves_memory_tools(
    tmp_path: Path,
) -> None:
    completed = _run_mock(tmp_path, mode="trusted")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    generation_root = tmp_path / "agents" / "generation"
    config = json.loads(
        (generation_root / "reasoning_mcp_config_seen.json").read_text(encoding="utf-8")
    )
    assert set(config) == {
        "command",
        "args",
        "cwd",
        "env",
        "required",
        "tool_timeout_sec",
        "default_tools_approval_mode",
    }
    assert config["default_tools_approval_mode"] == "approve"
    assert config["required"] is True
    assert config["tool_timeout_sec"] == 3600
    assert Path(config["args"][1]).is_absolute()
    assert not Path(config["args"][1]).is_relative_to(generation_root.resolve())


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

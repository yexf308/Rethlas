from __future__ import annotations

import os
import py_compile
import shutil
import subprocess
import sys
from pathlib import Path


RUNNER = Path(__file__).with_name("run_example.sh")
GENERATION_ROOT = RUNNER.parents[1]


def _make_runner_tree(tmp_path: Path) -> tuple[Path, Path]:
    generation = tmp_path / "agents" / "generation"
    tests_dir = generation / "tests"
    data_dir = generation / "data"
    tests_dir.mkdir(parents=True)
    data_dir.mkdir()
    shutil.copy2(RUNNER, tests_dir / "run_example.sh")
    shutil.copy2(GENERATION_ROOT / "AGENTS.md", generation / "AGENTS.md")
    shutil.copytree(GENERATION_ROOT / ".codex", generation / ".codex")
    shutil.copytree(GENERATION_ROOT / ".agents", generation / ".agents")
    shutil.copytree(
        GENERATION_ROOT / "mcp",
        generation / "mcp",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    (data_dir / "example.md").write_text("S", encoding="utf-8")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_codex = fake_bin / "codex"
    fake_codex.write_text(
        """#!/usr/bin/env python3
import hashlib
import json
import os
import pathlib
import sys

if "--version" in sys.argv:
    print("codex-mock 1.0")
    raise SystemExit(0)
assert "--dangerously-bypass-approvals-and-sandbox" not in sys.argv
assert sys.argv[sys.argv.index("--sandbox") + 1] == "workspace-write"
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
    from proof_context import aggregate_context_digest, parse_blueprint
    manifest = parse_blueprint(proof.decode("utf-8"), target_statement="S")
    receipt = pathlib.Path(os.environ["RETHLAS_RECEIPTS_ROOT"]) / f"{problem_id}.json"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps({
        "schema_version": "rethlas-publication-v1",
        "problem_id": problem_id,
        "statement_digest": os.environ["RETHLAS_EXPECTED_STATEMENT_SHA256"],
        "proof_digest": hashlib.sha256(proof).hexdigest(),
        "context_digest": aggregate_context_digest(manifest),
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
        overrides = [
            value.split("=", 1)[1]
            for index, value in enumerate(sys.argv)
            if index > 0
            and sys.argv[index - 1] == "--config"
            and value.startswith("mcp_servers.reasoning_agent.args=")
        ]
        assert len(overrides) == 1
        snapshot_args = json.loads(overrides[0])
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


def _run_mock(
    tmp_path: Path,
    *,
    mode: str,
    problem_file: str = "data/example.md",
    extra_environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    runner, fake_bin = _make_runner_tree(tmp_path)
    environment = dict(os.environ)
    environment.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
            "MAX_ITERATIONS": "1",
            "TIMER_INTERVAL_SECONDS": "1",
            "LOG_DIR": str(tmp_path / "logs"),
            "VERIFY_HEALTH_URL": "http://127.0.0.1:1/health",
            "MOCK_PUBLICATION": mode,
            "PROBLEM_FILE": problem_file,
        }
    )
    environment.update(extra_environment or {})
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
    malicious_source.write_text("MARKER = 'malicious bytecode loaded'\n", encoding="utf-8")
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
    environment = dict(os.environ)
    environment.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
            "MAX_ITERATIONS": "1",
            "TIMER_INTERVAL_SECONDS": "1",
            "LOG_DIR": str(tmp_path / "logs"),
            "VERIFY_HEALTH_URL": "http://127.0.0.1:1/health",
            "MOCK_PUBLICATION": "forged",
        }
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
    environment = dict(os.environ)
    environment.update(
        {
            "PATH": (
                f"{writable_venv / 'bin'}{os.pathsep}"
                f"{fake_bin}{os.pathsep}{environment['PATH']}"
            ),
            "MAX_ITERATIONS": "1",
            "TIMER_INTERVAL_SECONDS": "1",
            "LOG_DIR": str(tmp_path / "logs"),
            "VERIFY_HEALTH_URL": "http://127.0.0.1:1/health",
            "MOCK_PUBLICATION": "forged",
        }
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
    environment = dict(os.environ)
    environment.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
            "PROBLEM_FILE": "data/foo bar.md",
            "MAX_ITERATIONS": "1",
        }
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

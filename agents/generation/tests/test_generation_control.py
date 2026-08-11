from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from agents.generation.mcp import server


INSTANCE_A = "a" * 32
INSTANCE_B = "b" * 32


@pytest.fixture
def control_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, Path]:
    generation_root = tmp_path / "agents" / "generation"
    data_root = generation_root / "data"
    data_root.mkdir(parents=True)
    problem_path = data_root / "example.md"
    problem_path.write_text("Exact statement\n", encoding="utf-8")
    digest = hashlib.sha256(problem_path.read_bytes()).hexdigest()
    monkeypatch.setattr(server, "REPO_ROOT", generation_root)
    monkeypatch.setattr(server, "MEMORY_ROOT", generation_root / "memory")
    monkeypatch.setattr(server, "DATA_ROOT", data_root)
    monkeypatch.setattr(
        server,
        "GENERATION_CONTROL_ROOT",
        generation_root.parent / ".generation_control",
    )
    monkeypatch.setenv("RETHLAS_EXPECTED_PROBLEM_ID", "example")
    monkeypatch.setenv("RETHLAS_EXPECTED_STATEMENT_SHA256", digest)
    monkeypatch.setenv("RETHLAS_GENERATION_CONTROL_TOKEN", INSTANCE_A)
    return digest, problem_path


def _wait_evidence(problem_id: str, state: str) -> list[str]:
    if state == "waiting_cost_gate":
        event = {
            "event_type": "recursive_proving_round",
            "status": state,
        }
    else:
        event = {
            "event_type": "advisor_checkpoint",
            "status": state,
            "owner_action_required": True,
            "browser_dispatch_authorized": False,
            "advisor_request_id": None,
        }
    event_receipt = server.memory_append(problem_id, "events", event)
    branch_receipt = server.branch_update(problem_id, "root", {"status": state})
    return [event_receipt["record_id"], branch_receipt["record_id"]]


@pytest.mark.parametrize(
    "state", ("waiting_cost_gate", "waiting_owner_advisor_decision")
)
def test_generation_yield_binds_statement_instance_and_active_evidence(
    control_runtime: tuple[str, Path], state: str
) -> None:
    digest, _problem_path = control_runtime
    evidence = _wait_evidence("example", state)

    receipt = server.generation_yield(
        "example", state, "evidence-backed wait", evidence
    )

    assert receipt == server.generation_control_status("example", INSTANCE_A)
    assert receipt["schema"] == "rethlas_generation_control_v1"
    assert receipt["instance_id"] == INSTANCE_A
    assert receipt["statement_sha256"] == digest
    assert receipt["state"] == state
    assert receipt["evidence_record_ids"] == evidence
    path = server._generation_control_path("example", INSTANCE_A)
    assert path.parent == server.GENERATION_CONTROL_ROOT
    assert path.stat().st_mode & 0o777 == 0o600


def test_generation_control_instances_cannot_overwrite_each_other(
    control_runtime: tuple[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = _wait_evidence("example", "waiting_cost_gate")
    server.generation_yield(
        "example", "waiting_cost_gate", "instance A waits", evidence
    )
    server.generation_control_resume("example", INSTANCE_B)

    assert server.generation_control_status("example", INSTANCE_A)["state"] == (
        "waiting_cost_gate"
    )
    assert server.generation_control_status("example", INSTANCE_B)["state"] == (
        "running"
    )
    assert len(list(server.GENERATION_CONTROL_ROOT.glob("*.json"))) == 2


def test_generation_yield_rejects_phantom_or_mismatched_evidence_without_record(
    control_runtime: tuple[str, Path],
) -> None:
    with pytest.raises(ValueError, match="not active memory"):
        server.generation_yield(
            "example",
            "waiting_cost_gate",
            "no evidence",
            ["mem_phantom", "mem_phantom_two"],
        )

    event_only = server.memory_append(
        "example",
        "events",
        {"event_type": "recursive_proving_round", "status": "waiting_cost_gate"},
    )
    wrong_branch = server.branch_update("example", "root", {"status": "running"})
    with pytest.raises(ValueError, match="exact wait status"):
        server.generation_yield(
            "example",
            "waiting_cost_gate",
            "wrong branch state",
            [event_only["record_id"], wrong_branch["record_id"]],
        )
    assert not server._generation_control_path("example", INSTANCE_A).exists()


def test_status_fails_closed_after_evidence_is_superseded_or_statement_changes(
    control_runtime: tuple[str, Path],
) -> None:
    _digest, problem_path = control_runtime
    evidence = _wait_evidence("example", "waiting_owner_advisor_decision")
    server.generation_yield(
        "example",
        "waiting_owner_advisor_decision",
        "wait for owner",
        evidence,
    )
    server.memory_append(
        "example",
        "branch_states",
        {"branch_id": "root", "state": {"status": "running"}},
        supersedes=[evidence[1]],
    )
    with pytest.raises(ValueError, match="not active memory"):
        server.generation_control_status("example", INSTANCE_A)

    # Restore a fresh valid wait, then prove source mutation is independently
    # rejected before the runner can accept the legal-yield exit.
    evidence = _wait_evidence("example", "waiting_owner_advisor_decision")
    server.generation_yield(
        "example",
        "waiting_owner_advisor_decision",
        "wait for owner again",
        evidence,
    )
    problem_path.write_text("Changed statement\n", encoding="utf-8")
    with pytest.raises(ValueError, match="changed after the runner bound"):
        server.generation_control_status("example", INSTANCE_A)


def test_generation_control_rejects_symlink_root(
    control_runtime: tuple[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "control-link"
    link.symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(server, "GENERATION_CONTROL_ROOT", link)

    with pytest.raises(ValueError, match="real owner-only directory"):
        server.generation_control_resume("example", INSTANCE_A)


def test_generation_yield_requires_runner_bound_instance(
    control_runtime: tuple[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RETHLAS_GENERATION_CONTROL_TOKEN")
    evidence = _wait_evidence("example", "waiting_cost_gate")
    with pytest.raises(ValueError, match="32 lowercase hex"):
        server.generation_yield(
            "example", "waiting_cost_gate", "missing instance", evidence
        )


def test_generation_control_post_replace_fsync_failure_is_retry_safe(
    control_runtime: tuple[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    original_fsync = server.os.fsync
    calls = 0

    def fail_directory_fsync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected directory fsync failure")
        original_fsync(descriptor)

    with monkeypatch.context() as scoped:
        scoped.setattr(server.os, "fsync", fail_directory_fsync)
        with pytest.raises(OSError, match="directory fsync failure"):
            server.generation_control_resume("example", INSTANCE_A)

    # The replace may already be visible. Its deterministic desired state can
    # be read safely and an exact retry cannot create a second logical record.
    assert server.generation_control_status("example", INSTANCE_A)["state"] == (
        "running"
    )
    retry = server.generation_control_resume("example", INSTANCE_A)
    assert retry["state"] == "running"
    assert len(list(server.GENERATION_CONTROL_ROOT.glob("*.json"))) == 1


def test_generation_control_status_rejects_tampered_envelope(
    control_runtime: tuple[str, Path],
) -> None:
    server.generation_control_resume("example", INSTANCE_A)
    path = server._generation_control_path("example", INSTANCE_A)
    path.write_text("{}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid envelope"):
        server.generation_control_status("example", INSTANCE_A)


def test_generation_control_status_rejects_duplicate_json_keys(
    control_runtime: tuple[str, Path],
) -> None:
    server.generation_control_resume("example", INSTANCE_A)
    path = server._generation_control_path("example", INSTANCE_A)
    raw = path.read_text(encoding="utf-8")
    tampered = raw.replace(
        '"schema":"rethlas_generation_control_v1"',
        '"schema":"wrong","schema":"rethlas_generation_control_v1"',
    )
    assert tampered != raw
    path.write_text(tampered, encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate JSON key 'schema'"):
        server.generation_control_status("example", INSTANCE_A)

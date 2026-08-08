from __future__ import annotations

import json
import threading
import sys
from pathlib import Path
from typing import Any, Callable

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agents.generation.mcp import verification_client as client  # noqa: E402
from agents.generation.mcp import server as generation_server  # noqa: E402


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self.payload


def valid_payload(
    proof: str,
    *,
    statement: str = "S",
    verdict: str = "correct",
) -> dict[str, Any]:
    wrong = verdict == "wrong"
    item_ids, context_digest = client.expected_attestation(
        proof=proof,
        statement=statement,
    )
    manifest = client.parse_blueprint(proof, target_statement=statement)
    attestations = []
    for index, item_id in enumerate(item_ids):
        context = client.build_item_context(
            manifest,
            item_id,
            max_chars=client.VERIFY_CONTEXT_MAX_CHARS,
        )
        attestations.append(
            {
                "item_id": item_id,
                "disposition": (
                    "verified" if not wrong or index == 0 else "blocked"
                ),
                "final_round": 0,
                "expanded_proof_ids": [],
                "max_chars": client.VERIFY_CONTEXT_MAX_CHARS,
                "context_digest": context["digest"],
                "verdict": verdict,
            }
        )
    payload = {
        "output_schema_version": 2,
        "verification_report": {
            "summary": "checked",
            "critical_errors": [],
            "gaps": (
                [{"location": item_ids[0], "issue": "missing justification"}]
                if wrong
                else []
            ),
        },
        "verification_status": "final",
        "verdict": verdict,
        "repair_hints": "add the missing justification" if wrong else "",
        "needs_expanded_proofs": [],
        "checked_item_ids": item_ids,
        "proof_digest": client.proof_digest(proof),
        "context_digest": context_digest,
        "item_context_attestations": attestations,
    }
    payload["adaptive_context_digest"] = client.aggregate_adaptive_context_digest(
        manifest, attestations
    )
    return payload


def install_post(
    monkeypatch: pytest.MonkeyPatch,
    factory: Callable[[str, dict[str, Any]], object],
) -> None:
    def fake_post(
        endpoint: str,
        *,
        json: dict[str, Any],
        timeout: int,
        **kwargs: Any,
    ) -> FakeResponse:
        assert timeout == 3600
        return FakeResponse(factory(endpoint, json))

    monkeypatch.setattr(client.requests, "post", fake_post)


def test_correct_response_promotes_unchanged_draft_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "# theorem main\n\n## statement\nS\n\n## proof\nP\n"
    draft = tmp_path / "blueprint.md"
    verified = tmp_path / "blueprint_verified.md"
    draft.write_text(proof, encoding="utf-8")
    install_post(
        monkeypatch,
        lambda endpoint, request: valid_payload(request["proof"]),
    )

    result = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
    )

    assert result["published"] is True
    assert result["published_path"] == str(verified)
    assert verified.read_text(encoding="utf-8") == proof
    assert draft.read_text(encoding="utf-8") == proof
    assert "proof" not in result


def test_wrong_response_never_promotes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "candidate proof"
    draft = tmp_path / "blueprint.md"
    verified = tmp_path / "blueprint_verified.md"
    draft.write_text(proof, encoding="utf-8")
    install_post(
        monkeypatch,
        lambda endpoint, request: valid_payload(request["proof"], verdict="wrong"),
    )

    result = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
    )

    assert result["published"] is False
    assert draft.exists()
    assert not verified.exists()


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://verifier.example/verify",
        "http://127.0.0.1:8000/verify",
        "http://localhost:8000/verify",
        "http://[::1]:8000/verify",
    ],
)
def test_https_and_explicit_loopback_http_endpoints_are_accepted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
) -> None:
    proof = "candidate proof"
    draft = tmp_path / "blueprint.md"
    verified = tmp_path / "blueprint_verified.md"
    draft.write_text(proof, encoding="utf-8")
    install_post(
        monkeypatch,
        lambda actual_endpoint, request: valid_payload(request["proof"]),
    )

    result = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint=endpoint,
    )

    assert result["published"] is True


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://verifier.example/verify",
        "http://192.0.2.1/verify",
        "ftp://verifier.example/verify",
    ],
)
def test_remote_plaintext_and_non_http_endpoints_are_rejected_before_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
) -> None:
    monkeypatch.setattr(
        client.requests,
        "post",
        lambda *args, **kwargs: pytest.fail("network must not be called"),
    )

    with pytest.raises(ValueError, match="HTTPS or HTTP"):
        client.verify_blueprint_file(
            statement="S",
            draft_path=tmp_path / "missing.md",
            verified_path=tmp_path / "blueprint_verified.md",
            endpoint=endpoint,
        )


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://user:password@verifier.example/verify",
        "http://user@localhost:8000/verify",
    ],
)
def test_endpoint_userinfo_is_rejected_before_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
) -> None:
    monkeypatch.setattr(
        client.requests,
        "post",
        lambda *args, **kwargs: pytest.fail("network must not be called"),
    )

    with pytest.raises(ValueError, match="userinfo"):
        client.verify_blueprint_file(
            statement="S",
            draft_path=tmp_path / "missing.md",
            verified_path=tmp_path / "blueprint_verified.md",
            endpoint=endpoint,
        )


def test_digest_mismatch_is_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft = tmp_path / "blueprint.md"
    draft.write_text("proof A", encoding="utf-8")
    payload = valid_payload("proof A")
    payload["proof_digest"] = client.proof_digest("different proof")
    install_post(monkeypatch, lambda endpoint, request: payload)

    with pytest.raises(ValueError, match="proof_digest"):
        client.verify_blueprint_file(
            statement="S",
            draft_path=draft,
            verified_path=tmp_path / "blueprint_verified.md",
            endpoint="https://verifier/verify",
        )
    assert draft.exists()


def test_draft_change_during_verification_is_not_published(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = "proof A"
    draft = tmp_path / "blueprint.md"
    verified = tmp_path / "blueprint_verified.md"
    draft.write_text(original, encoding="utf-8")

    def mutate_during_post(endpoint: str, request: dict[str, Any]) -> object:
        draft.write_text("proof B", encoding="utf-8")
        return valid_payload(request["proof"])

    install_post(monkeypatch, mutate_during_post)

    with pytest.raises(ValueError, match="changed during verification"):
        client.verify_blueprint_file(
            statement="S",
            draft_path=draft,
            verified_path=verified,
            endpoint="https://verifier/verify",
        )
    assert draft.read_text(encoding="utf-8") == "proof B"
    assert not verified.exists()


def test_correct_with_findings_is_rejected_before_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "candidate proof"
    draft = tmp_path / "blueprint.md"
    draft.write_text(proof, encoding="utf-8")
    payload = valid_payload(proof)
    payload["verification_report"]["gaps"] = [
        {"location": "item-1", "issue": "gap"}
    ]
    install_post(monkeypatch, lambda endpoint, request: payload)

    with pytest.raises(ValueError, match="correct verdict"):
        client.verify_blueprint_file(
            statement="S",
            draft_path=draft,
            verified_path=tmp_path / "blueprint_verified.md",
            endpoint="https://verifier/verify",
        )


def test_empty_coverage_is_rejected_before_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "candidate proof"
    draft = tmp_path / "blueprint.md"
    draft.write_text(proof, encoding="utf-8")
    payload = valid_payload(proof)
    payload["checked_item_ids"] = []
    install_post(monkeypatch, lambda endpoint, request: payload)

    with pytest.raises(ValueError, match="exactly match"):
        client.verify_blueprint_file(
            statement="S",
            draft_path=draft,
            verified_path=tmp_path / "blueprint_verified.md",
            endpoint="https://verifier/verify",
        )


def test_spoofed_same_count_ids_and_context_digest_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "candidate proof"
    draft = tmp_path / "blueprint.md"
    draft.write_text(proof, encoding="utf-8")
    payload = valid_payload(proof)
    payload["checked_item_ids"] = ["pi_" + "0" * 24]
    payload["context_digest"] = "0" * 64
    install_post(monkeypatch, lambda endpoint, request: payload)

    with pytest.raises(ValueError, match="exactly match"):
        client.verify_blueprint_file(
            statement="S",
            draft_path=draft,
            verified_path=tmp_path / "blueprint_verified.md",
            endpoint="https://verifier/verify",
        )


@pytest.mark.parametrize("field", ["item_context", "adaptive_digest"])
def test_spoofed_adaptive_context_attestation_is_rejected_before_publish(
    field: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "# theorem main\n\n## statement\nS\n\n## proof\nP\n"
    draft = tmp_path / "blueprint.md"
    verified = tmp_path / "blueprint_verified.md"
    draft.write_text(proof, encoding="utf-8")
    payload = valid_payload(proof)
    if field == "item_context":
        payload["item_context_attestations"][0]["context_digest"] = "0" * 64
    else:
        payload["adaptive_context_digest"] = "0" * 64
    install_post(monkeypatch, lambda endpoint, request: payload)

    with pytest.raises(ValueError, match="context"):
        client.verify_blueprint_file(
            statement="S",
            draft_path=draft,
            verified_path=verified,
            endpoint="https://verifier/verify",
        )

    assert not verified.exists()


def test_non_cooperating_draft_write_cannot_change_published_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = "verified bytes"
    draft = tmp_path / "blueprint.md"
    verified = tmp_path / "blueprint_verified.md"
    draft.write_text(original, encoding="utf-8")
    install_post(
        monkeypatch,
        lambda endpoint, request: valid_payload(request["proof"]),
    )
    real_replace = client.os.replace

    def mutate_then_replace(
        source: str | Path,
        target: str | Path,
        **kwargs: Any,
    ) -> None:
        if Path(target).name == verified.name:
            draft.write_text("unverified bytes", encoding="utf-8")
        real_replace(source, target, **kwargs)

    monkeypatch.setattr(client.os, "replace", mutate_then_replace)

    result = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
    )

    assert result["published"] is True
    assert verified.read_text(encoding="utf-8") == original
    assert draft.read_text(encoding="utf-8") == "unverified bytes"


def test_verified_symlink_is_replaced_with_captured_regular_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "verified bytes"
    draft = tmp_path / "blueprint.md"
    backing = tmp_path / "attacker-controlled.md"
    verified = tmp_path / "blueprint_verified.md"
    draft.write_text(proof, encoding="utf-8")
    backing.write_text(proof, encoding="utf-8")
    verified.symlink_to(backing)
    install_post(
        monkeypatch,
        lambda endpoint, request: valid_payload(request["proof"]),
    )

    result = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
    )

    assert result["published"] is True
    assert not verified.is_symlink()
    backing.write_text("unverified bytes", encoding="utf-8")
    assert verified.read_text(encoding="utf-8") == proof


def test_parent_swap_during_verification_cannot_redirect_publish_or_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proof = "verified bytes"
    draft_parent = tmp_path / "drafts"
    draft_parent.mkdir()
    working_parent = tmp_path / "results" / "problem"
    working_parent.mkdir(parents=True)
    detached_parent = tmp_path / "detached-problem"
    attacker_parent = tmp_path / "attacker-controlled"
    attacker_parent.mkdir()
    draft = draft_parent / "blueprint.md"
    verified = working_parent / "blueprint_verified.md"
    receipt = tmp_path / "receipts" / "problem.json"
    draft.write_text(proof, encoding="utf-8")

    def swap_parent_during_post(endpoint: str, request: dict[str, Any]) -> object:
        working_parent.rename(detached_parent)
        working_parent.symlink_to(attacker_parent, target_is_directory=True)
        return valid_payload(request["proof"])

    install_post(monkeypatch, swap_parent_during_post)

    with pytest.raises(ValueError, match="parent changed during verification"):
        client.verify_blueprint_file(
            statement="S",
            draft_path=draft,
            verified_path=verified,
            receipt_path=receipt,
            problem_id="problem",
            endpoint="https://verifier/verify",
        )

    assert not (attacker_parent / verified.name).exists()
    assert not (detached_parent / verified.name).exists()
    assert not receipt.exists()


def test_receipt_parent_symlink_is_rejected_without_writing_target(
    tmp_path: Path,
) -> None:
    attacker_parent = tmp_path / "attacker-controlled"
    attacker_parent.mkdir()
    receipt_parent = tmp_path / "receipts"
    receipt_parent.symlink_to(attacker_parent, target_is_directory=True)

    with pytest.raises(ValueError, match="receipt parent"):
        client._write_receipt_atomic(receipt_parent / "problem.json", {"ok": True})

    assert not (attacker_parent / "problem.json").exists()


def test_receipt_target_symlink_is_rejected_without_touching_backing_file(
    tmp_path: Path,
) -> None:
    receipt_parent = tmp_path / "receipts"
    receipt_parent.mkdir()
    backing = tmp_path / "attacker-controlled.json"
    backing.write_text("unchanged", encoding="utf-8")
    receipt = receipt_parent / "problem.json"
    receipt.symlink_to(backing)

    with pytest.raises(ValueError, match="receipt target must not be a symlink"):
        client._write_receipt_atomic(receipt, {"ok": True})

    assert receipt.is_symlink()
    assert backing.read_text(encoding="utf-8") == "unchanged"


def test_draft_symlink_is_rejected_before_network_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backing = tmp_path / "backing.md"
    backing.write_text("proof", encoding="utf-8")
    draft = tmp_path / "blueprint.md"
    draft.symlink_to(backing)
    monkeypatch.setattr(
        client.requests,
        "post",
        lambda *args, **kwargs: pytest.fail("network must not be called"),
    )

    with pytest.raises(ValueError, match="regular file"):
        client.verify_blueprint_file(
            statement="S",
            draft_path=draft,
            verified_path=tmp_path / "blueprint_verified.md",
            endpoint="https://verifier/verify",
        )


def test_oversized_draft_is_rejected_before_read_or_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft = tmp_path / "blueprint.md"
    draft.write_bytes(b"x" * 17)
    monkeypatch.setattr(client, "MAX_BLUEPRINT_BYTES", 16)
    monkeypatch.setattr(
        client.requests,
        "post",
        lambda *args, **kwargs: pytest.fail("network must not be called"),
    )

    with pytest.raises(ValueError, match="VERIFY_MAX_PROOF_BYTES"):
        client.verify_blueprint_file(
            statement="S",
            draft_path=draft,
            verified_path=tmp_path / "blueprint_verified.md",
            endpoint="https://verifier/verify",
        )


def test_crlf_bytes_are_hashed_and_published_without_normalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = b"# theorem main\r\n\r\n## statement\r\nS\r\n\r\n## proof\r\nP\r\n"
    draft = tmp_path / "blueprint.md"
    verified = tmp_path / "blueprint_verified.md"
    draft.write_bytes(raw)
    install_post(
        monkeypatch,
        lambda endpoint, request: valid_payload(request["proof"]),
    )

    result = client.verify_blueprint_file(
        statement="S",
        draft_path=draft,
        verified_path=verified,
        endpoint="https://verifier/verify",
    )

    assert result["published"] is True
    assert verified.read_bytes() == raw
    assert result["proof_digest"] == client.proof_digest(raw.decode("utf-8"))


def test_same_verified_target_uses_one_cross_draft_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft_a = tmp_path / "a" / "blueprint.md"
    draft_b = tmp_path / "b" / "blueprint.md"
    verified = tmp_path / "published" / "blueprint_verified.md"
    draft_a.parent.mkdir()
    draft_b.parent.mkdir()
    draft_a.write_text("proof A", encoding="utf-8")
    draft_b.write_text("proof B", encoding="utf-8")
    barrier = threading.Barrier(2)

    def synchronized_response(endpoint: str, request: dict[str, Any]) -> object:
        barrier.wait(timeout=5)
        return valid_payload(request["proof"])

    install_post(monkeypatch, synchronized_response)
    successes: list[dict[str, Any]] = []
    failures: list[Exception] = []

    def publish(draft: Path) -> None:
        try:
            successes.append(
                client.verify_blueprint_file(
                    statement="S",
                    draft_path=draft,
                    verified_path=verified,
                    endpoint="https://verifier/verify",
                )
            )
        except Exception as exc:  # noqa: BLE001 - asserted below
            failures.append(exc)

    threads = [
        threading.Thread(target=publish, args=(draft_a,)),
        threading.Thread(target=publish, args=(draft_b,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(successes) == 1
    assert len(failures) == 1
    assert "different verified blueprint" in str(failures[0])
    assert verified.read_text(encoding="utf-8") in {"proof A", "proof B"}


def test_mcp_production_wrapper_uses_problem_id_and_publishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results_root = tmp_path / "results"
    result_dir = results_root / "category" / "problem"
    result_dir.mkdir(parents=True)
    draft = result_dir / "blueprint.md"
    draft.write_text("candidate proof", encoding="utf-8")
    data_root = tmp_path / "data"
    problem_source = data_root / "category" / "problem.md"
    problem_source.parent.mkdir(parents=True)
    problem_source.write_text("S", encoding="utf-8")
    receipts_root = tmp_path / "trusted-receipts"
    monkeypatch.setattr(generation_server, "RESULTS_ROOT", results_root)
    monkeypatch.setattr(generation_server, "DATA_ROOT", data_root)
    monkeypatch.setattr(generation_server, "RECEIPTS_ROOT", receipts_root)
    monkeypatch.delenv("RETHLAS_EXPECTED_PROBLEM_ID", raising=False)
    monkeypatch.delenv("RETHLAS_EXPECTED_STATEMENT_SHA256", raising=False)
    install_post(
        monkeypatch,
        lambda endpoint, request: valid_payload(request["proof"]),
    )

    result = generation_server.verify_blueprint_service(
        problem_id="category/problem",
        endpoint="https://verifier/verify",
    )

    assert result["published"] is True
    assert (result_dir / "blueprint_verified.md").read_text(encoding="utf-8") == (
        "candidate proof"
    )
    receipt = receipts_root / "category" / "problem.json"
    assert result["publication_receipt_path"] == str(receipt)
    assert receipt.exists()
    assert json.loads(receipt.read_text(encoding="utf-8"))["proof_digest"] == (
        client.proof_digest("candidate proof")
    )


@pytest.mark.parametrize("replace_results_root", [False, True])
def test_mcp_wrapper_rejects_symlinks_at_every_results_path_component(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replace_results_root: bool,
) -> None:
    generation_root = tmp_path / "generation"
    results_root = generation_root / "results"
    outside_root = tmp_path / "outside-results"
    outside_problem = outside_root / "category" / "problem"
    outside_problem.mkdir(parents=True)
    (outside_problem / "blueprint.md").write_text(
        "outside candidate proof",
        encoding="utf-8",
    )
    if replace_results_root:
        generation_root.mkdir()
        results_root.symlink_to(outside_root, target_is_directory=True)
    else:
        results_root.mkdir(parents=True)
        (results_root / "category").symlink_to(
            outside_root / "category",
            target_is_directory=True,
        )

    data_root = generation_root / "data"
    problem_source = data_root / "category" / "problem.md"
    problem_source.parent.mkdir(parents=True)
    problem_source.write_text("S", encoding="utf-8")
    receipts_root = tmp_path / "trusted-receipts"
    monkeypatch.setattr(generation_server, "RESULTS_ROOT", results_root)
    monkeypatch.setattr(generation_server, "DATA_ROOT", data_root)
    monkeypatch.setattr(generation_server, "RECEIPTS_ROOT", receipts_root)
    monkeypatch.delenv("RETHLAS_EXPECTED_PROBLEM_ID", raising=False)
    monkeypatch.delenv("RETHLAS_EXPECTED_STATEMENT_SHA256", raising=False)
    monkeypatch.setattr(
        client.requests,
        "post",
        lambda *args, **kwargs: pytest.fail("network must not be called"),
    )

    with pytest.raises(ValueError, match="non-symlink|non-symlink directories"):
        generation_server.verify_blueprint_service(
            problem_id="category/problem",
            endpoint="https://verifier/verify",
        )

    assert not (outside_problem / "blueprint_verified.md").exists()
    assert not (receipts_root / "category" / "problem.json").exists()


def test_mcp_wrapper_rejects_problem_changed_after_runner_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = tmp_path / "data"
    data_root.mkdir()
    (data_root / "problem.md").write_text("changed target", encoding="utf-8")
    monkeypatch.setattr(generation_server, "DATA_ROOT", data_root)
    monkeypatch.setenv("RETHLAS_EXPECTED_PROBLEM_ID", "problem")
    monkeypatch.setenv("RETHLAS_EXPECTED_STATEMENT_SHA256", "0" * 64)
    monkeypatch.setattr(
        client.requests,
        "post",
        lambda *args, **kwargs: pytest.fail("network must not be called"),
    )

    with pytest.raises(ValueError, match="changed after the runner bound"):
        generation_server.verify_blueprint_service(problem_id="problem")


def test_mcp_production_wrapper_rejects_parent_traversal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(generation_server, "RESULTS_ROOT", tmp_path / "results")
    with pytest.raises(ValueError, match=r"\.\."):
        generation_server.verify_blueprint_service(
            problem_id="../../outside",
        )

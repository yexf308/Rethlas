from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sys

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY_ROOT))

from agents.verification.api.contracts import (  # noqa: E402
    build_verification_output,
    validate_verification_output,
)


EXPECTED_IDS = ["statement:1", "statement:2"]
PROOF_DIGEST = "proof-sha256"
CONTEXT_DIGEST = "context-sha256"


def correct_payload() -> dict[str, object]:
    return {
        "verification_report": {
            "summary": "Every checked step is justified.",
            "critical_errors": [],
            "gaps": [],
        },
        "verdict": "correct",
        "repair_hints": "",
        "checked_item_ids": EXPECTED_IDS.copy(),
        "proof_digest": PROOF_DIGEST,
        "context_digest": CONTEXT_DIGEST,
    }


def wrong_payload() -> dict[str, object]:
    return {
        "verification_report": {
            "summary": "The final implication is unsupported.",
            "critical_errors": [],
            "gaps": [
                {
                    "location": "statement:2",
                    "issue": "The conclusion does not follow from the cited lemma.",
                }
            ],
        },
        "verdict": "wrong",
        "repair_hints": "Supply the missing implication after statement:2.",
        "checked_item_ids": EXPECTED_IDS.copy(),
        "proof_digest": PROOF_DIGEST,
        "context_digest": CONTEXT_DIGEST,
    }


def validate(payload: dict[str, object]) -> dict[str, object]:
    return validate_verification_output(
        payload,
        expected_checked_item_ids=EXPECTED_IDS,
        expected_proof_digest=PROOF_DIGEST,
        expected_context_digest=CONTEXT_DIGEST,
    )


def test_builds_happy_correct_output() -> None:
    output = build_verification_output(
        verification_report=correct_payload()["verification_report"],
        repair_hints="",
        checked_item_ids=EXPECTED_IDS,
        proof_digest=PROOF_DIGEST,
        context_digest=CONTEXT_DIGEST,
    )

    assert output == correct_payload()


def test_builds_happy_wrong_output() -> None:
    source = wrong_payload()
    output = build_verification_output(
        verification_report=source["verification_report"],
        repair_hints=source["repair_hints"],
        checked_item_ids=EXPECTED_IDS,
        proof_digest=PROOF_DIGEST,
        context_digest=CONTEXT_DIGEST,
    )

    assert output == source


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("verification_report", [], "verification_report must be an object"),
        ("verdict", 1, "verdict must be a string"),
        ("repair_hints", [], "repair_hints must be a string"),
        ("checked_item_ids", "statement:1", "checked_item_ids must be a list"),
        ("proof_digest", 1, "proof_digest must be a string"),
        ("proof_digest", "", "proof_digest must be non-empty"),
        ("context_digest", 1, "context_digest must be a string"),
        ("context_digest", "", "context_digest must be non-empty"),
    ],
)
def test_rejects_malformed_top_level_fields(
    field: str, value: object, message: str
) -> None:
    payload = correct_payload()
    payload[field] = value

    with pytest.raises(ValueError, match=f"^{message}$"):
        validate(payload)


@pytest.mark.parametrize(
    "field",
    [
        "verification_report",
        "verdict",
        "repair_hints",
        "checked_item_ids",
        "proof_digest",
        "context_digest",
    ],
)
def test_rejects_every_missing_top_level_field(field: str) -> None:
    payload = correct_payload()
    del payload[field]

    with pytest.raises(ValueError, match="missing required properties"):
        validate(payload)


def test_rejects_extra_property_at_every_object_level() -> None:
    top_extra = correct_payload()
    top_extra["extra"] = True
    with pytest.raises(ValueError, match="unexpected properties: 'extra'"):
        validate(top_extra)

    report_extra = correct_payload()
    report = report_extra["verification_report"]
    assert isinstance(report, dict)
    report["notes"] = "not in the contract"
    with pytest.raises(ValueError, match="unexpected properties: 'notes'"):
        validate(report_extra)

    finding_extra = wrong_payload()
    finding_report = finding_extra["verification_report"]
    assert isinstance(finding_report, dict)
    gaps = finding_report["gaps"]
    assert isinstance(gaps, list)
    gaps[0]["severity"] = "high"
    with pytest.raises(ValueError, match="unexpected properties: 'severity'"):
        validate(finding_extra)


def test_rejects_bad_report_and_finding_shapes() -> None:
    missing_report_field = correct_payload()
    report = missing_report_field["verification_report"]
    assert isinstance(report, dict)
    del report["summary"]
    with pytest.raises(ValueError, match="missing required properties: 'summary'"):
        validate(missing_report_field)

    bad_summary = correct_payload()
    report = bad_summary["verification_report"]
    assert isinstance(report, dict)
    report["summary"] = None
    with pytest.raises(ValueError, match="summary must be a string"):
        validate(bad_summary)

    bad_findings = correct_payload()
    report = bad_findings["verification_report"]
    assert isinstance(report, dict)
    report["critical_errors"] = {}
    with pytest.raises(ValueError, match="critical_errors must be a list"):
        validate(bad_findings)

    bad_finding = wrong_payload()
    report = bad_finding["verification_report"]
    assert isinstance(report, dict)
    report["gaps"] = ["not an object"]
    with pytest.raises(ValueError, match=r"gaps\[0\] must be an object"):
        validate(bad_finding)


@pytest.mark.parametrize("field", ["location", "issue"])
@pytest.mark.parametrize("value", ["", 3])
def test_rejects_empty_or_non_string_finding_fields(
    field: str, value: object
) -> None:
    payload = wrong_payload()
    report = payload["verification_report"]
    assert isinstance(report, dict)
    gaps = report["gaps"]
    assert isinstance(gaps, list)
    gaps[0][field] = value

    expected = "must be non-empty" if value == "" else "must be a string"
    with pytest.raises(ValueError, match=expected):
        validate(payload)


@pytest.mark.parametrize(
    ("checked_ids", "message"),
    [
        (["statement:1", "statement:1"], "contains duplicate item"),
        (["statement:1"], "must exactly match"),
        (["statement:2", "statement:1"], "must exactly match"),
        (["statement:1", "statement:2", "statement:3"], "must exactly match"),
        (["statement:1", ""], r"checked_item_ids\[1\] must be non-empty"),
        (["statement:1", 2], r"checked_item_ids\[1\] must be a string"),
    ],
)
def test_rejects_invalid_or_nonmatching_checked_item_ids(
    checked_ids: list[object], message: str
) -> None:
    payload = correct_payload()
    payload["checked_item_ids"] = checked_ids

    with pytest.raises(ValueError, match=message):
        validate(payload)


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("proof_digest", "proof_digest does not match expected_proof_digest"),
        ("context_digest", "context_digest does not match expected_context_digest"),
    ],
)
def test_rejects_digest_mismatch(field: str, message: str) -> None:
    payload = correct_payload()
    payload[field] = "different-digest"

    with pytest.raises(ValueError, match=f"^{message}$"):
        validate(payload)


def test_rejects_unknown_verdict() -> None:
    payload = correct_payload()
    payload["verdict"] = "uncertain"

    with pytest.raises(ValueError, match="verdict must be 'correct' or 'wrong'"):
        validate(payload)


def test_rejects_correct_verdict_with_findings() -> None:
    payload = wrong_payload()
    payload["verdict"] = "correct"
    payload["repair_hints"] = ""

    with pytest.raises(ValueError, match="verdict must be 'wrong'"):
        validate(payload)


def test_rejects_correct_verdict_with_repair_hints() -> None:
    payload = correct_payload()
    payload["repair_hints"] = "No repair is actually needed."

    with pytest.raises(ValueError, match="repair_hints must be empty"):
        validate(payload)


def test_rejects_wrong_verdict_without_findings() -> None:
    payload = correct_payload()
    payload["verdict"] = "wrong"
    payload["repair_hints"] = "Change something."

    with pytest.raises(ValueError, match="verdict must be 'correct'"):
        validate(payload)


@pytest.mark.parametrize("repair_hints", ["", "   ", "\n\t"])
def test_rejects_wrong_verdict_with_blank_repair_hints(repair_hints: str) -> None:
    payload = wrong_payload()
    payload["repair_hints"] = repair_hints

    with pytest.raises(ValueError, match="non-whitespace text"):
        validate(payload)


def test_validation_does_not_mutate_input_and_returns_detached_copy() -> None:
    payload = wrong_payload()
    before = deepcopy(payload)

    validated = validate(payload)

    assert payload == before
    assert validated == payload
    assert validated is not payload
    validated_report = validated["verification_report"]
    assert isinstance(validated_report, dict)
    validated_report["summary"] = "changed"
    assert payload == before


def test_build_does_not_mutate_or_alias_inputs() -> None:
    source = wrong_payload()
    report = source["verification_report"]
    repair_hints = source["repair_hints"]
    assert isinstance(report, dict)
    assert isinstance(repair_hints, str)
    report_before = deepcopy(report)
    ids_before = EXPECTED_IDS.copy()

    output = build_verification_output(
        verification_report=report,
        repair_hints=repair_hints,
        checked_item_ids=EXPECTED_IDS,
        proof_digest=PROOF_DIGEST,
        context_digest=CONTEXT_DIGEST,
    )

    assert report == report_before
    assert EXPECTED_IDS == ids_before
    output_report = output["verification_report"]
    assert isinstance(output_report, dict)
    output_report["summary"] = "changed"
    output_ids = output["checked_item_ids"]
    assert isinstance(output_ids, list)
    output_ids.append("statement:3")
    assert report == report_before
    assert EXPECTED_IDS == ids_before


def test_failed_validation_does_not_mutate_input() -> None:
    payload = correct_payload()
    payload["verdict"] = "wrong"
    before = deepcopy(payload)

    with pytest.raises(ValueError):
        validate(payload)

    assert payload == before

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest


GENERATION_ROOT = Path(__file__).resolve().parents[1]
SKILL = GENERATION_ROOT / ".agents" / "skills" / "recursive-proving" / "SKILL.md"
AGENTS = GENERATION_ROOT / "AGENTS.md"
RUNNER = Path(__file__).with_name("run_example.sh")
POLICY_RE = re.compile(
    r"<!-- rethlas-recursive-wait-policy\s*(\{.*?\})\s*-->", re.DOTALL
)


def _policy() -> tuple[dict[str, object], str]:
    text = SKILL.read_text(encoding="utf-8")
    match = POLICY_RE.search(text)
    assert match is not None
    return json.loads(match.group(1)), text


def _replay_wait_schedule(policy: dict[str, object], outcomes: list[str]) -> list[int]:
    timeout = int(policy["initial_timeout_ms"])
    maximum = int(policy["max_timeout_ms"])
    multiplier = int(policy["backoff_multiplier"])
    waits: list[int] = []
    for outcome in outcomes:
        waits.append(timeout)
        if outcome == "mailbox_progress":
            assert policy["reset_timeout_on_mailbox_progress"] is True
            timeout = int(policy["initial_timeout_ms"])
        else:
            assert outcome == "timeout_no_progress"
            timeout = min(timeout * multiplier, maximum)
    return waits


@dataclass
class _PolicyReplay:
    """Executable offline model of the instruction-level flow contract."""

    policy: dict[str, object]
    plan_ids: tuple[str, ...]
    next_timeout_ms: int = field(init=False)
    orchestration_resumptions: int = 0
    observed_orchestration_input_tokens: int | None = None
    no_progress_timeouts: int = 0
    status: str = "running"
    cost_gate_reason: str | None = None
    successful_plan_ids: set[str] = field(default_factory=set)
    failed_plan_ids: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.next_timeout_ms = int(self.policy["initial_timeout_ms"])

    def wait_timeout(self) -> int:
        if self.status != "running":
            raise RuntimeError("cost-gated replay cannot issue another wait")
        return self.next_timeout_ms

    def record_collaboration_result(
        self, outcome: str, *, observed_input_tokens: int | None = None
    ) -> None:
        if self.status != "running":
            raise RuntimeError("cost-gated replay cannot resume orchestration")
        if outcome not in {
            "timeout_no_progress",
            "mailbox_progress",
            "spawn_fanout_result",
            "followup_fanout_result",
        }:
            raise ValueError(f"unknown collaboration outcome: {outcome}")
        if observed_input_tokens is not None and observed_input_tokens < 0:
            raise ValueError("observed input tokens must be non-negative")
        self.orchestration_resumptions += 1
        if observed_input_tokens is not None:
            self.observed_orchestration_input_tokens = (
                self.observed_orchestration_input_tokens or 0
            ) + observed_input_tokens

        if outcome == "timeout_no_progress":
            self.no_progress_timeouts += 1
            self.next_timeout_ms = min(
                self.next_timeout_ms * int(self.policy["backoff_multiplier"]),
                int(self.policy["max_timeout_ms"]),
            )
        elif outcome == "mailbox_progress":
            self.no_progress_timeouts = 0
            self.next_timeout_ms = int(self.policy["initial_timeout_ms"])

        if self.orchestration_resumptions >= int(
            self.policy["max_orchestration_resumptions"]
        ):
            self._gate("max_orchestration_resumptions")
        elif (
            self.observed_orchestration_input_tokens is not None
            and self.observed_orchestration_input_tokens
            >= int(self.policy["max_observed_orchestration_input_tokens"])
        ):
            self._gate("max_observed_orchestration_input_tokens")
        elif self.no_progress_timeouts >= int(
            self.policy["max_consecutive_no_progress_timeouts"]
        ):
            self._gate("max_consecutive_no_progress_timeouts")

    def _gate(self, reason: str) -> None:
        self.status = "waiting_cost_gate"
        self.cost_gate_reason = reason

    def unclassified_plan_ids(self) -> set[str]:
        return set(self.plan_ids) - self.successful_plan_ids - self.failed_plan_ids


def test_recursive_wait_contract_uses_long_backoff_and_early_wake_reset() -> None:
    policy, text = _policy()

    assert policy == {
        "policy_id": "rethlas_recursive_wait_v1",
        "initial_timeout_ms": 600_000,
        "backoff_multiplier": 2,
        "max_timeout_ms": 3_600_000,
        "max_consecutive_no_progress_timeouts": 4,
        "max_orchestration_resumptions": 16,
        "max_observed_orchestration_input_tokens": 3_000_000,
        "max_status_queries_without_mailbox_change": 0,
        "reset_timeout_on_mailbox_progress": True,
        "enforcement_scope": ("instruction_and_runner_integrity_not_runtime_mediated"),
    }
    assert _replay_wait_schedule(
        policy,
        [
            "timeout_no_progress",
            "timeout_no_progress",
            "timeout_no_progress",
            "mailbox_progress",
            "timeout_no_progress",
        ],
    ) == [600_000, 1_200_000, 2_400_000, 3_600_000, 600_000]
    assert "wakes early" in text
    assert "mailbox" in text
    assert not re.search(r"timeout_ms\s*[=:]\s*60_?000\b", text)


def test_recursive_wait_contract_forbids_no_change_status_poll_and_auto_human() -> None:
    policy, text = _policy()

    assert policy["max_status_queries_without_mailbox_change"] == 0
    assert "With no new state, the permitted\n   status-query count is zero" in text
    assert "Never invent or inject a human hot-join message" in text
    assert "one multi-tool response" in text
    assert (
        "Stop **all** new collaboration calls, including\n"
        "   `wait_agent`, `list_agents`, `send_message`, and `spawn_agent`" in text
    )
    assert "continue or end locally without another poll" in text
    assert '"status": "running|completed|waiting_cost_gate"' in text
    assert "waiting_cost_gate" in text
    assert "built-in collaboration calls bypass repository\ncode" in text
    assert "not a runtime interceptor" in text


def test_recursive_wait_contract_is_integrity_bound_by_runner_and_agents() -> None:
    policy, _text = _policy()
    agents = AGENTS.read_text(encoding="utf-8")
    runner = RUNNER.read_text(encoding="utf-8")

    assert str(policy["policy_id"]) in agents
    assert "600,000 ms" in agents
    assert "Repeated 60-second polling is forbidden" in agents
    assert "only the repository owner decides" in agents
    assert 'root / "AGENTS.md"' in runner
    assert 'trees = [root / ".codex", root / ".agents", root / "mcp"]' in runner


def test_executable_policy_replay_backs_off_resets_and_stops_exactly() -> None:
    policy, _text = _policy()
    timeout_state = _PolicyReplay(policy, ("plan-a",))
    observed_waits = []
    for _ in range(4):
        observed_waits.append(timeout_state.wait_timeout())
        timeout_state.record_collaboration_result("timeout_no_progress")

    assert observed_waits == [600_000, 1_200_000, 2_400_000, 3_600_000]
    assert timeout_state.status == "waiting_cost_gate"
    assert timeout_state.cost_gate_reason == "max_consecutive_no_progress_timeouts"
    assert timeout_state.unclassified_plan_ids() == {"plan-a"}
    with pytest.raises(RuntimeError, match="cannot issue another wait"):
        timeout_state.wait_timeout()

    reset_state = _PolicyReplay(policy, ("plan-a",))
    reset_state.record_collaboration_result("timeout_no_progress")
    assert reset_state.wait_timeout() == 1_200_000
    assert reset_state.no_progress_timeouts == 1
    reset_state.record_collaboration_result("mailbox_progress")
    assert reset_state.wait_timeout() == 600_000
    assert reset_state.no_progress_timeouts == 0

    resumption_state = _PolicyReplay(policy, ("done", "unfinished"))
    resumption_state.successful_plan_ids.add("done")
    resumption_state.record_collaboration_result("spawn_fanout_result")
    for _ in range(14):
        resumption_state.record_collaboration_result("mailbox_progress")
    assert resumption_state.status == "running"
    resumption_state.record_collaboration_result("followup_fanout_result")
    assert resumption_state.orchestration_resumptions == 16
    assert resumption_state.status == "waiting_cost_gate"
    assert resumption_state.cost_gate_reason == "max_orchestration_resumptions"
    assert resumption_state.unclassified_plan_ids() == {"unfinished"}
    assert "unfinished" not in resumption_state.successful_plan_ids
    assert "unfinished" not in resumption_state.failed_plan_ids

    token_state = _PolicyReplay(policy, ("plan-a",))
    token_state.record_collaboration_result(
        "spawn_fanout_result", observed_input_tokens=2_999_999
    )
    assert token_state.status == "running"
    token_state.record_collaboration_result(
        "followup_fanout_result", observed_input_tokens=1
    )
    assert token_state.status == "waiting_cost_gate"
    assert token_state.cost_gate_reason == "max_observed_orchestration_input_tokens"


def test_45_minute_incident_replay_uses_conservative_resumption_proxy() -> None:
    policy, _text = _policy()
    old_resumptions = 49
    old_reported_tokens = 9_052_168
    old_reported_input_tokens = 9_049_038
    old_average_input_tokens = old_reported_input_tokens / old_resumptions

    # Mailbox progress resets the timeout, so elapsed time alone cannot recover
    # an exact wake/timeout ordering. Use the policy's unconditional resumption
    # gate as the conservative bound and include the spawn-fanout resumption.
    bounded_resumptions = int(policy["max_orchestration_resumptions"])
    incident_average_projection = bounded_resumptions * old_average_input_tokens

    assert bounded_resumptions == 16
    assert bounded_resumptions < old_resumptions
    assert incident_average_projection < int(
        policy["max_observed_orchestration_input_tokens"]
    )
    assert incident_average_projection < old_reported_tokens / 3

    # This arithmetic is evidence for the observed incident, not a universal
    # cost ceiling. If orchestration-only usage is unavailable and future root
    # contexts average 200k input tokens, the 16-resumption proxy can exceed 3M.
    assert bounded_resumptions * 200_000 > int(
        policy["max_observed_orchestration_input_tokens"]
    )

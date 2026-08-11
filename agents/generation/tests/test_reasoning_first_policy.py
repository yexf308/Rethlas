from __future__ import annotations

import json
import re
from pathlib import Path


GENERATION_ROOT = Path(__file__).resolve().parents[1]
AGENTS = GENERATION_ROOT / "AGENTS.md"
POLICY_RE = re.compile(
    r"<!-- rethlas-reasoning-first-policy\s*(\{.*?\})\s*-->", re.DOTALL
)


def _policy() -> tuple[dict[str, object], str]:
    text = AGENTS.read_text(encoding="utf-8")
    match = POLICY_RE.search(text)
    assert match is not None
    return json.loads(match.group(1)), text


def _action_allowed(
    policy: dict[str, object],
    *,
    phase: str,
    action: str,
    named_knowledge_gap: bool = False,
    query_count: int = 0,
) -> bool:
    if phase == "protected_root_deep_work":
        return action in {
            "read_problem",
            "read_local_reference",
            "local_reasoning",
            "local_computation",
            "continuation_memory_search_once",
        }
    if phase == "candidate_fast_lane":
        return action in {
            "assemble_blueprint",
            "audit_candidate",
            "write_blueprint",
            "verify_blueprint",
            "repair_verifier_defect",
        }
    if action == "external_retrieval":
        return named_knowledge_gap and query_count < int(
            policy["max_targeted_retrieval_queries_per_gap"]
        )
    return True


def test_reasoning_first_policy_is_machine_readable_and_bounded() -> None:
    policy, text = _policy()
    assert policy == {
        "policy_id": "rethlas_reasoning_first_v1",
        "default_initial_deep_work_minutes": 30,
        "minimum_initial_deep_work_minutes": 10,
        "maximum_initial_deep_work_minutes": 90,
        "deep_work_minimum_is_soft": True,
        "initial_external_retrieval_calls": 0,
        "initial_collaboration_spawns": 0,
        "initial_memory_init_calls": 0,
        "initial_memory_search_calls_for_continuation": 1,
        "persistence_mode": "write_behind_phase_checkpoint",
        "checkpoint_tool": "memory_append_batch",
        "max_checkpoint_records": 32,
        "max_root_only_batches_before_first_critic": 1,
        "legal_yield_tool": "generation_yield",
        "retrieval_requires_explicit_knowledge_gap": True,
        "max_targeted_retrieval_queries_per_gap": 2,
        "initial_adversarial_critic_count": 1,
        "max_parallel_subagents_before_first_critic_report": 1,
        "candidate_fast_lane_forbids_new_search": True,
        "candidate_fast_lane_forbids_new_branches": True,
        "candidate_fast_lane_forbids_new_subagents": True,
        "advisor_after_root_and_critic_failure_synthesis": True,
        "telemetry_must_not_invent_reasoning_tokens": True,
        "enforcement_scope": (
            "instruction_runner_prompt_and_contract_tests_not_sampling_interceptor"
        ),
    }
    assert "soft reasoning target" in text
    assert "single pre-critic checkpoint" in text
    assert "root may publish at most one" in text
    assert "root-only skills contribute scratch" in text
    assert "Search volume" in text
    assert "candidate fast lane" in text


def test_protected_phase_and_candidate_fast_lane_reject_fragmenting_actions() -> None:
    policy, _text = _policy()
    for action in (
        "memory_init",
        "memory_append",
        "memory_append_batch",
        "branch_update",
        "external_retrieval",
        "spawn_subagent",
    ):
        assert not _action_allowed(
            policy, phase="protected_root_deep_work", action=action
        )
    for action in (
        "external_retrieval",
        "memory_search",
        "spawn_subagent",
        "propose_plan",
        "advisor_checkpoint",
        "wait_agent",
    ):
        assert not _action_allowed(policy, phase="candidate_fast_lane", action=action)
    for action in (
        "assemble_blueprint",
        "audit_candidate",
        "write_blueprint",
        "verify_blueprint",
        "repair_verifier_defect",
    ):
        assert _action_allowed(policy, phase="candidate_fast_lane", action=action)


def test_external_retrieval_requires_named_gap_and_two_query_budget() -> None:
    policy, _text = _policy()
    assert not _action_allowed(
        policy,
        phase="post_checkpoint",
        action="external_retrieval",
        named_knowledge_gap=False,
    )
    assert _action_allowed(
        policy,
        phase="post_checkpoint",
        action="external_retrieval",
        named_knowledge_gap=True,
        query_count=0,
    )
    assert _action_allowed(
        policy,
        phase="post_checkpoint",
        action="external_retrieval",
        named_knowledge_gap=True,
        query_count=1,
    )
    assert not _action_allowed(
        policy,
        phase="post_checkpoint",
        action="external_retrieval",
        named_knowledge_gap=True,
        query_count=2,
    )


def test_high_frequency_skills_use_write_behind_and_old_conflicts_are_absent() -> None:
    skill_names = (
        "obtain-immediate-conclusions",
        "construct-counterexamples",
        "construct-toy-examples",
        "direct-proving",
        "propose-subgoal-decomposition-plans",
        "query-memory",
        "identify-key-failures",
        "recursive-proving",
        "verify-proof",
    )
    for skill_name in skill_names:
        skill = (
            GENERATION_ROOT / ".agents" / "skills" / skill_name / "SKILL.md"
        ).read_text(encoding="utf-8")
        assert "memory_append_batch" in skill, skill_name

    agents = AGENTS.read_text(encoding="utf-8")
    recursive = (
        GENERATION_ROOT / ".agents" / "skills" / "recursive-proving" / "SKILL.md"
    ).read_text(encoding="utf-8")
    forbidden = (
        "Every intermediate artifact must be written to memory",
        "Always call `search_arxiv_theorems` for nontrivial subgoals",
        "Use web search early to gather background",
        "Spawn one sub-agent per decomposition plan",
        "may itself spawn sub-agents recursively",
        "Wait for all confirmed sub-agents",
    )
    combined = agents + "\n" + recursive
    for stale_clause in forbidden:
        assert stale_clause not in combined


def test_skill_interface_prompts_do_not_reintroduce_old_fanout_or_churn() -> None:
    interface_expectations = {
        "recursive-proving": "one context-free adversarial critic",
        "propose-subgoal-decomposition-plans": "one primary decomposition plan",
        "search-math-results": "one named external knowledge gap",
        "direct-proving": "one selected plan",
        "query-memory": "one bounded slice",
    }
    for skill_name, expected in interface_expectations.items():
        metadata = (
            GENERATION_ROOT
            / ".agents"
            / "skills"
            / skill_name
            / "agents"
            / "openai.yaml"
        ).read_text(encoding="utf-8")
        assert expected in metadata
        assert "one sub-agent per decomposition plan" not in metadata
        assert "propose several subgoal" not in metadata
        assert "multiple subgoal plans" not in metadata


def test_non_success_yields_cannot_masquerade_as_solution() -> None:
    _policy_data, agents = _policy()
    assert "The only successful terminal state" in agents
    assert "waiting_cost_gate" in agents
    assert "waiting_owner_advisor_decision" in agents
    assert "the theorem remains unsolved" in agents
    assert "generation_yield" in agents
    assert "does not stop the runner" in agents

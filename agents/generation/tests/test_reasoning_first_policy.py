from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


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


def _node_binary() -> Path:
    configured = os.environ.get("RETHLAS_TEST_NODE_BIN")
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured).expanduser())
    discovered = shutil.which("node")
    if discovered:
        candidates.append(Path(discovered))
    candidates.append(
        Path("/Applications/ChatGPT.app/Contents/Resources/cua_node/bin/node")
    )
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()
    pytest.skip("Node.js unavailable; set RETHLAS_TEST_NODE_BIN")


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


def test_checkpoint_recovery_requires_unknown_primary_and_identical_payload() -> None:
    _policy_value, agents = _policy()
    timeout_60s = (
        "tool call failed for "
        "`reasoning_checkpoint_primary/memory_append_batch`: "
        "timed out awaiting tools/call after 60s"
    )
    timeout_60000ms = (
        "tool call failed for "
        "`reasoning_checkpoint_primary/memory_append_batch`: "
        "timed out awaiting tools/call after 60000ms"
    )
    checkpoint_contract = agents.split(
        "Checkpoint batches use the two dedicated", 1
    )[1].split("## Phase-boundary routing", 1)[0]
    assert "**one** `functions.exec` JavaScript\nprogram" in agents
    assert "const checkpointArgs = Object.freeze({problem_id, items});" in agents
    primary_call = (
        "tools.mcp__reasoning_checkpoint_primary__memory_append_batch("
    )
    recovery_call = (
        "tools.mcp__reasoning_checkpoint_recovery__memory_append_batch("
    )
    assert checkpoint_contract.count(primary_call) == 1
    assert checkpoint_contract.count(recovery_call) == 1
    assert "tools.mcp__reasoning_agent__memory_append_batch(" not in (
        checkpoint_contract
    )
    assert checkpoint_contract.count(
        "memory_append_batch(\n      checkpointArgs\n    )"
    ) == 2
    assert f'failure === "{timeout_60s}"' in checkpoint_contract
    assert f'failure === "{timeout_60000ms}"' in checkpoint_contract
    assert "failure.includes" not in checkpoint_contract
    assert "failure.match" not in checkpoint_contract
    assert "RegExp" not in checkpoint_contract
    assert "receipt.isError === true" in agents
    assert 'receipt.isError !== false' in agents
    assert '["content", "isError", "structuredContent"]' in agents
    assert "body.status !== \"ok\"" in agents
    assert "rethlas_memory_batch_local_commit_receipt_v1" in agents
    assert "const hostBodyKeys = [...localBodyKeys, \"publication_receipt\"]" in agents
    assert "if (!localCommit && !hostPublication)" in agents
    assert 'publication.state !== "accepted"' in agents
    assert 'publication.publication_class !== "reasoning_checkpoint"' in agents
    assert "!sameJson(textBody, body)" in agents
    assert "never make a\nthird call, poll the unknown primary" in agents
    assert "long\n`reasoning_agent` server must never call" in agents
    assert "entire recovery\nallowlist" in checkpoint_contract
    assert "Compare with `===` only" in checkpoint_contract
    assert "yields `Script running with cell ID`" in checkpoint_contract
    assert "use `functions.wait` on that exact same cell" in checkpoint_contract
    assert "bounded\nto at most 120 seconds" in checkpoint_contract
    assert "continuation of the one outer cell, not an MCP\npoll or retry" in (
        checkpoint_contract
    )
    assert "Never issue a separate primary poll, new `functions.exec`" in (
        checkpoint_contract
    )

    def retryable(failure: object) -> bool:
        return type(failure) is str and failure in (timeout_60s, timeout_60000ms)

    class EqualityImpostor:
        def __eq__(self, _other: object) -> bool:
            return True

    assert retryable(timeout_60s)
    assert retryable(timeout_60000ms)
    for semantic_or_unclassified in (
        {"isError": True, "content": ["semantic rejection"]},
        "validation failed",
        "transport unknown",
        "timed out awaiting tools/call after 60000ms",
        "prefix " + timeout_60000ms,
        timeout_60000ms + " suffix",
        timeout_60000ms.replace("primary", "recovery"),
        EqualityImpostor(),
        None,
    ):
        assert not retryable(semantic_or_unclassified)


def test_checkpoint_receipt_validator_executes_fail_closed_mutation_matrix() -> None:
    _policy_value, agents = _policy()
    checkpoint_contract = agents.split(
        "Checkpoint batches use the two dedicated", 1
    )[1].split("## Phase-boundary routing", 1)[0]
    program = checkpoint_contract.split("```javascript\n", 1)[1].split(
        "\n```", 1
    )[0]
    validator = "const checkedReceipt = " + program.split(
        "const checkedReceipt = ", 1
    )[1].split("\nlet receipt;", 1)[0]
    checkpoint_args = {
        "problem_id": "data/frontiermath/chowla-cosine",
        "items": [
            {"channel": "proof_steps", "record": {"claim": "x"}},
            {
                "channel": "failed_paths",
                "record": {"claim": "y"},
                "active": False,
                "supersedes": ["mem_" + "a" * 64],
            },
        ],
    }
    harness = r'''
const hex = "a".repeat(64);
const body = {
  schema_version: "rethlas_memory_batch_receipt_v3",
  status: "ok",
  problem_id: checkpointArgs.problem_id,
  batch_id: `batch_${hex}`,
  checkpoint_sha256: "b".repeat(64),
  timestamp_utc: "2026-08-12T12:00:00.000001+00:00",
  committed_at_utc: "2026-08-12T12:00:01.000001+00:00",
  committed_at_monotonic: 100,
  commit_sha256: "c".repeat(64),
  count: 2,
  records: [
    {
      record_id: `mem_${"d".repeat(64)}`,
      channel: "proof_steps",
      active: true,
      supersedes: []
    },
    {
      record_id: `mem_${"e".repeat(64)}`,
      channel: "failed_paths",
      active: false,
      supersedes: [`mem_${hex}`]
    }
  ],
  checkpoint_path:
    `/tmp/memory/data/frontiermath/chowla-cosine/` +
    `.phase_checkpoints/batch_${hex}.json`,
  publication_receipt: {
    schema_version: "rethlas_memory_batch_publication_receipt_v1",
    state: "accepted",
    run_id: "guardian-soak-20260812-fresh-07",
    problem_id: checkpointArgs.problem_id,
    batch_id: `batch_${hex}`,
    checkpoint_sha256: "b".repeat(64),
    commit_sha256: "c".repeat(64),
    publication_class: "reasoning_checkpoint",
    cycle_id: `cycle_${"f".repeat(32)}`,
    cutoff_action_id: `cadact_${"1".repeat(32)}`,
    cutoff_kind: "review_1",
    cutoff_at_utc: "2026-08-12T12:30:00+00:00",
    cutoff_monotonic: 200,
    accepted_at_utc: "2026-08-12T12:00:01.000001+00:00",
    accepted_at_monotonic: 100,
    boot_identity: "boot-id",
    receipt_sha256: "2".repeat(64)
  }
};
const envelope = (value) => ({
  content: [{type: "text", text: JSON.stringify(value)}],
  structuredContent: value,
  isError: false
});
const clone = (value) => JSON.parse(JSON.stringify(value));
const localBody = clone(body);
localBody.schema_version =
  "rethlas_memory_batch_local_commit_receipt_v1";
delete localBody.publication_receipt;
const cases = [];
const raw = (name, value) => cases.push([name, () => value]);
const synced = (name, mutate) => {
  const value = clone(body);
  mutate(value);
  raw(name, envelope(value));
};
const syncedLocal = (name, mutate) => {
  const value = clone(localBody);
  mutate(value);
  raw(name, envelope(value));
};

raw("undefined", undefined);
raw("string", "ok");
raw("null", null);
raw("array", []);
raw("isError true", {...envelope(clone(body)), isError: true});
raw("isError string", {...envelope(clone(body)), isError: "false"});
raw("missing isError", {
  content: envelope(body).content,
  structuredContent: body
});
raw("extra outer field", {...envelope(clone(body)), extra: null});
raw("structuredContent string", {
  ...envelope(clone(body)),
  structuredContent: "ok"
});
raw("empty content", {...envelope(clone(body)), content: []});
raw("two content blocks", {
  ...envelope(clone(body)),
  content: [...envelope(body).content, ...envelope(body).content]
});
raw("content block extra field", {
  ...envelope(clone(body)),
  content: [{...envelope(body).content[0], extra: null}]
});
raw("content invalid json", {
  ...envelope(clone(body)),
  content: [{type: "text", text: "{"}]
});
raw("content disagrees", {
  ...envelope(clone(body)),
  content: [{
    type: "text",
    text: JSON.stringify({...body, status: "error"})
  }]
});

synced("top extra field", (value) => { value.extra = null; });
synced("status error", (value) => { value.status = "error"; });
synced("top schema", (value) => { value.schema_version = "v2"; });
synced("host fields under local schema", (value) => {
  value.schema_version = "rethlas_memory_batch_local_commit_receipt_v1";
});
synced("problem mismatch", (value) => {
  value.problem_id = "other";
  value.publication_receipt.problem_id = "other";
});
synced("count string", (value) => { value.count = "2"; });
synced("count boolean", (value) => { value.count = true; });
synced("record count mismatch", (value) => { value.records.pop(); });
synced("batch malformed", (value) => {
  value.batch_id = "batch_bad";
  value.publication_receipt.batch_id = "batch_bad";
});
synced("checkpoint digest malformed", (value) => {
  value.checkpoint_sha256 = "no";
  value.publication_receipt.checkpoint_sha256 = "no";
});
synced("timestamp invalid", (value) => {
  value.timestamp_utc = "not-a-time";
});
synced("timestamp after commit", (value) => {
  value.timestamp_utc = "2026-08-12T12:00:02+00:00";
});
synced("commit monotonic string", (value) => {
  value.committed_at_monotonic = "100";
  value.publication_receipt.accepted_at_monotonic = "100";
});
synced("path unbound", (value) => {
  value.checkpoint_path = "/tmp/other.json";
});
synced("publication null", (value) => {
  value.publication_receipt = null;
});
synced("publication extra field", (value) => {
  value.publication_receipt.extra = null;
});
synced("publication rejected", (value) => {
  value.publication_receipt.state = "rejected";
});
synced("publication control", (value) => {
  value.publication_receipt.publication_class = "control_only";
});
synced("nested problem mismatch", (value) => {
  value.publication_receipt.problem_id = "other";
});
synced("nested batch mismatch", (value) => {
  value.publication_receipt.batch_id = `batch_${"9".repeat(64)}`;
});
synced("nested checkpoint mismatch", (value) => {
  value.publication_receipt.checkpoint_sha256 = "9".repeat(64);
});
synced("nested commit mismatch", (value) => {
  value.publication_receipt.commit_sha256 = "9".repeat(64);
});
synced("accepted utc mismatch", (value) => {
  value.publication_receipt.accepted_at_utc =
    "2026-08-12T12:00:02+00:00";
});
synced("accepted monotonic mismatch", (value) => {
  value.publication_receipt.accepted_at_monotonic = 101;
});
synced("accepted at wall cutoff", (value) => {
  value.committed_at_utc = value.publication_receipt.cutoff_at_utc;
  value.publication_receipt.accepted_at_utc =
    value.publication_receipt.cutoff_at_utc;
});
synced("accepted at monotonic cutoff", (value) => {
  value.committed_at_monotonic = 200;
  value.publication_receipt.accepted_at_monotonic = 200;
});
synced("bad cutoff kind", (value) => {
  value.publication_receipt.cutoff_kind = "review_3";
});
synced("bad cycle id", (value) => {
  value.publication_receipt.cycle_id = "cycle_bad";
});
synced("bad cutoff action id", (value) => {
  value.publication_receipt.cutoff_action_id = "cadact_bad";
});
synced("empty boot identity", (value) => {
  value.publication_receipt.boot_identity = "";
});
synced("non-printable boot identity", (value) => {
  value.publication_receipt.boot_identity = "boot\nidentity";
});
synced("oversized boot identity", (value) => {
  value.publication_receipt.boot_identity = "b".repeat(129);
});
synced("malformed receipt digest", (value) => {
  value.publication_receipt.receipt_sha256 = "no";
});
synced("record extra field", (value) => {
  value.records[0].extra = null;
});
synced("record duplicate id", (value) => {
  value.records[1].record_id = value.records[0].record_id;
});
synced("record malformed id", (value) => {
  value.records[0].record_id = "mem_bad";
});
synced("record channel", (value) => {
  value.records[0].channel = "failed_paths";
});
synced("record active", (value) => { value.records[0].active = false; });
synced("record supersedes", (value) => {
  value.records[0].supersedes = [`mem_${hex}`];
});
syncedLocal("local extra field", (value) => { value.extra = null; });
syncedLocal("local publication null", (value) => {
  value.publication_receipt = null;
});
syncedLocal("local publication object", (value) => {
  value.publication_receipt = clone(body.publication_receipt);
});
syncedLocal("host schema without publication", (value) => {
  value.schema_version = "rethlas_memory_batch_receipt_v3";
});
syncedLocal("local status error", (value) => { value.status = "error"; });
syncedLocal("local path unbound", (value) => {
  value.checkpoint_path = "/tmp/other.json";
});
syncedLocal("local count mismatch", (value) => { value.count = 1; });
syncedLocal("local record mismatch", (value) => {
  value.records[0].channel = "failed_paths";
});
syncedLocal("local malformed commit digest", (value) => {
  value.commit_sha256 = "no";
});

const failed = [];
const validHost = checkedReceipt(envelope(clone(body)));
if (validHost.status !== "ok") failed.push("valid host receipt");
const validLocal = checkedReceipt(envelope(clone(localBody)));
if (
  validLocal.schema_version !==
    "rethlas_memory_batch_local_commit_receipt_v1"
) failed.push("valid local receipt");
for (const [name, build] of cases) {
  let rejected = false;
  try {
    checkedReceipt(build());
  } catch (_error) {
    rejected = true;
  }
  if (!rejected) failed.push(name);
}
process.stdout.write(JSON.stringify({
  mutation_count: cases.length,
  failed
}));
'''
    script = (
        "const checkpointArgs = Object.freeze("
        + json.dumps(checkpoint_args, separators=(",", ":"))
        + ");\n"
        + validator
        + harness
    )
    completed = subprocess.run(
        [str(_node_binary())],
        input=script,
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )
    assert completed.returncode == 0, completed.stderr
    outcome = json.loads(completed.stdout)
    assert outcome == {"mutation_count": 62, "failed": []}


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
        "search-math-results",
        "verify-proof",
    )
    for skill_name in skill_names:
        skill = (
            GENERATION_ROOT / ".agents" / "skills" / skill_name / "SKILL.md"
        ).read_text(encoding="utf-8")
        assert "memory_append_batch" in skill, skill_name
        assert "- `memory_append`" not in skill, skill_name
        assert "- `branch_update`" not in skill, skill_name

    agents = AGENTS.read_text(encoding="utf-8")
    assert "legacy JSONL outside the host publication registry" in agents
    assert "Relevant released-run tools" in agents
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

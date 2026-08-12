---
name: recursive-proving
description: Add one adversarial critic after a coherent root proof attempt and direct screening have produced a concrete failure synthesis. Use when the primary solver needs a bounded independent attack on its strongest plan before deciding whether any wider parallel work is justified.
---

# Recursive Proving

Use this skill when direct proving has failed on the current decomposition plans.

<!-- rethlas-recursive-wait-policy
{
  "policy_id": "rethlas_recursive_wait_v1",
  "initial_timeout_ms": 600000,
  "backoff_multiplier": 2,
  "max_timeout_ms": 3600000,
  "max_consecutive_no_progress_timeouts": 4,
  "max_orchestration_resumptions": 16,
  "max_observed_orchestration_input_tokens": 3000000,
  "cost_gate_policy_env": "RETHLAS_COST_GATE_POLICY",
  "cost_gate_policy_values": ["owner_gated", "disabled_by_owner"],
  "default_cost_gate_policy": "owner_gated",
  "disabled_by_owner_keeps_telemetry": true,
  "disabled_by_owner_may_yield_waiting_cost_gate": false,
  "max_status_queries_without_mailbox_change": 0,
  "reset_timeout_on_mailbox_progress": true,
  "enforcement_scope": "instruction_runner_integrity_and_host_generation_yield_preflight"
}
-->

<!-- rethlas-advisor-checkpoint-policy
{
  "policy_id": "rethlas_advisor_checkpoint_v1",
  "allowed_triggers": [
    "root_solver_and_first_critic_shared_failure_synthesis",
    "all_current_branches_terminal_blocked_or_dead_end",
    "all_remaining_routes_evidence_backed_near_exhaustion"
  ],
  "requires_failure_synthesis": true,
  "near_exhaustion_requires_no_live_subagents": true,
  "near_exhaustion_requires_no_scheduled_next_action": true,
  "near_exhaustion_requires_obstruction_record_per_remaining_route": true,
  "cost_gate_alone_may_trigger": false,
  "automatic_broker_prepare": false,
  "automatic_browser_dispatch": false,
  "automatic_followup": false,
  "prompt_must_synthesize_current_state": true,
  "source_context_sha256_required": true,
  "continuation_requires_new_request_and_exact_owner_authorization": true,
  "max_verified_fact_or_proof_ids": 12,
  "max_failed_path_record_ids": 12,
  "max_bottleneck_utf8_bytes": 2000,
  "max_recommended_question_utf8_bytes": 4000,
  "max_checkpoint_utf8_bytes": 16384,
  "deduplicate_until_new_math_or_advisor_receipt": true,
  "enforcement_scope": "instruction_and_runner_integrity_not_runtime_mediated"
}
-->

<!-- rethlas-recursive-pair-policy
{
  "policy_id": "rethlas_recursive_pair_v1",
  "root_role": "primary_solver",
  "initial_subagent_roles": ["adversarial_critic"],
  "initial_subagent_count": 1,
  "max_live_subagents": 2,
  "fork_turns": "none",
  "subagents_may_spawn": false,
  "subagents_write_shared_memory": false,
  "max_report_utf8_bytes": 8192,
  "candidate_preempts_wait_all": true,
  "expansion_requires_mutually_exclusive_obligations": true,
  "expansion_requires_root_cost_justification": true,
  "root_is_canonical_memory_writer": true,
  "enforcement_scope": "instruction_and_contract_tests_not_runtime_interceptor"
}
-->

The machine-readable policy above is part of this skill's contract. The runner
integrity-binds this file, but built-in collaboration calls bypass repository
code: this is an instruction-level flow contract, not a runtime interceptor. It
budgets root-agent orchestration resumptions, not mathematical work performed
inside a sub-agent. The collaboration runtime does not expose a trustworthy
orchestration-only token counter, so the input-token gate applies when usage is
observable; the resumption counter is the mandatory proxy otherwise. The
durable owner policy `RETHLAS_COST_GATE_POLICY` decides whether those two
thresholds stop orchestration. It defaults to `owner_gated` and cannot be
changed by the model or a wrapper restart. Under `disabled_by_owner`, keep
recording the counters and threshold crossings, but do not stop, yield, prune a
proof lane, or ask for owner input because of token, resumption, elapsed-time,
or model-cost telemetry.

## Input Contract

Read:

- the current set of decomposition plans
- the direct-proving reports and key stuck points for each plan
- the known stuck points from other plans
- relevant `failed_paths`, `branch_states`, and search results

## Procedure

1. Confirm that the root has completed its protected deep-work phase, screened
   the primary plan and at most one fallback with `$direct-proving`, and
   persisted one concrete shared failure synthesis. If a complete candidate
   already exists, do not invoke this skill; enter the candidate fast lane.
2. Spawn exactly one adversarial critic. Use a context-free fork
   (`fork_turns="none"`) when the host supports it, and pass a bounded prompt
   containing the authoritative problem path/id, the strongest plan, the
   relevant memory record ids, and the exact obstruction. Do not copy the full
   root transcript and do not insert a status query around the spawn.
3. Tell the critic to attack the plan's decisive dependencies, search for a
   counterexample, and identify the smallest viable repair. It must not restart
   a broad literature survey, spawn another agent, or write progress into
   shared memory. It returns one report of at most 8,192 UTF-8 bytes; the root
   is the canonical memory writer.
4. Wait for the critic with the completion-driven protocol below. If the root
   independently obtains a complete candidate, stop waiting immediately and
   enter the candidate fast lane. If interruption is supported, interrupt the
   critic; otherwise issue no more collaboration calls for it and ignore late
   nonessential progress.
5. On the critic's final report, the root performs one synthesis and persists
   the critic result plus the resulting branch decision in one
   `memory_append_batch` checkpoint.
6. If the critic validates or repairs a complete proof, assemble the draft and
   verify it immediately. If it identifies no new mechanism, invoke
   `$identify-key-failures` and consider the evidence-triggered advisor
   checkpoint before spending on wider recursion.
7. Expand only when the critic identifies mutually exclusive, concrete proof
   obligations and the root records why parallelism is worth the added context
   and orchestration cost. At most two sub-agents may be live. Spawn the
   selected obligations in one fanout, again with context-free forks, no child
   spawning, no shared-memory progress writes, and bounded final reports.
8. A complete candidate from any selected obligation preempts wait-all. Stop
   unrelated waits/spawns/search, assemble the proof, and enter verification.
   If every selected obligation returns a decisive failure, hand the compact
   reports to `$identify-key-failures`.

## Completion-driven wait protocol

Maintain the confirmed sub-agent IDs and completed IDs locally. A final report
from a confirmed ID is authoritative progress; do not call `list_agents` merely
to rediscover it.

1. The first `wait_agent` timeout is **600,000 ms**. `wait_agent` wakes early
   when a mailbox message or completion arrives, so a long timeout does not
   delay useful progress.
2. On a timeout with no mailbox change, do not call `list_agents`, do not send a
   reminder, and do not immediately poll again at the same interval. Double
   the next timeout, capped at **3,600,000 ms**.
3. On a real mailbox update, process it, update the completed-ID set, and reset
   the next timeout to 600,000 ms. Progress messages that are not final do not
   mark an agent complete.
4. Use `list_agents` only to reconcile an ambiguous tool failure or a mailbox
   update that lacks a canonical sender/status. With no new state, the permitted
   status-query count is zero. One final reconciliation is allowed only after
   an explicit ambiguity, not after an ordinary timeout.
5. If follow-up guidance is genuinely required for several agents, emit all
   independent messages in one multi-tool response when supported. Never send
   periodic "still working?" prompts. One follow-up fanout batch is the default
   maximum for a recursive round.
6. Count every root-model resumption after a collaboration tool result as one
   orchestration resumption, and always persist whether 16 resumptions or
   3,000,000 observed orchestration input tokens has been crossed. Under
   `owner_gated`, either threshold stops **all** new collaboration calls,
   including `wait_agent`, `list_agents`, `send_message`, and `spawn_agent`.
   Under `disabled_by_owner`, those thresholds are telemetry only: continue the
   completion-driven protocol without a cost yield. Independently of cost
   policy, stop the recursive round after four consecutive no-progress
   timeouts, persist that liveness failure, and continue or end locally without
   another poll. This is a route/liveness decision: return the bounded evidence
   to the root or the already scheduled critic. It is never
   `waiting_cost_gate`, never a self-issued review verdict, never a new `T0`,
   and never permission to work through a review or hard-stop boundary. Never
   invent missing reports or convert a cost/liveness gate into a mathematical
   verdict.
7. An enabled cost gate is deterministic flow control, not a request for human
   input. Never invent or inject a human hot-join message, and never decide on
   the owner's behalf that human intervention should occur.
   It also never authorizes an advisor request: do not open Chrome, invoke a
   browser advisor, prepare/authorize/retry an advisor job, or ask another agent
   to do so. Only the repository owner may initiate that separate workflow.

## Event-driven strategic advisor checkpoint

The root (not a sub-agent) may recommend one owner-decided Pro consultation
after either (a) the protected root attempt and first adversarial critic have
produced a shared, evidence-backed failure synthesis with no complete
candidate, (b) every confirmed branch has reached a terminal blocked/dead-end
result, or (c) all remaining routes are demonstrably near exhaustion. Every
trigger requires no live sub-agent. The third trigger additionally requires
no already scheduled next action, every remaining route has a concrete
failure/obstruction record, and `$identify-key-failures` has synthesized the
shared obstruction. A subjective sentence that the search is "stuck", a cost
gate, timeout, long runtime, or token count never satisfies any trigger.
For the root-plus-critic trigger, the root attempt, critic report, and failure
synthesis must each have durable record ids. This is a strategic mathematical
checkpoint, not a retry policy or a mandatory ceremony.

Persist exactly one bounded `events` record using the
`rethlas_advisor_checkpoint_v1` limits above. Include only evidence-backed
verified fact/proof ids (use an empty list if none has actually been verified),
failed-path record ids, the central bottleneck, and one exact recommended
question asking Pro to choose or sharpen the next mathematical direction. The
question is generated from this checkpoint's current problem state, never
copied from a fixed generic prompt: restate the authoritative problem
succinctly, summarize the included verified facts and failed routes with their
ids, distinguish proof from heuristic evidence, state the current bottleneck,
and ask for one bounded decisive next step. Hash the canonical problem
statement plus the exact included fact/proof records, failed-path records, and
bottleneck as `source_context_sha256`. Set
`owner_action_required=true`, `browser_dispatch_authorized=false`, and
`advisor_request_id=null`. Derive a content-addressed checkpoint id from the
canonical payload and do not repeat the same checkpoint until new mathematical
evidence or a new advisor receipt exists. Persist the event and a
`branch_states` transition to `waiting_owner_advisor_decision` as two items in
one `memory_append_batch`, bind both returned record ids by input order, and call
`generation_yield(problem_id, state="waiting_owner_advisor_decision",
reason=..., evidence_record_ids=[advisor_event_id, branch_state_id])` as the
final tool action. Then end locally without polling the owner. A branch-state
write without this bound yield receipt does not stop the runner.

```json
{
  "event_type": "advisor_checkpoint",
  "policy_id": "rethlas_advisor_checkpoint_v1",
  "checkpoint_id": "acp_<24 lowercase sha256 hex>",
  "trigger": "root_solver_and_first_critic_shared_failure_synthesis|all_current_branches_terminal_blocked_or_dead_end|all_remaining_routes_evidence_backed_near_exhaustion",
  "verified_fact_or_proof_ids": [],
  "failed_path_record_ids": [],
  "central_bottleneck": "...",
  "source_context_sha256": "<64 lowercase sha256 hex>",
  "recommended_exact_question": "...",
  "owner_action_required": true,
  "browser_dispatch_authorized": false,
  "advisor_request_id": null,
  "status": "waiting_owner_advisor_decision"
}
```

This recommendation never calls `advisor_bridge.py`, never prepares or
authorizes a job, never opens Chrome, and never grants a Send click. The owner
may ignore, edit, or separately authorize the exact question. If an
`advisor_available` receipt later arrives, the root must first review the
untrusted report against existing facts and failed paths, persist which
suggestions were accepted or rejected and why, and only then synthesize a new
branch plan. The report remains non-verifying and non-publishing.

If later evidence justifies another consultation, create a new checkpoint from
the then-current verified facts, failed paths, accepted/rejected parts of the
prior report, work performed since it arrived, and the new bottleneck. It must
contain a newly synthesized exact question and source-context digest. Never
send a follow-up automatically. The owner must prepare a new request id and
authorize that exact new question. The owner may explicitly continue in the
same ChatGPT conversation through the broker's digest-bound lineage workflow;
transcript continuity supplies context only and grants no authority. After
writing the checkpoint, keep `waiting_owner_advisor_decision`, make the bound
`generation_yield` call, and stop: do not poll, dispatch, start a paid turn, or
interrupt an active one.

Repeated 60-second polling is forbidden for recursive orchestration. A runtime
that cannot honor the long wait or early mailbox wake must fail the round's
orchestration contract rather than silently reverting to a busy poll.

## Output Contract

Append an `events` record for the recursive round:

```json
{
  "event_type": "recursive_proving_round",
  "pair_policy_id": "rethlas_recursive_pair_v1",
  "plan_ids": ["..."],
  "subagents": [
    {"id": "...", "role": "adversarial_critic|selected_obligation"}
  ],
  "shared_stuck_points": {
    "plan_id": ["..."]
  },
  "status": "running|completed|liveness_stopped|waiting_cost_gate",
  "successful_plan_ids": ["..."],
  "failed_plan_ids": ["..."],
  "candidate_preempted_wait_all": false,
  "expansion_cost_justification": null,
  "orchestration_cost": {
    "policy_id": "rethlas_recursive_wait_v1",
    "cost_gate_policy": "owner_gated|disabled_by_owner",
    "orchestration_resumptions": 0,
    "observed_orchestration_input_tokens": null,
    "observed_thresholds": [],
    "wait_timeouts_ms": [],
    "no_progress_timeouts": 0,
    "status_queries": 0,
    "spawn_fanout_batches": 1,
    "followup_fanout_batches": 0,
    "cost_gate_reason": null
  }
}
```

Persist the recursive-round event and its `branch_states` status/per-plan
outcomes as two items in one `memory_append_batch`.
After four consecutive no-progress timeouts, use `status="liveness_stopped"`,
persist the bounded reports and missing agent ids, issue no generation yield,
and return control to the root or scheduled route review under the unchanged
cycle clock.
Only under `owner_gated`, if a cost threshold fires, use
`status="waiting_cost_gate"`, keep unfinished plan IDs out of both outcome
lists, and persist the same `orchestration_cost` object. Bind the batch
receipt's event and branch-state record ids by input order, then call
`generation_yield(problem_id, state="waiting_cost_gate", reason=...,
evidence_record_ids=[recursive_event_id, branch_state_id])` as the final tool
action. Do not issue another collaboration or reasoning call afterward. Under
`disabled_by_owner`, record the threshold in `observed_thresholds` but never
write `waiting_cost_gate` and never call that yield; the trusted MCP/host
preflight rejects such a call before any control record is written.

## Tools

- `memory_search`
- `memory_append_batch`
- `generation_yield`
- `search_matlas_theorems` (official Matlas published-journal/book search) and
  the distinct legacy `search_arxiv_theorems` Danus/LeanSearch arXiv provider;
  neither is an implicit fallback, and results are leads rather than proof
  evidence or full articles/PDFs. Record provider errors as operational and
  use at most one authorized web/arXiv fallback for the same named gap within
  the two-query limit.
  For official results retain `candidate_id`, map `paper_id` to a nonempty DOI
  (or title/authors/year with a web-verification obligation), and map
  `theorem_id` to `entity_name`. Preserve `candidate_id` as the provider
  candidate ID; do not treat it as the bibliographic theorem number. Legacy
  results keep `arxiv_id`/`theorem_id`, and unread primary text stays a lead.
- Codex sub-agent tools: `spawn_agent`, `send_message`, `wait_agent`,
  `list_agents`, and `interrupt_agent` when available (exact availability
  varies by host)

## Failure Logging

If the root and critic fail to repair the strongest plan, append one compact
summary to `failed_paths` and invoke `$identify-key-failures`. The root may then
consider the evidence-triggered advisor checkpoint before wider expansion. For
a near-exhaustion checkpoint, first prove the additional no-live,
no-scheduled-action, and per-route obstruction-record conditions.

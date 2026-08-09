---
name: recursive-proving
description: Launch one sub-agent per decomposition plan after direct screening has identified the key stuck points for each plan. Use when all current plans have been screened by direct proving, none fully solves the problem, and parallel recursive work is needed.
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
  "max_status_queries_without_mailbox_change": 0,
  "reset_timeout_on_mailbox_progress": true,
  "enforcement_scope": "instruction_and_runner_integrity_not_runtime_mediated"
}
-->

The machine-readable policy above is part of this skill's contract. The runner
integrity-binds this file, but built-in collaboration calls bypass repository
code: this is an instruction-level flow contract, not a runtime interceptor. It
budgets root-agent orchestration resumptions, not mathematical work performed
inside a sub-agent. The collaboration runtime does not expose a trustworthy
orchestration-only token counter, so the input-token gate applies when usage is
observable; the resumption gate is the mandatory proxy otherwise.

## Input Contract

Read:

- the current set of decomposition plans
- the direct-proving reports and key stuck points for each plan
- the known stuck points from other plans
- relevant `failed_paths`, `branch_states`, and search results

## Procedure

1. Confirm that all current decomposition plans have already been attempted with `$direct-proving` and that none has fully solved the problem.
2. Spawn one sub-agent per decomposition plan. When the host supports multiple
   independent tool calls in one response, issue the complete spawn fanout in
   that single response. Do not insert a status query between spawns. With a
   full-history fork, omit model/agent-type overrides; with an explicit
   override, do not use a full-history fork. Record only canonical IDs returned
   by successful spawn calls.
3. Give each sub-agent:
   - the full target theorem
   - the assigned decomposition plan
   - the key stuck points for its own plan
   - the key stuck points found in the other plans
   - the instruction to follow `AGENTS.md`
4. Tell each sub-agent to tackle the assigned plan under the instructions in `AGENTS.md`, treating that plan as its starting point rather than restarting the search from zero. If new evidence or discoveries justify it, the sub-agent may refine, extend, or locally revise the plan, but it should preserve continuity with the assigned plan instead of discarding it outright.
5. Tell each sub-agent that it may itself spawn sub-agents recursively if that helps its assigned plan.
6. Require each sub-agent to write progress, failures, and any successful proof development back into the shared memory using the same data-relative `problem_id` as the MCP `problem_id`.
7. Wait for all confirmed sub-agents using the completion-driven protocol
   below, then gather their reports.
8. If any plan succeeds, assemble the proof draft from that plan.
9. If all plans fail, hand the collected reports to `$identify-key-failures`.

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
   orchestration resumption. Stop **all** new collaboration calls, including
   `wait_agent`, `list_agents`, `send_message`, and `spawn_agent`, when either
   16 resumptions or 3,000,000 observed orchestration input tokens is reached.
   Also stop after four consecutive no-progress timeouts. Persist the exact
   gate reason, then continue or end locally without another poll, and leave
   the recursive round incomplete; do not invent missing reports or convert
   the gate into a mathematical verdict.
7. A cost gate is deterministic flow control, not a request for human input.
   Never invent or inject a human hot-join message, and never decide on the
   owner's behalf that human intervention should occur.

Repeated 60-second polling is forbidden for recursive orchestration. A runtime
that cannot honor the long wait or early mailbox wake must fail the round's
orchestration contract rather than silently reverting to a busy poll.

## Output Contract

Append an `events` record for the recursive round:

```json
{
  "event_type": "recursive_proving_round",
  "plan_ids": ["..."],
  "subagent_ids": ["..."],
  "shared_stuck_points": {
    "plan_id": ["..."]
  },
  "status": "running|completed|waiting_cost_gate",
  "successful_plan_ids": ["..."],
  "failed_plan_ids": ["..."],
  "orchestration_cost": {
    "policy_id": "rethlas_recursive_wait_v1",
    "orchestration_resumptions": 0,
    "observed_orchestration_input_tokens": null,
    "wait_timeouts_ms": [],
    "no_progress_timeouts": 0,
    "status_queries": 0,
    "spawn_fanout_batches": 1,
    "followup_fanout_batches": 0,
    "cost_gate_reason": null
  }
}
```

Update `branch_states` with the recursive round status and per-plan outcomes.
If a cost gate fires, use `status="waiting_cost_gate"`, keep unfinished plan IDs
out of both outcome lists, and persist the same `orchestration_cost` object.

## Tools

- `memory_search`
- `memory_append`
- `branch_update`
- `search_arxiv_theorems`
- Codex sub-agent tools: `spawn_agent`, `send_message`, `wait_agent`,
  `list_agents` (exact availability varies by host)

## Failure Logging

If every plan fails in the recursive round, append a summary record to `failed_paths` and immediately invoke `$identify-key-failures`.

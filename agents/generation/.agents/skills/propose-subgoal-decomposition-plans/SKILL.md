---
name: propose-subgoal-decomposition-plans
description: Select one primary decomposition plan and at most one materially different fallback from the current evidence. Use after a coherent root attempt or failure synthesis reveals a real obstruction; do not use merely to create breadth at the start of a problem.
---

# Propose Subgoal Decomposition Plans

Use this skill when the agent has enough context to choose a strong next plan
without fragmenting the proof search.

## Input Contract

Read:

- the current target theorem or branch goal
- relevant `immediate_conclusions`, `toy_examples`, and `counterexamples`
- relevant `failed_paths` and `branch_states`
- recent search results and useful references from `events`

## Procedure

1. Gather the current information that materially constrains the problem: useful examples, failed claims, known obstructions, and relevant search results.
2. Propose one primary plan and, only when it uses a genuinely different
   mechanism, one fallback. Do not create more than two current plans.
3. For each plan, state:
   - the main idea of the plan
   - the ordered subgoals
   - why this plan is plausible given the current information
   - which earlier failures or counterexamples it tries to avoid
4. Hand the primary plan to `$direct-proving`. Screen the fallback only after a
   decisive primary-plan obstruction; do not interleave both.

## Output Contract

Return the selected plan set as one consolidated `subgoals` record for the
root's next `memory_append_batch` checkpoint:

```json
{
  "record_type": "decomposition_plan_set",
  "goal": "...",
  "plans": [
    {
      "plan_id": "...",
      "role": "primary|fallback",
      "plan_summary": "...",
      "subgoals": ["..."],
      "motivation": ["..."]
    }
  ],
  "uses_information_from": {
    "examples": ["..."],
    "counterexamples": ["..."],
    "key_failures": ["..."],
    "search_results": ["..."]
  },
  "status": "selected|failed|solved",
  "branch_id": "optional"
}
```

Do not append a second event that merely restates the same plan set.

## MCP Tools

- `memory_search`
- `memory_append`
- `memory_append_batch`
- `branch_update`
- `search_arxiv_theorems`

## Failure Logging

If the agent cannot yet propose a meaningful primary plan, return an event
payload for the next phase checkpoint with:

- `event_type="decomposition_plans_not_ready"`
- the missing information
- the blockers that prevent proposing plans

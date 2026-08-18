---
name: identify-key-failures
description: Compress three terminal route-solver reports into one reusable failure synthesis. Use before a new three-route generation or an evidence-triggered Pro recommendation.
---

# Identify Key Failures

Use this skill to turn many failed attempts into reusable guidance for the next planning round.

## Input Contract

Read:

- the failed decomposition plans
- direct-proving stuck points
- recursive sub-agent reports
- existing `failed_paths`
- relevant `counterexamples` and `toy_examples`

## Procedure

1. Gather the protected route-set record and exactly three bound route-solver
   reports from the completed fanout. Reject an incomplete or duplicate plan
   association rather than inventing the missing direction.
2. List the key stuck points for each plan.
3. Identify common points across those failures:
   - recurring obstructions or counterexamples
   - decomposition patterns that keep breaking
   - search gaps or missing background facts
4. Summarize what the failures suggest for the next generation of decomposition plans.
5. Return one synthesized `failed_paths` item for the root's next
   `memory_append_batch` checkpoint.
6. Decide among three next states: one genuinely new exact three-route
   generation, an evidence-triggered owner Pro checkpoint, or a truthful
   non-success yield. Do not refill one route slot in isolation.

## Output Contract

Return for `failed_paths`:

```json
{
  "record_type": "key_failures_summary",
  "failed_plan_ids": ["..."],
  "plan_failures": [
    {
      "plan_id": "...",
      "stuck_points": ["..."]
    }
  ],
  "common_failures": ["..."],
  "implications_for_next_plans": ["..."]
}
```

Include a next-state event in the same batch only when a concrete next action
has been selected.

## MCP Tools

- `memory_search`
- `memory_append_batch`

## Failure Logging

If the reports are too weak to identify meaningful common failures, return an
`events` payload with `event_type="key_failures_inconclusive"` and state what
information is still missing. Do not expand or request Pro until the evidence
gap is closed.

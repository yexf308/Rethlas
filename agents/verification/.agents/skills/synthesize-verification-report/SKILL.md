---
name: synthesize-verification-report
description: Aggregate all detected errors and gaps into the final verification report, apply strict accept/reject logic, and produce repair hints when rejected.
---

# Synthesize Verification Report

Produce the final verification output JSON and verdict.

## Input Contract

Read all findings from:

- `statement_checks`
- `reference_checks`

Each issue must include `location` and `issue`.

Also read the exact `Expected_checked_item_ids`, `Proof_digest`, and
`Fact_context.digest` values from the API prompt.

## Procedure

1. Query `statement_checks` and `reference_checks` with an adequate
   `max_chars`. If either query reports incomplete or truncated coverage, add
   a gap for incomplete verification-memory aggregation rather than silently
   dropping records.
2. Collect all critical errors and all gaps from previous checks.
3. Build a complete `verification_report` object with:
   - `summary`
   - `critical_errors`
   - `gaps`
4. Apply strict verdict rule:
   - `correct` iff `critical_errors=[]` and `gaps=[]`.
   - otherwise `wrong`.
5. If verdict is `wrong`, produce concrete non-empty `repair_hints`.
6. Copy the expected item id list and both supplied digests exactly into the
   output; never invent, normalize, or recompute them.
7. Validate the output via `validate_verification_output`.
8. Persist output via `write_verification_output`.

## Output Contract

Final output JSON:

```json
{
  "verification_report": {
    "summary": "string",
    "critical_errors": [],
    "gaps": []
  },
  "verdict": "correct",
  "repair_hints": "",
  "checked_item_ids": ["pi_0123456789abcdef01234567"],
  "proof_digest": "0000000000000000000000000000000000000000000000000000000000000000",
  "context_digest": "1111111111111111111111111111111111111111111111111111111111111111"
}
```

Those concrete values illustrate the shape only; use the exact item ID and
digests supplied for the current request.

If there is any error or gap, verdict must be `"wrong"` and `repair_hints` must be non-empty.

## MCP Tools

- `memory_query`
- `memory_append`
- `validate_verification_output`
- `write_verification_output`

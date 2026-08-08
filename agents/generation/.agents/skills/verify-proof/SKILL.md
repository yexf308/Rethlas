---
name: verify-proof
description: Verify candidate proofs with the local proof verification MCP service. Use only when a full candidate proof of the entire problem has been assembled in markdown, and before publishing the final verified blueprint.
---

# Verify Proof

Use the local proof verification service as the canonical verifier before accepting a solution.
Do not use this skill for partial proofs, isolated subgoals, or branches that have not yet produced a full proof draft of the whole problem.

## Input Contract

Read:

- target theorem statement
- assembled proof blueprint candidate from `results/{problem_id}/blueprint.md` as pure markdown text
- relevant prior failure reports and branch context

## Procedure

1. Read the current `results/{problem_id}/blueprint.md` draft as pure text.
2. First check that `blueprint.md` contains a full proof draft of the entire target theorem rather than a partial proof, fragment, or exploratory notes. If it does not, do not call the verifier yet.
3. Call MCP tool `verify_blueprint_service` with:
   - `problem_id`: the current data-relative problem id
   Do not pass the target statement or raw blueprint text as tool arguments.
   The tool reads the target from `data/{problem_id}.md` and checks the runner's
   bound source digest.
4. Read `verification_report.summary`, `critical_errors`, `gaps`, `verdict`,
   `repair_hints`, `checked_item_ids`, both digests, and `published`.
5. Persist the complete MCP publication envelope without renaming or dropping
   fields. It contains the six-field HTTP verification result plus
   `published`; successful publication also includes `published_path` and
   `publication_receipt_path`.
6. Treat the proof as failed if any of the following hold:
   - `verdict` is `"wrong"`
   - `verification_report.critical_errors` is non-empty
   - `verification_report.gaps` is non-empty
7. Only treat the proof as passed when none of the failure conditions above
   hold and `published=true`.
8. Never rename or copy the draft yourself. Atomic publication is performed
   inside the tool after digest and coverage validation.

## Output Contract

Append to `verification_reports`:

```json
{
  "verification_report": {
    "summary": "string",
    "critical_errors": [
      {"location": "", "issue": "detailed description of the issue"}
    ],
    "gaps": [
      {"location": "", "issue": "detailed description of the gap"}
    ]
  },
  "verdict": "string",
  "repair_hints": "string",
  "checked_item_ids": ["pi_0123456789abcdef01234567"],
  "proof_digest": "0000000000000000000000000000000000000000000000000000000000000000",
  "context_digest": "1111111111111111111111111111111111111111111111111111111111111111",
  "published": true,
  "published_path": "results/problem/blueprint_verified.md",
  "publication_receipt_path": "../.verification_receipts/problem.json"
}
```

These concrete IDs and digests illustrate the response shape; the tool returns
the values bound to the current blueprint.

Persist the verification service response exactly as returned.

If verification fails, revise `blueprint.md` directly and append to `failed_paths` when a branch is invalidated.

## MCP Tools

- `verify_blueprint_service`
- `memory_append`
- `memory_search`
- `branch_update`
- Codex built-in web search and `search_arxiv_theorems` when the verifier identifies a missing lemma or gap

## Failure Logging

Always persist verification output, including successful checks.

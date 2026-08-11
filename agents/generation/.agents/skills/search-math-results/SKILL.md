---
name: search-math-results
description: Resolve one named external knowledge gap that blocks an active proof route. Use only after a coherent local reasoning pass has identified a specific missing theorem, construction, counterexample, definition, or attribution whose answer could materially advance or kill that route.
---

# Search Math Results

Use this skill as a bounded exception to independent reasoning, not as the
default opening move or a general background sweep. Do not invoke it during the
protected deep-work phase or after the candidate fast lane begins.

## Input Contract

Read:

- the current target statement, subgoal, lemma, or claim
- one explicit `named_knowledge_gap`
- the route-changing decision that the answer would enable
- the search intent:
  - `theorem`
  - `construction`
  - `example`
  - `counterexample`
  - `background`
- relevant branch/subgoal context from memory

## Procedure

1. State the named gap and why resolving it could change the active route. If
   that cannot be stated precisely, return to independent reasoning without a
   search call.
2. Start with one `search_arxiv_theorems` query.
3. Phrase the query as a complete mathematical statement whenever possible,
   inspect the results, and stop as soon as the named gap is resolved or the
   active route is killed.
4. If the theorem query is not useful, use at most one built-in web-search
   query for the same named gap. Do not broaden it into a survey. The total
   query budget for one gap is two.
5. Download a paper only when one identified result is likely to resolve the
   named gap. Keep the PDF and extracted text inside `downloads/`, read the
   relevant proof and definitions, and verify applicability before relying on
   it.
6. If the result is partial, identify the extra hypotheses, where the proof
   fails without them, and whether that failure advances or kills the route.
7. Return immediately to a coherent reasoning phase. Summarize only findings
   that change the proof state; preserve the complete statement and source ids
   if the result may enter the proof.

## Usefulness Test

Treat theorem-search results as useful only if they do at least one of the following:

- provide a theorem/lemma/definition close to the target statement
- provide a construction/example/counterexample that can be adapted
- suggest a standard technique or reformulation relevant to the current branch
- expose a meaningful obstruction or extra hypothesis in a partial result that clarifies why the full problem is harder

If the first results are vague, off-topic, or too weak, use the one permitted
fallback query. If that also fails, close the named gap as unresolved and stop
retrieval for this phase.

## Output Contract

Return this compact record to the root for inclusion in the next
`memory_append_batch` phase checkpoint:

```json
{
  "event_type": "search_math_results",
  "named_knowledge_gap": "...",
  "route_changing_decision": "...",
  "query_count": 1,
  "query": "...",
  "search_intent": "theorem|construction|example|counterexample|background",
  "primary_tool": "search_arxiv_theorems",
  "fallback_used": false,
  "results_summary": ["..."],
  "useful_references": [
    {
      "title": "...",
      "complete_statement": "...",
      "url_or_id": "...",
      "paper_id": "...",
      "arxiv_id": "...",
      "theorem_id": "...",
      "local_pdf_path": "optional",
      "local_text_path": "optional",
      "expanded_definitions": ["paper-context expansions of terms/concepts used in the statement"],
      "applicability_check": ["why the statement does or does not apply in the current setting"],
      "partial_result_analysis": ["extra hypotheses, where the method fails for the full problem, and what difficulty this reveals"],
      "proof_insights": ["optional extracted techniques or ideas from the proof"],
      "why_useful": "..."
    }
  ],
  "branch_id": "optional",
  "subgoal_id": "optional"
}
```

## MCP Tools

- `search_arxiv_theorems`
- `memory_append`
- `memory_search`

## Failure Logging

If neither bounded query yields useful information, return an event payload
for the next phase checkpoint with:

- `event_type="search_math_results_stalled"`
- the attempted queries
- the reason the results were not useful

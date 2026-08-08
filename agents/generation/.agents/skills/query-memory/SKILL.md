---
name: query-memory
description: Retrieve previously saved immediate conclusions, toy examples, counterexamples, failed paths, or branch states from memory. Use when you want to check whether earlier conclusions, examples, counterexamples, failed paths, or brach states can bring insight to the current question, claim, subgoal, or branch decision, or when you want to test a claim against previously saved counterexamples.
---


# Query Memory

Use this skill when you want to check whether earlier conclusions, examples, counterexamples, failed paths, or brach states can bring insight to the current question, claim, subgoal, or branch decision, or when you want to test a claim against previously saved counterexamples.

## Input Contract

Read:

- the current question, claim, subgoal, or branch decision
- the specific type of prior artifact you want to recover
- the most relevant channel list, chosen from:
  - `immediate_conclusions`
  - `toy_examples`
  - `counterexamples`
  - `failed_paths`
  - `branch_states`

## Procedure

1. Form a concrete natural-language query describing the information you want to recover.
2. Choose the smallest relevant list of channels instead of searching everything by default.
3. Call `memory_search(problem_id, query, channels=..., limit_per_channel=..., max_chars=...)`. Omit `max_chars` to start with the default 20,000-character budget and increase it only when a broader context is necessary.
4. Check `complete`, `truncated`, `omitted_count`, `omitted_ids`,
   `omitted_ids_complete`, `returned_chars`, and `corpus_count` before
   interpreting the hits. The returned character count covers compact, whole
   result records; records are never partially sliced. Omitted ID samples are
   capped, so use `omitted_count` rather than the sample length as the total.
5. Inspect the highest-relevance active hits in each requested channel. BM25 relevance is primary; newer records come first only when scores are tied or nearly tied. Use `include_inactive=true` only when auditing superseded or explicitly inactive history.
6. If `truncated=true`, treat the response as partial: narrow the query or channel list, or deliberately retry with a larger `max_chars`. Never infer that an omitted fact does not exist.
7. Summarize the useful retrieved items and explain how they affect the current proof state.
8. If no useful item is found, say that clearly and then switch to another appropriate skill. Only describe the search as exhaustive when `complete=true`.

## Output Contract

Append a summary record to `events`:

```json
{
  "event_type": "query_memory",
  "query": "...",
  "channels": ["counterexamples", "failed_paths"],
  "limit_per_channel": 10,
  "results_summary": ["..."],
  "useful_hits": [
    {
      "channel": "counterexamples",
      "score": 0.0,
      "why_relevant": "...",
      "record_excerpt": "..."
    }
  ],
  "branch_id": "optional",
  "subgoal_id": "optional"
}
```

## MCP Tools

- `memory_search`
- `memory_append`

`memory_append` returns a compact metadata receipt with a `record_id` by default. Use that id in a later append's `supersedes` list when replacing a stale fact. Request `return_mode="full"` only when the complete just-written record is genuinely needed in the immediate context.

## Failure Logging

If the retrieval is not useful, append an `events` record with:

- `event_type="query_memory_stalled"`
- the attempted query
- the channels searched
- the reason the retrieved items were not useful

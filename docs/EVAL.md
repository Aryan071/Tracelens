# Evaluation of the explanation layer

What is measured, how, what the numbers are, and what they do not show. Reproduce with
`make eval-cases` (regenerate the documents), `make eval-template`, and, with your own key,
`ANTHROPIC_API_KEY=... make eval-llm`.

## The evaluation set
13 Evidence documents in `explainer/eval/cases/`, produced by the real engine (`analyze_pair`, `analyze_errors`) from synthetic request data, one per failure shape: a synchronous call in a hot function, N+1 queries, a dropped index (with and without an EXPLAIN plan), a new query on the hot path, two queries changing at once, two commits that cannot be told apart, a regression no commit explains, an unknown commit range, no control endpoints, and three error cases (new error with a suspect commit, without one, and from an unmappable frame). Each case lists the **key points** a correct explanation must state (for example for the dropped index: the endpoint, the migration commit, the index name, the table, the sequential scan), written by hand from how the scenario was built. Each point accepts several spellings and is matched case-insensitively against the summary and claims.

## Results

### Deterministic template (no model), `make eval-template`
Run 2026-10-10. Final explanation key-point recall: **100% (13/13 documents, 42/42 key points)**. The template passes the same validator as a model's answer on all 13 documents (`explainer/tests/test_service.py`). It reached 92% before three real gaps were fixed: it dropped the engine's own caveats (no control endpoints), the throwing function's name, and the query text. These are changes to what the template says, made because a reader needs those facts, not changes to the expectations.

### The validator against corrupted answers, `explainer/eval/mutations.py` (no model)
Each case's valid explanation with one claim corrupted the way a model plausibly goes wrong:

| Corruption | Answers | Rejected by the validator |
|---|---|---|
| a citation ID that does not exist | 13 | 13 (100%) |
| a citation ID with a typo | 13 | 13 (100%) |
| a number changed (x1.37) | 13 | 13 (100%) |
| citing a different real item than the claim is about | 11 | 11 (100%) |

### Live model, `make eval-llm`
**Not yet measured.** No Anthropic credentials were available in the environment this was built in, and a simulated model would measure nothing, so there is no model result to report. The runner records, per document and in total: whether the first answer was valid without a retry, the share of claims whose citations all exist, the share whose numbers are all in the cited items, retries, template fallbacks, key-point recall of the final text and of model-written text only, and latency. Run it with a key and paste `docs/EVAL-llm.md` here.

## What the numbers do and do not show
- The template's 100% is recall of facts the evidence contains, from expectations written by the same person who wrote the scenarios. It shows the template is complete for these shapes, not that it generalizes to evidence shapes nobody listed.
- The validator checks form and grounding, not truth: it rejects invented IDs and invented figures, and cannot reject a true number attached to the wrong conclusion, a wrong causal claim made from real items, or a claim that cites a correct item but says something the item does not. Key-point recall (and reading the output) is what covers that, and only a live run measures it for a model.
- Mutations are single-claim and synthetic; "cites a different item" is caught here because the other item lacks the claim's number, and would pass if it happened to contain it.
- The documents come from synthetic request data with the real engine, not from production traffic.

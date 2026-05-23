# Real Answer-Level Eval — Design Sketch

**Status:** Sketch / notes for later. NOT a committed plan.
**Date:** 2026-05-22
**Context:** Written after the embedding-v2 sanity sweep + `/api/research` smoke test.
Records why the current sweep does not validate the product claim, and what a
real eval would need. Companion to the current interim eval:
`2026-05-21-retrieval-eval-corpus-design.md`.

---

## The problem the current eval cannot solve

Harbor Clerk's core claim: *a frontier model, via the MCP server, can reliably
search the corpus and produce complete, correct, grounded answers.*

The current sweep validates none of that. It is **circular**: phase 1 records
what the frontier model (Sonnet) cited; `--mode retrieval-eval` then scores
whether HC's `/api/search` resurfaces those same documents. The frontier
model's own output is the gold standard — so the eval can only measure "does
retrieval surface what a capable agent wanted," and only imperfectly. It can
never detect the agent being *wrong*: a hallucinated fact or a missed thread
still scores fine.

Two distinct validations are conflated, and only the first is (partially) done:

- **Retrieval validation** — does HC surface the right documents? *(current eval, roughly)*
- **Answer validation** — is the model's answer correct, complete, grounded? *(not done at all)*

The product question is the second one.

---

## What a real eval must measure

Per question, against **external ground truth** — not the model's own output:

| Property | Definition | How |
|---|---|---|
| Correctness | Factual claims match known-true answers | Judge / exact-match vs. an answer key |
| Groundedness | Every claim is cited and the cited doc supports it | Automated claim -> citation -> support check |
| Completeness | Answer covers the key facts / surfaces the key docs | Judge vs. a curated key-facts list |
| Tool-use quality | Right tools, good queries, no premature give-up | Audit the persisted tool transcript |
| Honest failure | On not-in-corpus questions, declines vs. confabulates | Negative test cases; check for refusal |

Groundedness is the highest-value automatable property — for a document
appliance, a confident *ungrounded* answer is the number-one failure mode.

---

## The central new artifact: a ground-truth set

~30-50 Q&A items spanning all three corpora (CUAD legal PDFs, Enron email,
synthetic bilingual). Each item:

- `question`
- `answer_key` — the known-correct answer, or a list of must-include facts
- `gold_docs` — the document(s) that actually contain the answer (filename
  stems; stable across re-ingest)
- `type` — one of `lookup` (single fact, checkable), `synthesis` (multi-doc
  research), `stats` (corpus / entity aggregates), `negative` (answer not in
  the corpus)

This is the expensive part — it needs human authoring and verification — but
it is the only thing that makes "correct" measurable. Start small (10-15, one
corpus) and grow.

---

## Mechanics

- **Persist the tool transcript.** Today `claude_baseline.py` saves only
  `tool_call_count`. Save the full tool-call sequence (tool name, args, result
  summary) per question — without it, tool-use quality is unmeasurable. The
  sanity run already showed wild variance (2-28 tool calls / question) that
  cannot be diagnosed from a bare count.
- **LLM-as-judge for answers.** Reuse the sweep's phase-5 judge machinery.
  Judge `(answer, cited docs, answer_key)` for groundedness / completeness /
  correctness. Pin one judge model across runs so scores stay comparable.
- **Route metrics by question type.** recall@K is meaningful only for `lookup`
  questions (few gold docs). For `synthesis` (baselines cite 50-215 docs ->
  recall@10 structurally caps near 0.1) and `stats` (0 citations -> silently
  dropped today) it is broken — use judge scores instead. The real eval must
  score the 0-citation questions, not drop them.
- **Breadth for "reliably."** >=2 frontier models (e.g. Sonnet + a GPT + a
  Gemini), all three corpora, repeated runs to measure variance. n=16 / one
  model / one corpus cannot support a reliability claim.

---

## What to keep

- The sweep harness, the three corpora, the question banks.
- `--mode retrieval-eval` stays valid as a *pure retrieval* check (recall / MRR
  vs. a gold-doc set) — just label it honestly as retrieval-only, not answer
  validation.
- Title-based matching (`58b5c04`) and the `--no-ingest` flag.

---

## Suggested phasing (cheap -> expensive)

1. **Now (~$1-2):** point the phase-5 judge at the existing 16 frontier
   baselines -> first groundedness / completeness signal, no new infra.
2. **Small:** add tool-transcript persistence to `claude_baseline.py`.
3. **Medium:** author a 10-15 item ground-truth set (one corpus first), wire
   judge-against-answer-key.
4. **Larger:** expand to 30-50 items across all corpora; add negative cases;
   add a second frontier model; add variance runs.

---

## Open questions

- Judge model: same Sonnet generation, or a different / stronger judge for
  independence from the model under test?
- How exhaustive must `gold_docs` be for completeness scoring — an exact set,
  or "must include at least these N"?
- Groundedness granularity — per sentence, per paragraph, or per cited claim?
- Is a true "thoroughness" measure (did it find *everything* relevant)
  feasible without near-exhaustive corpus labeling, or is judge-based
  completeness a good-enough proxy?

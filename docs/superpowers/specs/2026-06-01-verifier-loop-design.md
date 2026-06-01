# Verifier-Loop Pass for Research Synthesis — Design

> **Status:** spec, not yet implemented. Linked from the 2026-05-31 v3-sweep readout follow-ups.

## Goal

Close the local-vs-cloud groundedness gap (≈0.72 on enron and synthetic; final readout shows local trails Sonnet 4.10 vs. 3.66 for the best local average). Do it without compromising correctness — the same v3 data shows correctness is already within 0.4 points of Sonnet on the top models.

The mechanism: after research synthesis emits a draft report with `[Title, page]` citations, run a **verifier loop** that re-reads each cited passage and judges "does this passage actually support the claim that referenced it?" Any mismatches feed back to a single revision pass.

## Why this lever, not the others

The brainstorm map covered six possible levers (more gap rounds, more queries per round, self-critique, verifier loop, multi-angle synthesis, semantic coverage replacement). Verifier wins on:

1. **Targets the specific gap** — local groundedness, not local correctness. Self-critique helps both; verifier hits grounding directly.
2. **Bounded cost** — work scales linearly with citation count (typically 5-20 per report), not with corpus size or question difficulty.
3. **Independent of model size** — works the same on qwen3-8b and qwen36-35b-a3b.
4. **Easy to A/B** — same eval harness, flag-gated, compare grnd Δ on the existing v3 captures.

## Design

### Where it hooks in

In `src/harbor_clerk/llm/research.py::research_stream`, between the existing synthesis step (after `_build_synthesis_messages` → LLM produces draft) and the SSE `done` event.

The pipeline becomes:

```
plan → search → read → extract → synthesize → VERIFY → REVISE (if needed) → done
```

### Claim granularity: per-citation

A "claim" is the sentence (or two-sentence span) that contains an inline citation marker `[Title, page]`. The verifier processes one claim per cited passage.

Rationale: this matches how the judge in `tests/test_corpora/runner/answer_judge.py` scores groundedness — it inspects each cited title against the model's answer. Per-citation granularity also bounds the verifier's work to O(citations), typically 5-20 per report. Anything finer (per-clause) explodes the cost; anything coarser (per-paragraph) loses the precision that makes the verifier useful.

### Verifier prompt shape

For each cited claim, send the LLM:

```
TASK: Judge whether the cited passage supports the claim.

CLAIM:
{claim_sentence}

CITED PASSAGE (from {doc_title}, page {page}):
{passage_text}

Reply with ONLY a JSON object:
{"verdict": "supported" | "partial" | "unsupported", "reason": "<one sentence>"}
```

- `supported`: claim is directly stated or clearly entailed by the passage.
- `partial`: passage relates to the claim but is missing a key element (a number, a date, a name).
- `unsupported`: passage does not address the claim.

### Revision pass

If any verdict is `unsupported` or `partial`, the verifier emits structured feedback and the synthesizer is called once more with:

```
ORIGINAL QUESTION: {user_question}

PREVIOUS DRAFT:
{draft_report}

CITATION VERIFICATION FEEDBACK:
- [{doc_title}, p{page}] verdict: {partial|unsupported}
  Reason: {reason}
- ...

Revise the draft to fix the cited-claim mismatches. You may:
- replace a problematic citation with a different one that genuinely supports the claim
- soften or remove a claim that has no supporting evidence
- drop the citation if the surrounding prose stands on its own
Do NOT introduce new claims or new citations that the verifier hasn't seen.
```

The "Do NOT introduce new claims" constraint keeps the second pass focused on correction, not expansion. We don't want a self-amplifying drift where each round adds new unverified content.

### Iteration policy

Single revision pass only. If the verifier still finds mismatches in the revised draft, that's the final output — we log the residual issues but don't loop again.

Rationale:
- Two-pass already roughly doubles wall time. Three-pass is rarely worth it in the published verifier-loop literature, and we have no signal yet that our domain warrants more.
- Adaptive looping (continue until no mismatches) opens the door to pathological infinite loops on models that struggle with the verifier prompt. Hard cap of 1 revision is the conservative starting point.

### Gating: when to run the verifier

Configuration:
- `research_verifier_enabled: bool = False` (settings flag, default off until A/B validated)
- `research_verifier_models: set[str] | None = None` (when set, only these model ids get verified; None = all models when enabled)

When `enabled=True`:
- Run on every research call regardless of how short the draft is. (Even 3-citation reports benefit.)
- Skip if the draft has zero citations (no claims to verify).

### Wall-time budget

Estimated cost per question:
- Single citation verification: 1 LLM call, ~200-500 tokens prompt + ~30 tokens response → ~5-10s on qwen36-35b
- 10 citations × 5-10s = 50-100s of verification
- Revision pass: 1 LLM call, full draft + feedback → 30-60s
- **Total added per question: 1.5-3 min**

Current research runs at ~1-3 min per question. With verifier: 2.5-6 min. Well under the 30-min budget cap.

The verifier passes can be parallelized across citations (independent calls), which would cut the verification phase to one LLM-call latency. Defer parallelization to v2; ship sequential first.

## A/B methodology

Re-run the v3 sweep with `research_verifier_enabled=True` on the top 3 local models — qwen36-35b-a3b, gemma4-26b-a4b, gpt-oss-20b — across all three corpora (cuad, enron, synthetic). 9 cells.

Compare against the v3 baseline (already captured at `~/Library/Application Support/Harbor Clerk/test-corpora/results/2026-05-31-local-aeval-v3/`).

Success criteria:
- **Primary:** average groundedness delta ≥ +0.30 across the 9 cells. (Need to close ~0.72; +0.30 is a meaningful first step. A larger delta is welcome but +0.30 justifies shipping.)
- **Guard rail:** correctness delta no worse than -0.10. (Verification + revision should not degrade correctness; if it does, the revision pass is over-correcting.)
- **Guard rail:** wall time per question ≤ 6 min average. (Anything longer means either too many citations per draft or per-citation latency is mistuned.)

If primary success criterion clears, ship verifier-enabled by default for the curated set. If only the larger 2 models clear it, ship gated to `research_verifier_models = {qwen36-35b-a3b, gemma4-26b-a4b}`.

## Files to touch

- `src/harbor_clerk/llm/research.py`: new functions `_verify_citations(draft, sources)` and `_revise_with_feedback(draft, feedback)`; integration in `research_stream` between synthesis and the final SSE `done` event
- `src/harbor_clerk/llm/research_prompts.py` (new file): the verifier system prompt and revision prompt, kept separate from the synthesis prompt so prompt-tuning is independent
- `src/harbor_clerk/config.py`: add `research_verifier_enabled: bool = False` and `research_verifier_models: set[str] | None = None` settings
- `frontend/src/pages/ChatPage.tsx`: surface a new SSE event type `verifier_pass` showing per-citation verdicts (collapsed by default; expandable in the research panel)
- `tests/test_research_verifier.py` (new): unit tests for `_verify_citations` claim extraction, prompt rendering, and the gating logic
- `scripts/test_corpora/runner/sweep.py`: a `--verifier` flag that toggles the setting before each cell

## Out of scope / Deferred

- **Parallel citation verification.** Independent calls per citation are trivially parallelizable but add complexity around llama-server's per-slot scheduling. Sequential ships first; revisit after the A/B if wall time is the binding constraint.
- **Self-critique pass.** Different lever (catches missing claims, not unsupported ones). Worth a separate spec after this one ships.
- **Multi-claim-per-citation handling.** Some sentences cite the same source twice for two related claims. v1 treats each citation independently; v2 could group them.
- **Verifier judge model selection.** v1 uses the same active model for the verifier call. Could in principle use a smaller/cheaper model as the verifier (Sonnet-style cross-model judging is the canonical approach). Defer until we see whether qwen36 self-verifies adequately.
- **Citation rewriting vs. claim deletion.** v1 lets the synthesizer pick between replacing the citation, softening the claim, or dropping it. Could be made more directive ("always prefer replacing"), but the synthesizer has more context than the verifier on which option is right for a given mismatch.
- **Adversarial questioning** (from the brainstorm). Different lever. Parked.

## Open questions

1. **Verifier prompt tuning.** The three verdicts (`supported`/`partial`/`unsupported`) are a first cut. Could be richer ("supported but tangential", "supported but the model misstated the source's framing"). Start with three; widen if the A/B shows the binary supported/not-supported is too coarse.
2. **Streaming UX.** During the verifier phase, the user sees no token stream because the verifier calls don't go through the synthesis path. Need a progress indicator — probably `verifier_pass` SSE events with `{citation_index, total, verdict}` shape.
3. **What happens on JSON parse failure** in the verifier output. v1 should treat unparseable verdict as `supported` (fail-open) so a brittle JSON output doesn't trash a real report. Log and continue.

## Implementation order (rough)

1. Add settings flag + plumb through to research.py
2. Implement `_verify_citations` returning a list of verdict dicts (without revision)
3. Add the `verifier_pass` SSE event so the frontend can see verdicts
4. Implement `_revise_with_feedback` and the conditional revision call
5. Add unit tests
6. Wire `--verifier` flag into the test_corpora sweep harness
7. Run the A/B on 9 cells, evaluate against success criteria
8. Decide: ship enabled-by-default, ship gated, or iterate on the design

---

**Companion docs:**
- 2026-05-31 v3 sweep final readout (in conversation)
- `pr_followups.md` Performance section (#220 adaptive gap rounds — companion lever, deferred)
- `project_hc_chat_tool_budget.md` (per-slot context implications for the verifier's prompt budget)

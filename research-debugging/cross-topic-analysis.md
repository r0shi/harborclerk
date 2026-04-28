# Cross-topic / cross-model analysis (3 topics × 6 models = 18 runs, all standard depth, 30 min)

**Generated:** 2026-04-28
**Inputs:** the wine question (already analyzed in [`comparison-top2-vs-baseline.md`](comparison-top2-vs-baseline.md)) plus 18 standard-depth runs across the cheese / seafood / sourdough topics.

## Headline result

The fix-set in PR #218 generalizes. **Every one of the 18 cross-topic runs completed cleanly** (one transient connection error on cheese × Qwen3.6 35B-A3B was a one-shot retry away from succeeding). No silent empty reports, no thinking-spillage failures, no hangs. The pipeline is now reliable.

What the cross-topic data exposes are **product-level quality differences** that are now actionable because the plumbing is no longer in the way. Most of the gap between the local-LLM output and the Claude-baseline output is now in synthesis depth and citation discipline — not in retrieval coverage and not in completion reliability.

## Per-model summary (across all 3 topics)

| Model | Mean report (B) | Mean citations | Mean notes (B) | Notes |
|---|---:|---:|---:|---|
| **GPT-OSS 20B** | 6,573 | **27.0** | 17,664 | Most rigorous citing; uses markdown tables on every comparison-style question; longest mean report |
| **Qwen3.6 35B-A3B** | 6,508 | 18.0 | 12,951 | Strongest narrative structure (intro / executive summary / themed sections); especially good on history-shaped topics (sourdough) |
| **Gemma 4 26B-A4B** | 4,764 | 16.7 | 12,199 | Consistently solid; somewhat terse |
| **Qwen3 8B** | 5,447 | 12.3 | 11,120 | Mid-pack across all metrics |
| **Qwen3 4B** | 4,370 | 12.0 | 11,972 | Smallest among the credible cohort; good citation density for its size |
| **SmolLM3 3B** | 37,709 | **8.0** | 46,178 | Outlier — verbose in length, but **0 citations** on cheese and seafood; report content frequently confabulates from query keywords |

## Patterns that argue for changes

### 1. **Synthesis is undershooting the available budget**

Synthesis cap is 10 000 tokens. Mean report length across the *credible* models (excluding SmolLM3) is ~1 400 tokens. Models are stopping confidently before the cap, but the resulting reports are noticeably thinner than what the notes contain (typical notes are 9 000–17 000 chars = ~3 000–5 000 tokens, suggesting at least 2× more content was available than the synthesis surfaced).

**Suggested change:** strengthen the synthesis system prompt to require a more thorough report. Specifically, on a "compare X" question, ask for at least one paragraph per region/tradition/case identified in the notes, rather than letting the model decide its own granularity. A simple addition along the lines of *"For each distinct region/tradition/case mentioned in the notes, write a dedicated section. Do not collapse multiple subjects into a single sentence."* would push the synthesis density up. Avoid hard length-mandates ("write at least 3 000 words"); they degrade quality.

### 2. **Citation discipline is wildly inconsistent across small models**

SmolLM3 3B produced **zero citations** on cheese and seafood despite being asked to cite, and **24 citations** on sourdough. The cheese report opens by hallucinating "Argan and Levain cultures" as a cheese-making tradition — those are corpus topic labels (an olive-oil place name and the sourdough starter) that leaked into the seeded queries. SmolLM3 then confabulated cheese context for them rather than treating the absence-of-evidence as a signal.

**Suggested changes:**

- **Synthesis prompt: require explicit "no-evidence" language.** If a region/topic appears in the planned queries but not in the actual notes, the synthesis should say so, not invent. An additional rule along the lines of *"You may only make claims that appear in the notes section between `<notes>...</notes>` markers. If the planned queries mentioned a region that is not present in the notes, briefly state that the corpus did not yield evidence for it. Never substitute general knowledge for a missing corpus citation."* would help.

- **Note-extraction prompt: require empty-result honesty.** If the passages don't actually discuss cheese (because seeded queries pulled wine/oil docs), the note extractor should say so rather than producing notes that look generic. A rule like *"If the passages do not contain information relevant to the research question, return: 'No relevant findings in this passage set.' Do not extract content that is not on-topic."*

- **Possibly: skip SmolLM3 from the default research strategy.** Per [`models.py`](../src/harbor_clerk/llm/models.py), models <4 GB use the `sweep` strategy — SmolLM3 is included. Given its tendency to confabulate without citations, defaulting it away from research mode (or warning the user) might be the right product call.

### 3. **GPT-OSS 20B's table format is the gold standard for compare-style questions**

GPT-OSS produced **18 markdown tables across cheese, 7 across seafood, 6 across sourdough** — an average of 10 tables per report. Every other model produced **zero tables** across all 18 reports. Tables make the comparison much easier for a reader to scan. This is not just a stylistic preference: looking at the raw cheese reports, GPT-OSS's `| Theme | Key Findings | Source |` table covers more region/tradition pairs in 2 KB than Qwen3.6's narrative does in 4 KB.

**Suggested change:** detect comparative questions (heuristic: the question contains "compare", "differences", "vs", "across", "different X and Y") and append a directive to the synthesis system prompt nudging toward tabular structure. *"For comparative questions, prefer markdown tables for the substantive content. Use one row per region/tradition/case, with columns for the dimensions being compared and a Source column for citations."* Models that don't natively use tables (Qwen3, Gemma) will still produce a usable report; models that do (GPT-OSS) will lean into their strength.

### 4. **Sourdough surfaces the draft-spam pattern across all models**

The corpus has 10+ near-duplicate drafts of the `painaulevain` series. Notes lengths on sourdough are conspicuously high for some models (GPT-OSS: 33 777 B, **3.4× its mean**; SmolLM3: 44 823 B). Most of that extra content is the model dutifully extracting the same fact from `painaulevain4`, `painaulevain5`, `painaulevain5WY`, etc., as if they were independent sources. Citation counts confirm it: GPT-OSS's sourdough report has 21 citations to ~4 unique pieces of content.

**Suggested change:** **before note extraction**, deduplicate near-identical draft passages by content hash + title prefix similarity. Concretely: in `_read_evidence` (research.py), after the per-passage read, hash the first 500 chars of each passage and group by hash; for groups of ≥2 with overlapping doc-title prefixes (`painaulevain*`, `Christoffel-*`, `terroir*`, `Yarra Valley *`), keep one representative (the highest-score one) and merge the others into a single citation as *"also in `<title-pattern>` versions 1, 4, 5"*. This is a targeted, conservative change — won't affect single-version documents, won't cross-contaminate unrelated docs.

### 5. **Wall-time on small/medium models is well under the 30 min budget**

Run times on the credible-cohort models cluster around 60–95 seconds total. The 30-minute budget is doing nothing on these models. Given how short reports are (point #1), a configurable knob to **add a third research round at standard depth** for small/medium models — when wall-time used so far is < 50 % of budget — would let the time budget actually buy more coverage. The existing thorough depth has `gap_round=True` *and* `paginate=True` and `max_passages=100`, but only one gap round still.

**Suggested change:** in `research.py`, after the existing single gap round, if `elapsed < time_limit_s * 0.4` and the previous gap round produced ≥1 new document, run a second gap round. Keep depth=`thorough` as the more aggressive ceiling.

### 6. **Reasoning-content fallback (F19/F22) is doing real work**

Looking at notes lengths on the heavy-reasoning models (GPT-OSS especially), several runs would have been empty without the F19 fallback that uses `reasoning_content` when `content` is empty. The fallback isn't a degraded path in practice — for GPT-OSS specifically, the structured "thinking" the model emits *is* the content, just routed through a different field. The current code logs a WARNING when the fallback fires; over time, that warning can be downgraded to INFO once we confirm it's a normal path on certain models, not an aberration.

### 7. **Qwen3.6 35B-A3B is the best narrative-history reporter**

On sourdough specifically, Qwen3.6's report opens with an **executive summary**, then a structured *"Pre-WWI Dominance and Post-War Decline"* section that reads like editorial copy: it identifies Raymond Calvel as a key figure, traces the post-WWI shift to commercial yeast and the baguette, distinguishes solid vs liquid levain, and notes the San Francisco branch of the tradition — all with `painaulevain` citations. This is the kind of report you'd publish, not just a fact dump. GPT-OSS 20B's table format is more useful for tabulated comparisons, but Qwen3.6 wins on history-shaped questions.

**Suggested change:** none, this is an emergent strength of the model. Worth documenting in the README as the recommended pick for narrative summaries.

## Specific changes to consider for a follow-up PR

| Idea | Where | Effort |
|---|---|---|
| Strengthen synthesis prompt: per-section rule + no-fabrication rule | `_SYNTHESIS_SYSTEM` in [`research.py:39`](../src/harbor_clerk/llm/research.py:39) | small |
| Strengthen note-extraction prompt: empty-result honesty | `_NOTE_EXTRACTION_SYSTEM` in [`research.py`](../src/harbor_clerk/llm/research.py) | small |
| Auto-table-format hint for comparative questions | new helper in `_build_synthesis_messages` | small |
| Draft-content dedup before note extraction | new pass in `_read_evidence` | medium |
| Optional second gap round when wall-time is under-spent | conditional block in `research_stream` | medium |
| Document Qwen3.6 35B as best narrative model in models.md / README | docs | trivial |
| Consider warning on SmolLM3 for research mode (or default to chat-only) | `default_research_strategy` or new `supports_research` flag | small |

## Pipeline reliability scorecard (post-PR #218)

| Concern | Status |
|---|---|
| Models silently produce empty reports | **FIXED** — F19/F22 fallback + per-call logs catch all cases |
| Mid-research API hangs | Not observed across 18 runs |
| Model switches don't take effect | **FIXED in Swift** (this PR) — mtime poller catches it within 3 s; further hardening for the port-bind / orphan-process race documented as follow-up |
| Reports tagged with wrong model_id | **FIXED** (F23) — read from message |
| Per-phase visibility | **FIXED** — INFO logs include phase, elapsed, prompt+completion tokens, cap-hits |

## Known issues remaining (for the follow-up PR)

1. **Swift's `llamaService.stop()` doesn't always terminate the underlying process** — the orphan kept the port and caused subsequent restarts to fail. Mitigated by the sweep script's defensive force-kill; needs a proper Swift fix that confirms exit before returning from `stop()`.

2. **Brief llama unreachability mid-synthesis can mark a research as `interrupted`** — happened once on cheese × Qwen3.6 35B-A3B (succeeded on retry). The synthesis HTTP path retries on 5xx but not on `ConnectError`. Adding a single retry on `ConnectError` (with 2 s backoff) would protect against transient unreachability when llama is between requests.

3. **DispatchSource-based config watcher still drops events** — root cause not yet diagnosed. The 3 s mtime poller saves us in practice. Further investigation could either fix the dispatch source (and remove the poller) or just delete the dispatch source and rely on the poller exclusively.

## Files for next-PR consumption

- `cheese/<model>-standard-30m.md` × 6 models — full reports
- `seafood/<model>-standard-30m.md` × 6 models — full reports
- `sourdough/<model>-standard-30m.md` × 6 models — full reports
- `cheese|seafood|sourdough/<model>-standard-30m.detail.json` × 18 — full API state including notes, tool calls, per-message metadata
- This file

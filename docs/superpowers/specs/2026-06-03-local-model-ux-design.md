# Local Model UX and Private AI Framing - Design Spec

**Date:** 2026-06-03
**Status:** Accepted for planning
**Scope:** Model selection, onboarding, model labels, warnings, eval disclosure, and the in-app wording for local AI. This spec does not change model-serving internals.

## Overview

The recent eval and implementation work changes the product read: curated local models are not just a demo. They can produce useful cited answers on some document tasks, especially after weaker models were removed and prompts/tools were tightened. The UI should therefore present local AI as a real product capability, while staying honest about task and model limits.

The phrase "assistant" should be avoided. It is too broad and too generic. For now, use "private AI," "local AI," "cited answers," or "research" depending on the surface. "Private AI" is not perfect, but it is acceptable placeholder copy.

## Goals

- Ask the user to choose/download a model during startup/onboarding, not silently auto-select one.
- Warn on weaker/smaller models instead of hard-blocking them.
- Present curated models with qualitative labels that map to real user expectations.
- Surface model identity in Ask and Research without turning normal use into a benchmark dashboard.
- Keep eval scores out of primary UI unless they are qualitative and stable.
- Explain local/cloud boundaries clearly.
- Leave room for a future cloud-model in-app mode that keeps Harbor Clerk citations first-class.
- Define a clear go/no-go bar for whether cloud-model in-app mode belongs in the initial release.
- Make display-only verifier/validator output a candidate default-on citation-support signal, without presenting it as a calibrated correctness score.

## Non-goals

- No new model benchmark publication system in this scope.
- No automatic model download before user consent.
- No hard quality gate that prevents users from choosing a smaller curated model.
- No "frontier quality on your Mac" claim.
- No cloud-provider integration implementation in this spec.

## Startup and onboarding

At initial launch, Harbor Clerk generally does not have models downloaded. Model setup should be explicit:

- During setup, after folder selection or during the first AI action, ask the user to choose a local AI model.
- Explain storage size, expected speed tier, and recommended use in plain language.
- Allow "skip for now" if the user only wants search/browse.
- If no model is configured and the user opens Ask/Research, show a focused setup prompt.

Do not auto-download a model on first launch. The download is large enough and operationally meaningful enough that user consent matters.

## Model labels

Recommended initial labels:

- Qwen3.6 35B-A3B: Best local research.
- Gemma 26B-A4B: Balanced local research.
- GPT-OSS 20B: Strong tables and comparisons.
- Qwen3 8B: Lightweight lookup.
- Qwen3 4B: Smallest usable model.

The exact model IDs can be visible in technical detail, but the main label should describe the job the model is good at. This is especially important for less technical users who do not know what a model family or parameter count implies.

## Warnings

Warn, do not block:

- Smaller models may miss details in long or messy corpora.
- Some models are better for quick lookup than multi-step research.
- Important answers should be verified from citations.
- Research mode may be slower or less complete on smaller models.

Warnings should appear:

- In model selection cards.
- In Ask/Research model tooltips or model selector detail.
- In docs, with a calmer and more complete explanation.

Avoid modal warnings that make normal use feel broken.

## Ask and Research surfaces

Ask and Research should show the active model in a compact way:

- Model name in the header or composer area.
- Tooltip or popover with label, local/cloud status, and warning tier.
- Clear action to change model, probably linking to Settings -> Models.

The answer UI should keep citations visually primary. Model identity is helpful context, but citations are the trust mechanism.

## Qualitative tiers

Use qualitative tiers until benchmark publishing is automated enough to avoid stale public numbers:

- Best local research.
- Balanced.
- Lightweight.
- Experimental or not recommended for Research, if any future model warrants it.

If numeric evals appear in docs, they should live in eval docs, not the main product UI.

## Boundary disclosure

Every model surface should distinguish:

- Local AI: prompts and retrieved snippets stay on the machine.
- MCP/cloud model access: cloud model receives only the tool responses/snippets allowed by the API key and tool contract.
- Future in-app cloud mode: provider receives the selected prompt and retrieved context needed to answer.

Err on too much disclosure. This is a trust feature, not copy clutter.

## Future cloud-model in-app mode

A sophisticated-operator feature is worth designing separately: let users connect cloud LLMs directly to local Ask and Research while preserving Harbor Clerk's first-class citations.

Potential value:

- Frontier/cloud reasoning and synthesis quality.
- Harbor Clerk retrieval, source policy, and citations remain the grounding layer.
- Users can compare local and cloud answers over the same corpus.
- Useful for teams that already have API keys and understand cloud data boundaries.

Constraints:

- Requires provider API keys.
- May require valid HTTPS/TLS setup depending on transport.
- Needs extremely clear disclosure of what leaves the machine.
- Must use the same `SourceRef` contract so citations survive synthesis.
- Should have a per-run/model visible boundary indicator.

Recommendation: design this as a fast-follow after SourceRef and Search/Find All parity. It should not block the initial release unless the release is deliberately targeted at sophisticated operators only.

### Cloud in-app go/no-go

Cloud in-app models can be included in the initial public release only if the core path is boringly stable. Otherwise, keep cloud use to MCP/connector/operator docs and ship in-app cloud mode as a fast-follow.

Go criteria:

- `SourceRef` is implemented for the Ask/Research path, and cloud answers preserve clickable/inspectable citations without relying on brittle string parsing.
- The model selector clearly distinguishes local AI from cloud AI before the user sends a prompt.
- Provider API keys have an acceptable storage story for public release. On macOS, prefer Keychain or an equivalently explicit secure-storage path; do not bury keys in surprising plaintext locations.
- The UI has a per-run boundary disclosure: what prompt/context leaves the machine, which provider receives it, and whether source snippets or tool results are sent.
- Absolute paths are not sent to cloud-visible prompts/results unless a future explicit path-disclosure scope opts into it.
- Logs, errors, traces, and analytics do not accidentally persist provider keys or full prompt/context bodies.
- Network failures, invalid keys, rate limits, cancellation, and provider errors fail gracefully without losing the local answer/search workflow.
- At least one provider path has mocked tests plus a live smoke-test checklist that covers no-key, invalid-key, successful answer, citation rendering, long context, email/attachment citation, cancellation, and offline behavior.
- Cloud mode remains optional. Search, Documents, local AI, MCP, and CLI should not depend on it.

No-go / defer criteria:

- Citation chips require post-hoc parsing of the cloud model's prose.
- Data-boundary copy is ambiguous enough that "private AI" could be read as applying to cloud runs.
- Provider-key storage is not settled.
- Path disclosure policy is not enforced by tests.
- Cloud failures create confusing or broken states in Ask/Research.
- The release story starts depending on cloud mode to make local model quality look better.

"Stable" means the boring failure cases have been exercised, not merely that a happy-path API call works once.

## Citation-support indicator

The verifier/validator should not be marketed as a correctness score. It is not calibrated, and the 2026-06-01 A/B showed that using verifier feedback to revise reports was net-negative overall. However, display-only verifier verdicts may be valuable as a transparency feature if framed correctly.

Recommended label: **Citation support** or **Grounding check**, not "confidence."

Target default:

- Enable the display-only verifier by default for Research once the validation bar below is met.
- Keep the revision/rewrite pass disabled by default.
- Run the check as a trust signal for the user, not as an automatic answer modifier.
- If latency is noticeable, prefer showing the answer/report first with a "checking citations..." state and updating the badge when the check completes.
- Start with Research reports, where the verifier already has a report + citation structure. Extend to Ask after `SourceRef` gives the chat path equally reliable citation spans.

Possible stoplight:

- Green: cited claims checked; no partial or unsupported citations found.
- Yellow: some citations are partial, thin, skipped, or could not be checked.
- Red: at least one cited claim appears unsupported by the cited source.
- Gray: not checked, verifier unavailable, no citations, or check failed.

Rules:

- The indicator says whether citations appear to support cited claims. It does not say the whole answer is true.
- Show a compact global badge plus expandable per-citation verdicts and reasons.
- Keep revision disabled by default. The display signal and the rewrite loop are separate features.
- Fail open: if the verifier fails, keep the answer and show "not checked" rather than damaging a valid answer.
- Do not show percentages. A stoplight or short qualitative status is enough.
- Prefer deterministic checks first where possible: citation resolves, chunk exists, page/source exists, and source text is inspectable. Use the LLM verifier for support judgment, not basic provenance.

Go criteria for shipping the indicator:

- Human spot-checks or eval traces show the verdicts are directionally useful and not mostly noise.
- False red/yellow rates are low enough that the UI does not train users to ignore the badge.
- The UI copy makes the boundary clear: "citation support," not "answer correctness."
- The added latency is visible and acceptable, or the check runs after the answer/report is produced.
- Local and cloud model modes both produce compatible verifier inputs through `SourceRef`.
- Settings allow an advanced user to disable the citation-support check if the added latency or compute cost is undesirable.

If this bar is not met, do not make the check default-on. Keep verifier verdicts as an experimental diagnostics/eval feature until the signal is stable enough to help users rather than distract them.

## Release-blocking requirements

- Initial setup has a clear model selection/download path.
- Users can skip AI setup and still use Search/Documents/Folders.
- Ask/Research clearly show which model is active.
- Weaker curated models show warnings, not blocks.
- Public copy says local AI is useful but variable.
- "Assistant" language is reduced or removed from primary surfaces.
- Cloud in-app models are excluded unless the go/no-go criteria above are satisfied.
- Display-only verifier/validator output is targeted to be enabled by default as a citation-support signal once the validation bar is met.
- Verifier revision is not enabled by default; any verifier-derived UI is framed as citation support, not correctness confidence.

## Tests

- Frontend tests or manual coverage for no-model, model-selected, and model-download-in-progress states.
- Manual startup flow verification on a fresh install.
- Manual Ask/Research verification that model identity appears and does not crowd citations.
- Docs/copy review for local/cloud boundary clarity.
- Cloud in-app go/no-go checklist if cloud mode is considered for release.
- Verifier indicator validation if citation-support UI is considered for release.

## Open questions

- Whether "private AI" should become the stable label. Recommendation: acceptable for now, revisit after the larger copy pass.
- Whether to show model family names in the main label. Recommendation: yes, but pair them with plain-language use labels.
- Whether cloud in-app mode should be part of the first public release. Decision: no unless the explicit go/no-go criteria above are satisfied.
- Whether verifier output can become a default-on public-facing trust signal. Recommendation: yes for display-only citation support after validation; no for revision or numerical correctness/confidence scoring.

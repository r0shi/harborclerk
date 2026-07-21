# CLI vs JSON tool-call surface for small local models — experiment spec

**Status:** Deferred. Run after the next full research-harness baseline (the one that captures all the recent MCP fixes — PR-E/G/H/J/find_all). Picking up earlier would muddle the comparison.

## Motivation

The research harness currently exposes tools to LLMs as OpenAI-style function-calling JSON (`src/harbor_clerk/llm/tools.py:_BASE_CHAT_TOOLS`, ~2,000 tokens, 15 tools). Now that the CLI (`harbor-clerk <command>`, one command per MCP tool, full `--help` pages under `src/harbor_clerk/cli/help/`) exists, the question is whether small local models (Qwen3 4B, Phi-4 Mini, SmolLM3 3B) would perform better invoking tools as shell commands than as JSON.

## Hypothesis

Distribution shift, not context savings, is the real lever. Small models have seen vastly more `command --flag value | jq ...` patterns in pretraining than function-calling JSON dialogues. Prior work (CodeAct, Wang et al.) showed measurable gains from shell/code-shaped tool invocation on small models. The HC CLI is a near-ideal substrate because every command already returns JSON, so the response channel is unchanged — only invocation moves.

**Null hypothesis to beat:** modern instruct models (Qwen3, Phi-4, SmolLM3) already received explicit function-calling post-training, so the distribution gap may have closed. The experiment is to find out.

## Token accounting (already done)

| Surface | Tokens (approx) |
|---|---|
| Full MCP docstrings (Claude/Opus path) | ~5,400 |
| `_BASE_CHAT_TOOLS` (small-model path today) | ~2,000 |
| All 19 CLI help files combined | ~15,000 |
| Single CLI help file (e.g. `search.txt`) | ~740 |
| `harbor-clerk --help` top-level only | <200 (estimate) |

Dumping all help upfront is a loss. Progressive disclosure (`--help` top-level + drill-in on demand) lands around 1.5–2k for a typical run — same magnitude as today. **The context-savings argument is weak; do not rely on it.** The experiment is purely about capability.

## Experiment design

**Pick one weak model.** Qwen3 4B is the lead candidate — already in `llm/models.py`, reasonable inference cost, has shown room to grow in prior eval runs.

**Build a minimal CLI-faker tool layer.** Same backend functions as current tool dispatch; different surface:

- Model emits a string like `search "termination clause" -k 5` instead of a JSON tool call.
- Executor pattern-matches `<command> [args]`, validates against an allowlist (`search`, `read-passages`, `expand-context`, `get-document`, `list-recent`), calls the same Python function, returns the same JSON the tool call would have returned.
- One bootstrap message in the system prompt: the `harbor-clerk --help` top-level listing.
- Model can issue `harbor-clerk <command> --help` to fetch a single help page; that page enters context, no LRU eviction.

**Match the existing eval rig.** Reuse `scripts/test_corpora/runner/` with the same corpora, questions, judges, and groundtruth used in the post-fix baseline run. Two configurations:

- **Control:** Qwen3 4B + current `_BASE_CHAT_TOOLS` JSON tool calls.
- **Treatment:** Qwen3 4B + CLI-faker shell-command surface.

Same model weights, same temperature, same retrieval backend. Only invocation surface changes.

**Metrics:**

1. Eval pass rate (judged answer correctness — primary).
2. Tool-call success rate (well-formed invocations / total attempts).
3. Tokens consumed per question (input + output).
4. Number of tool calls per question.
5. Distribution of tool usage (which commands the model reaches for).

## Success criteria

- **≥10% absolute improvement** in eval pass rate over control → distribution-shift hypothesis confirmed, productize.
- **5–10% improvement** → ambiguous; re-run on a second model (Phi-4 Mini) before deciding.
- **<5% / wash / worse** → null hypothesis stands. Park the CLI surface as a CLI-only thing for human-driven agent harnesses (OpenClaw, Claude Code), not a research-harness route.

## Out of scope

- Productionizing the CLI surface inside the chat path. Experiment first; ship later only if results justify it.
- Cloud models (Claude, GPT). Their JSON tool-call training is mature; the distribution-shift argument doesn't apply.
- Free-form text output parsing. HC CLI already returns JSON; this experiment does not need to test scraping.
- Sandbox/security hardening of a real shell executor. The faker is regex-based command dispatch, not actual shell exec.

## Prerequisite

Wait for the next full research-harness baseline run (capturing PR-E/G/H/J/find_all/etc.). Comparing CLI vs JSON before that baseline lands would conflate two independent improvements. The post-baseline state is the new control.

## Reference: where this idea was vetted

Brainstorm conversation 2026-05-27. Three pieces of background that shaped the spec:

1. The full MCP spec is NOT the small-model baseline today — `_BASE_CHAT_TOOLS` already trimmed it. Any future framing of "save tokens" needs to compare against `_BASE_CHAT_TOOLS`, not against full MCP.
2. CodeAct-style results from 2024 are the closest prior art. Cite when writing this up.
3. The CLI's JSON-returning help pages are what makes this substrate cheap to test. If the CLI returned free-form text, this experiment would not be worth running.

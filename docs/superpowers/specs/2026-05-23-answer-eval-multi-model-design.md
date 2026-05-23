# PR-C — Answer-eval multi-model support (Provider protocol)

**Status:** spec — awaiting user review before plan + implementation
**Author:** Claude (with Alex)
**Companion docs:** [PR-A spec](2026-05-22-answer-eval-enron-design.md), [PR-B spec](2026-05-23-answer-eval-synthetic-design.md), [eval sketch](2026-05-19-real-eval-sketch.md)

## Goal

Extend `--mode answer-eval` to support a **second baseline model** (`gpt-4o`)
alongside Sonnet, so each PR-A/B corpus eval can be re-run head-to-head across
two frontier providers without forking the runner. Architecturally: introduce a
narrow **Provider protocol** under
`scripts/test_corpora/runner/providers/` (new package), refactor the existing
`claude_baseline.py` into a concrete `AnthropicProvider` implementing it, add a
parallel `OpenAIProvider`, and select the implementation via the model name in
the existing `--models` flag. Output paths already key on model
(`workdir/answer-eval/{captures,verdicts}/<corpus>/<model>/`), so reuse-by-default
keeps the Sonnet baselines from PR-A/B intact and GPT-4o results land in a
sibling directory.

This is the third piece in the answer-eval phase 2 arc:

- **PR-A** (Enron, merged in #385): extended `--mode answer-eval` to Enron + added the `find` qtype + deterministic coverage scoring.
- **PR-B** (Synthetic, in #386): extended to the synthetic corpus via sidecar-driven generator.
- **PR-C** (this PR): extends to a second frontier model via a Provider protocol so PR-A/B numbers can be reproduced head-to-head with GPT-4o.

## Non-goals

- **No judge changes.** The judge stays Sonnet-only across all providers — same
  judge means comparable scores. (Future: cross-judge eval is its own PR.)
- **No multi-provider runs in a single invocation.** The runner stays
  single-model per invocation. Multi-model is achieved by running the harness
  twice with different `--models` values. Answer-eval's existing per-model
  directory layout (`workdir/answer-eval/{captures,verdicts}/<corpus>/<model>/`)
  keeps parallel runs from clobbering each other; no path changes needed.
- **No HC chat/research integration.** The Provider protocol is designed to be
  reusable for the future "bring your own cloud LLM key" use case in HC's
  chat/research path, but THIS PR only ships the abstraction inside
  `scripts/test_corpora`. Lifting it into `harbor_clerk` proper is a separate
  effort with its own design review.
- **No model routing or fallback.** One provider per run; if the API errors, the
  run fails. (The existing `_with_anthropic_retry` retry budget extends to the
  Anthropic provider; an analogous `_with_openai_retry` is in scope for the
  OpenAI provider.)
- **No tool-schema convergence work.** Each provider translates the MCP tool
  list to its own native schema (Anthropic's `input_schema`, OpenAI's
  `parameters`). The tool *executions* go through the same `SyncMcpSession`.

## Why this architecture (Option 2: pluggable Provider protocol)

Two architecturally-distinct options were considered (transcript: user
explicitly chose Option 2 after confirming the abstraction is reusable for the
future HC cloud-LLM use case):

- **Option 1 — fork-and-extend `claude_baseline.py`:** Copy the file to
  `openai_baseline.py`, swap `anthropic` for `openai`, hand-edit the tool-call
  loop. Fastest to ship; bad once we want a third provider; locks PR-C's value
  to the eval harness specifically.

- **Option 2 — Provider protocol + concrete implementations:** Define
  `Provider` as a small Python `Protocol` (`run_question(...) -> BaselineResult`),
  refactor `claude_baseline.py` into `anthropic_provider.py` implementing it,
  add `openai_provider.py` as a peer. One factory function maps model name →
  provider instance. New providers (Gemini, Bedrock, Azure) become drop-in.
  More work up-front; pays back the moment we need a third provider OR want to
  lift the abstraction into HC's chat/research path.

The user confirmed Option 2 is reusable for the future HC cloud-LLM use case,
so the engineering investment is paid back in two places.

## Architecture

```
scripts/test_corpora/runner/
├── providers/
│   ├── __init__.py              # exports: Provider, BaselineResult, make_provider
│   ├── base.py                  # Protocol + BaselineResult dataclass (moved from claude_baseline.py)
│   ├── anthropic_provider.py    # AnthropicProvider — refactored from claude_baseline.py
│   ├── openai_provider.py       # OpenAIProvider — new, mirrors Anthropic's shape
│   └── factory.py               # make_provider(model: str, *, mcp_session) -> Provider
├── answer_eval.py               # _live_capture_fn uses make_provider() instead of importing BaselineGenerator directly
├── claude_baseline.py           # shim: re-exports BaselineResult + BaselineGenerator for back-compat (sweep.py)
└── sweep.py                     # unchanged — imports through the shim
```

**Key design decisions:**

1. **`Provider` is a `typing.Protocol`, not an ABC.** Duck-typed mocks in tests
   stay simple; no inheritance required. Mirrors how `AnswerJudge`'s client
   parameter is duck-typed today.

2. **`BaselineResult` dataclass stays as the universal return type.** Every
   provider returns the same shape: `answer`, `cited_doc_ids`, `cited_doc_titles`,
   `tool_call_count`, `tool_transcript`, `elapsed_seconds`, `model`, `timestamp`.
   This is what `answer_eval.py`'s judging loop reads, and what gets serialized
   to `baselines/<corpus>/<question_id>.json`. Zero schema change.

3. **`make_provider(model, *, mcp_session)` is the single dispatch point.**
   String matching: any model name starting with `claude-` → Anthropic; any name
   starting with `gpt-` or `o1-`/`o3-` → OpenAI. Unknown prefixes raise a clear
   `ValueError` with the list of supported prefixes. No registry config file —
   the prefix list is hand-curated and lives in `factory.py`. The factory
   constructs the underlying client (`anthropic.Anthropic()` or
   `openai.OpenAI()`) lazily inside the matched branch, so callers don't have to
   know which client class to instantiate. Both clients read their API keys from
   the standard env vars (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`) — same pattern
   as today's `_live_capture_fn`.

   For back-compat with `sweep.py`'s existing `BaselineGenerator(client=...,
   mcp_session=..., model=..., doc_ids_seen=...)` call sites, both provider
   classes accept a `client=None` keyword: when supplied, the factory's
   convenience role is bypassed and the caller-built client is used verbatim.
   This is how the shim keeps sweep.py untouched.

4. **`claude_baseline.py` becomes a thin re-export shim** to avoid touching
   `sweep.py`'s 30+ import sites in this PR. The sweep keeps importing
   `BaselineGenerator` from `claude_baseline`; the shim re-exports it from the
   new `providers.anthropic_provider` module. (A follow-up PR can migrate
   `sweep.py` to use `make_provider` directly, but that's a much larger blast
   radius — better to defer.)

5. **`SYSTEM_PROMPT` is shared.** Currently inlined in `claude_baseline.py`;
   moves to `providers/base.py` as `DEFAULT_SYSTEM_PROMPT`. Both providers use it
   verbatim. (The prompt-tuning experiment queued after PR-C may parameterize
   this; out of scope here.)

6. **OpenAI tool-call loop mirrors Anthropic's structurally** but uses OpenAI's
   message+tool_calls format. Both loops:
   1. Send the user question + tool list.
   2. Receive a response; check `finish_reason` (`stop` = done, `tool_calls` =
      execute tools).
   3. Execute each tool via the shared `SyncMcpSession`.
   4. Append `tool` role messages with results, loop back.

7. **Error handling** — both providers expose the same retry surface: a
   provider-specific `_with_<provider>_retry()` helper in `sweep.py`. Existing
   `_with_anthropic_retry` is renamed to `_with_provider_retry(client_kind, fn, ...)`
   that dispatches on the model name. (See "Failure paths" below for the
   error-class mapping.)

## File-by-file changes

### Created

- **`scripts/test_corpora/runner/providers/__init__.py`** — re-exports
  `Provider`, `BaselineResult`, `make_provider`, `DEFAULT_SYSTEM_PROMPT`.
- **`scripts/test_corpora/runner/providers/base.py`** — `Provider` Protocol
  (single method: `run_question(question, question_id, corpus) -> BaselineResult`),
  `BaselineResult` dataclass, `DEFAULT_SYSTEM_PROMPT` constant.
- **`scripts/test_corpora/runner/providers/anthropic_provider.py`** —
  `AnthropicProvider` class; constructor takes `(*, mcp_session, model="claude-sonnet-4-6",
  client=None, doc_ids_seen=None)` (client defaults to `anthropic.Anthropic()`
  on first use); body is the existing `BaselineGenerator.run_question` loop moved
  here verbatim. Keeps `.write()` as a `@staticmethod` for shim compatibility.
- **`scripts/test_corpora/runner/providers/openai_provider.py`** —
  `OpenAIProvider` class; constructor takes `(*, mcp_session, model="gpt-4o",
  client=None, doc_ids_seen=None)` (client defaults to `openai.OpenAI()`);
  tool-call loop adapted to OpenAI's API shape.
- **`scripts/test_corpora/runner/providers/factory.py`** — `make_provider(model,
  *, mcp_session, doc_ids_seen=None) -> Provider`; prefix-based dispatch;
  raises `ValueError` on unknown.
- **`scripts/test_corpora/tests/test_providers_factory.py`** — unit tests for
  factory dispatch + unknown-model handling.
- **`scripts/test_corpora/tests/test_openai_provider.py`** — unit tests
  mirroring the existing `claude_baseline` tests (mock the OpenAI client, test
  tool-loop + cited-doc capture + transcript shape + empty `(empty)` fallback).

### Modified

- **`scripts/test_corpora/runner/claude_baseline.py`** — becomes a 5-line shim:
  ```python
  """Back-compat shim — see providers/anthropic_provider.py."""
  from scripts.test_corpora.runner.providers.anthropic_provider import (
      AnthropicProvider as BaselineGenerator,
  )
  from scripts.test_corpora.runner.providers.base import (
      BaselineResult,
      DEFAULT_SYSTEM_PROMPT as SYSTEM_PROMPT,
  )
  __all__ = ["BaselineGenerator", "BaselineResult", "SYSTEM_PROMPT"]
  ```
  The shim preserves all existing imports in `sweep.py` and tests. The
  `AnthropicProvider` class keeps `.write()` as a `@staticmethod` for the same
  reason.

- **`scripts/test_corpora/runner/answer_eval.py`** — `_live_capture_fn` switches
  from `BaselineGenerator(client=anthro, mcp_session=mcp, model=model)` to
  `make_provider(model, mcp_session=mcp)`. The Anthropic client construction
  moves into the Anthropic provider's own factory branch; the OpenAI client
  construction lives in its branch.

- **`scripts/test_corpora/runner/sweep.py`** — `_with_anthropic_retry` renamed
  to `_with_provider_retry`; signature gains a `client_kind: Literal["anthropic",
  "openai"]` parameter; dispatches the exception classes accordingly. Existing
  call sites stay the same (the kind is derived from the model name).

- **`scripts/test_corpora/tests/test_claude_baseline.py`** — tests stay; they
  exercise the shim's re-exports, which in turn exercise `AnthropicProvider`.
  Imports remain `from scripts.test_corpora.runner.claude_baseline import ...`.

### Untouched

- `runner/answer_judge.py` — judge stays Sonnet-only.
- `runner/sampler.py`, `runner/state.py`, `runner/metrics.py`, etc. — no changes.
- All ground-truth YAML files and generators.
- All other tests except the two new provider test files.

## CLI surface

No new flags. `--models gpt-4o` Just Works:

```bash
# Same harness invocation as PR-A/B, just with a different model name
uv run --project scripts/test_corpora python -m scripts.test_corpora.runner.sweep \
    --mode answer-eval \
    --corpora synthetic \
    --models gpt-4o \
    --label pr-c-synthetic-gpt4o \
    --workdir "$HC_WORKDIR" \
    --api-base "$HC_API_BASE"
```

Multi-model runs are two sequential invocations with different `--models` and
`--label` values. Captures and verdicts already key on model
(`workdir/answer-eval/captures/<corpus>/<model>/<question_id>.json`,
`workdir/answer-eval/verdicts/<corpus>/<model>/<question_id>.json`) — no path
changes needed for parallel runs. Per-label reports live under
`workdir/answer-eval/reports/<label>/`.

## Failure paths

The OpenAI provider needs the same defensive surface as the Anthropic one:

| Failure | Anthropic today | OpenAI in PR-C |
|---|---|---|
| Rate limit | `RateLimitError` → backoff via `_with_anthropic_retry` | `openai.RateLimitError` → backoff via `_with_provider_retry` |
| Overloaded | `OverloadedError` → backoff | `openai.APIStatusError` (502/503) → backoff |
| Bad request | bubble up | bubble up |
| Tool call returns empty content | `(empty)` literal | same `(empty)` literal |
| Tool call raises | uncaught (bubbles to sweep retry budget) | same |
| Model omits tool args | `KeyError` → fix in judge-style defensive pattern | same |
| `finish_reason == "length"` (response truncated mid-tool-call) | not currently handled | log + treat as `end_turn` (best-effort answer) |

The truncation case is a new wrinkle for OpenAI — Anthropic's API rarely hits
context limits with our 8000-token cap, but GPT-4o's `max_tokens` semantics are
subtly different. PR-C's OpenAI provider treats `finish_reason == "length"` as a
soft end-of-turn with a logged warning, rather than crashing. (Spec'd defensively
based on judge's `_score()` precedent from PR-B's post-eval hardening.)

## Tests

| File | New test | Asserts |
|---|---|---|
| `test_providers_factory.py` | `test_factory_dispatches_claude_to_anthropic` | `make_provider("claude-sonnet-4-6", ...)` returns `AnthropicProvider` instance |
| `test_providers_factory.py` | `test_factory_dispatches_gpt_to_openai` | `make_provider("gpt-4o", ...)` returns `OpenAIProvider` |
| `test_providers_factory.py` | `test_factory_dispatches_o1_to_openai` | `make_provider("o1-preview", ...)` returns `OpenAIProvider` |
| `test_providers_factory.py` | `test_factory_raises_on_unknown_model` | unknown prefix → `ValueError` with supported prefixes in message |
| `test_openai_provider.py` | `test_openai_provider_parses_simple_answer` | mock OpenAI client → end_turn on first response → returns final text |
| `test_openai_provider.py` | `test_openai_provider_tool_loop_captures_cited_docs` | mock client emits one tool_calls round → cited_doc_ids harvested from MCP result |
| `test_openai_provider.py` | `test_openai_provider_transcript_records_each_call` | tool_transcript has one entry per tool_call with `tool`, `args`, `result_summary` |
| `test_openai_provider.py` | `test_openai_provider_treats_length_finish_as_end_turn` | `finish_reason == "length"` returns the partial answer without crashing |
| `test_openai_provider.py` | `test_openai_provider_falls_back_when_mcp_returns_no_content` | empty tool result → `(empty)` literal |
| `test_claude_baseline.py` | (existing tests, unchanged) | shim re-exports work; `BaselineGenerator` is still importable from old path |

Live validation: one end-to-end run against `synthetic` with `gpt-4o`,
re-using PR-B's frozen `synthetic.yaml`, expected to complete 29 items.

## Open questions / follow-ups

- **OpenAI's tool-call concurrency.** GPT-4o sometimes emits multiple
  `tool_calls` in parallel. PR-C executes them sequentially (same as Anthropic
  today). If `gpt-4o`'s parallel tool calling materially helps end-to-end speed,
  consider a `concurrent.futures` thread pool in a follow-up.
- **Cross-provider judge bias.** Sonnet judging GPT-4o's answers introduces a
  potential same-family bias against the cross-family case. Worth a future "swap
  the judge to GPT-4o" experiment for sensitivity analysis — out of scope here.
- **Lifting the Provider protocol into `harbor_clerk` proper.** The eval-harness
  Provider can serve as the proof-of-concept; once it's been used for 2+
  providers in the eval, lift it into `src/harbor_clerk/llm/providers/` for the
  "bring your own cloud LLM" use case.
- **Prompt-tuning experiment.** Queued after PR-C: tune HC's MCP system prompt
  to fix the negative-hedging finding that's now confirmed across CUAD, Enron,
  and synthetic. PR-C does NOT change the system prompt; that's the experiment's
  job.

## Validation plan

- All 317 existing tests pass + new tests pass.
- Ruff clean on every changed file.
- Live end-to-end: `gpt-4o` against synthetic (29 items), captured + judged +
  metrics rolled up. Results comment on the PR; absolute numbers are a
  one-shot reference point (not a benchmark — Sonnet remains the gold
  baseline).

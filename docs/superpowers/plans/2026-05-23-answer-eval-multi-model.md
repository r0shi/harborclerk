# PR-C Answer-Eval Multi-Model Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a second baseline model (`gpt-4o`) to `--mode answer-eval` via a pluggable Provider protocol, without breaking any existing Sonnet wiring.

**Architecture:** New `scripts/test_corpora/runner/providers/` package holds (1) `base.py` with the `Provider` typing.Protocol + `BaselineResult` dataclass + `DEFAULT_SYSTEM_PROMPT`, (2) `anthropic_provider.py` with `AnthropicProvider`, (3) `openai_provider.py` with `OpenAIProvider`, (4) `factory.py` with `make_provider(model, *, mcp_session)`. The old `claude_baseline.py` becomes a 5-line re-export shim so `sweep.py`'s import sites stay untouched.

**Tech Stack:** Python 3.12, `anthropic>=0.43` (already installed), `openai>=1.50` (added in Task 9), `pytest`, MCP tool sessions via `SyncMcpSession`.

**Spec reference:** `docs/superpowers/specs/2026-05-23-answer-eval-multi-model-design.md`

---

## File Structure

**New files (this PR):**
- `scripts/test_corpora/runner/providers/__init__.py` — re-exports
- `scripts/test_corpora/runner/providers/base.py` — Protocol + BaselineResult + DEFAULT_SYSTEM_PROMPT
- `scripts/test_corpora/runner/providers/anthropic_provider.py` — AnthropicProvider
- `scripts/test_corpora/runner/providers/openai_provider.py` — OpenAIProvider
- `scripts/test_corpora/runner/providers/factory.py` — make_provider
- `scripts/test_corpora/tests/test_providers_base.py` — Protocol + BaselineResult smoke tests
- `scripts/test_corpora/tests/test_providers_factory.py` — factory dispatch tests
- `scripts/test_corpora/tests/test_openai_provider.py` — OpenAI provider tests

**Modified files (this PR):**
- `scripts/test_corpora/runner/claude_baseline.py` — collapses to a 5-line shim
- `scripts/test_corpora/runner/answer_eval.py` — `_live_capture_fn` uses `make_provider`
- `scripts/test_corpora/runner/sweep.py` — generalizes `_with_anthropic_retry` to handle both providers
- `scripts/test_corpora/tests/test_sweep_overload_retry.py` — extends to cover OpenAI exceptions
- `scripts/test_corpora/pyproject.toml` — adds `openai>=1.50` dependency

**Untouched:** `runner/answer_judge.py`, ground-truth YAMLs, all other tests.

---

## Task 1: Provider Protocol + BaselineResult + DEFAULT_SYSTEM_PROMPT in `providers/base.py`

**Files:**
- Create: `scripts/test_corpora/runner/providers/__init__.py` (empty for now; populated in Task 6)
- Create: `scripts/test_corpora/runner/providers/base.py`
- Create: `scripts/test_corpora/tests/test_providers_base.py`

- [ ] **Step 1: Write the failing test for the Protocol + dataclass shape**

Create `scripts/test_corpora/tests/test_providers_base.py`:

```python
# scripts/test_corpora/tests/test_providers_base.py
"""Smoke tests for the Provider Protocol + BaselineResult dataclass."""
import dataclasses

from scripts.test_corpora.runner.providers.base import (
    BaselineResult,
    DEFAULT_SYSTEM_PROMPT,
    Provider,
)


def test_baseline_result_is_a_dataclass_with_expected_fields():
    """BaselineResult is the universal return shape from every Provider."""
    assert dataclasses.is_dataclass(BaselineResult)
    fields = {f.name for f in dataclasses.fields(BaselineResult)}
    assert fields == {
        "question_id",
        "question",
        "answer",
        "cited_doc_ids",
        "cited_doc_titles",
        "tool_call_count",
        "tool_transcript",
        "elapsed_seconds",
        "model",
        "timestamp",
    }


def test_default_system_prompt_mentions_corpus_and_mcp_tools():
    """DEFAULT_SYSTEM_PROMPT scopes the model to MCP-only retrieval."""
    assert "MCP" in DEFAULT_SYSTEM_PROMPT or "mcp" in DEFAULT_SYSTEM_PROMPT
    assert "corpus" in DEFAULT_SYSTEM_PROMPT.lower()


def test_provider_is_a_protocol_with_run_question():
    """Provider is a Protocol; any class with run_question matching the
    signature satisfies it (duck-typed at runtime via @runtime_checkable)."""

    class _FakeProvider:
        def run_question(self, question: str, question_id: str, corpus: str) -> BaselineResult:
            return BaselineResult(
                question_id=question_id,
                question=question,
                answer="",
                cited_doc_ids=[],
                cited_doc_titles=[],
                tool_call_count=0,
                tool_transcript=[],
                elapsed_seconds=0.0,
                model="fake",
                timestamp="2026-05-23T00:00:00Z",
            )

    # @runtime_checkable Protocol allows isinstance() against duck-typed classes
    assert isinstance(_FakeProvider(), Provider)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/alex/mcp-gateway && uv run --project scripts/test_corpora --extra test pytest scripts/test_corpora/tests/test_providers_base.py -v
```

Expected: ImportError — `scripts.test_corpora.runner.providers.base` does not exist yet.

- [ ] **Step 3: Create the providers/ package**

```bash
mkdir -p /Users/alex/mcp-gateway/scripts/test_corpora/runner/providers
```

Create `scripts/test_corpora/runner/providers/__init__.py`:

```python
# scripts/test_corpora/runner/providers/__init__.py
"""Multi-provider abstraction for the answer-eval baseline.

See docs/superpowers/specs/2026-05-23-answer-eval-multi-model-design.md for
the design rationale (Provider protocol + factory dispatch by model-name
prefix). Public exports are populated in Task 6.
"""
```

- [ ] **Step 4: Implement `providers/base.py`**

Create `scripts/test_corpora/runner/providers/base.py`:

```python
# scripts/test_corpora/runner/providers/base.py
"""Provider Protocol + universal BaselineResult dataclass.

Every concrete provider (AnthropicProvider, OpenAIProvider, ...) exposes the
same `run_question(question, question_id, corpus) -> BaselineResult` method.
BaselineResult is the canonical serialization shape under
`workdir/answer-eval/captures/<corpus>/<model>/<question_id>.json` — same
fields across providers so the judging loop in answer_eval.py is provider-blind.
"""

from __future__ import annotations

import dataclasses
from typing import Protocol, runtime_checkable


DEFAULT_SYSTEM_PROMPT = """You are answering a user's question about a specific document corpus.
You have access to the corpus only through the provided MCP tools (kb_search,
kb_read_passages, kb_get_document, kb_find_related, etc.). Use them as
needed. Cite specific documents in your answer by doc_id.

Be thorough — your answer is the gold-standard reference for evaluating
local models. Spend tool calls liberally."""


@dataclasses.dataclass
class BaselineResult:
    """Universal return shape from every Provider's `run_question`.

    Fields mirror the pre-refactor `claude_baseline.BaselineResult` so the
    file-on-disk format and downstream consumers (judge, metrics roll-up)
    don't need to change.
    """

    question_id: str
    question: str
    answer: str
    cited_doc_ids: list[str]
    # Stable, ingest-independent identifiers for the cited docs, parallel to
    # cited_doc_ids (same order). doc_id is a random per-ingest UUID, so a
    # baseline's cited_doc_ids never intersect a phase-4 response's doc_ids
    # unless both ran against the exact same ingest. doc_title (the filename
    # stem) is stable across re-ingests, so citation_overlap computed on
    # titles stays correct even when baseline and response come from
    # different ingests. Empty string for any cited doc whose tool result
    # carried no title.
    cited_doc_titles: list[str]
    tool_call_count: int
    # Per-call record [{tool, args, result_summary}] in call order — for
    # groundedness scoring and tool-use auditing.
    tool_transcript: list[dict]
    elapsed_seconds: float
    model: str
    timestamp: str


@runtime_checkable
class Provider(Protocol):
    """The narrow protocol every baseline provider satisfies.

    Implementations: AnthropicProvider, OpenAIProvider. Use
    `providers.factory.make_provider(model, *, mcp_session)` to construct
    one by model name.
    """

    def run_question(self, question: str, question_id: str, corpus: str) -> BaselineResult:
        ...
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd /Users/alex/mcp-gateway && uv run --project scripts/test_corpora --extra test pytest scripts/test_corpora/tests/test_providers_base.py -v
```

Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
cd /Users/alex/mcp-gateway && git add scripts/test_corpora/runner/providers/__init__.py scripts/test_corpora/runner/providers/base.py scripts/test_corpora/tests/test_providers_base.py && git commit -m "feat(eval): Provider Protocol + BaselineResult in providers/base.py" -m "First task in PR-C's multi-model arc — adds the universal return shape and the Protocol every provider satisfies. Empty providers/__init__.py for now (populated in Task 6 after all concrete providers + factory exist)." -m "Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: AnthropicProvider in `providers/anthropic_provider.py`

**Files:**
- Create: `scripts/test_corpora/runner/providers/anthropic_provider.py`
- Read: `scripts/test_corpora/runner/claude_baseline.py:1-195` (source for the loop body)

- [ ] **Step 1: Write the failing test that imports AnthropicProvider from the new path**

Append to `scripts/test_corpora/tests/test_providers_base.py`:

```python
def test_anthropic_provider_class_is_importable_from_new_path():
    """AnthropicProvider lives at providers.anthropic_provider — this is the
    canonical path; claude_baseline.py becomes a shim in Task 3."""
    from scripts.test_corpora.runner.providers.anthropic_provider import AnthropicProvider

    assert AnthropicProvider.__name__ == "AnthropicProvider"
    # AnthropicProvider satisfies the Provider Protocol (structural check)
    from scripts.test_corpora.runner.providers.base import Provider

    # AnthropicProvider needs a real client+mcp_session to instantiate, so we
    # use a structural check via hasattr rather than isinstance against an
    # instance. Provider is @runtime_checkable, but structural duck-typing
    # against the class itself reads more clearly.
    assert hasattr(AnthropicProvider, "run_question")
    assert callable(AnthropicProvider.run_question)
    _ = Provider  # silence unused-import warning
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/alex/mcp-gateway && uv run --project scripts/test_corpora --extra test pytest scripts/test_corpora/tests/test_providers_base.py::test_anthropic_provider_class_is_importable_from_new_path -v
```

Expected: ModuleNotFoundError on `providers.anthropic_provider`.

- [ ] **Step 3: Implement `providers/anthropic_provider.py`**

Create `scripts/test_corpora/runner/providers/anthropic_provider.py`:

```python
# scripts/test_corpora/runner/providers/anthropic_provider.py
"""AnthropicProvider — Sonnet (or any claude-* model) + Harbor Clerk MCP.

Body is the pre-refactor BaselineGenerator.run_question loop, lifted out of
claude_baseline.py verbatim. claude_baseline.py becomes a thin re-export
shim in Task 3 so sweep.py's existing import sites stay untouched.
"""

from __future__ import annotations

import dataclasses
import json
import time
from pathlib import Path
from typing import Any

import anthropic

from scripts.test_corpora.runner.providers.base import (
    DEFAULT_SYSTEM_PROMPT,
    BaselineResult,
)


class AnthropicProvider:
    """Runs a Sonnet (or other claude-*) model + Harbor Clerk MCP for one question.

    Constructor accepts `client=None` for back-compat with the pre-refactor
    `BaselineGenerator(client=..., mcp_session=...)` call signature in
    sweep.py — when not provided, the factory's convenience role lazily
    constructs `anthropic.Anthropic()` (which reads ANTHROPIC_API_KEY from
    env). Tests pass a mock client explicitly.
    """

    def __init__(
        self,
        *,
        mcp_session: Any,
        model: str = "claude-sonnet-4-6",
        client: anthropic.Anthropic | None = None,
        doc_ids_seen: list[str] | None = None,
    ):
        self._client = client if client is not None else anthropic.Anthropic()
        self._mcp = mcp_session
        self._model = model
        # Ordered map doc_id -> doc_title, in first-seen order. dict preserves
        # insertion order (3.7+), so cited_doc_ids and cited_doc_titles stay
        # parallel. For tests: pre-seed via doc_ids_seen (titles default to "").
        self._cited: dict[str, str] = {did: "" for did in doc_ids_seen} if doc_ids_seen else {}

    def _list_tools(self) -> list[dict]:
        """Discover MCP tools and convert to Anthropic's tool schema."""
        if self._mcp is None:
            return []
        # mcp.types.Tool has .name, .description, .inputSchema
        tools = self._mcp.list_tools()  # sync wrapper expected
        return [{"name": t.name, "description": t.description or "", "input_schema": t.inputSchema} for t in tools]

    def _exec_tool(self, name: str, args: dict) -> str:
        """Execute one MCP tool call, capture any doc_ids in the result."""
        result = self._mcp.call_tool(name, args)
        # Collect text content; capture doc_ids if present in the JSON
        text_chunks: list[str] = []
        for block in result.content:
            if hasattr(block, "text"):
                text_chunks.append(block.text)
                # Greedy extraction: if the tool returned JSON containing
                # doc_id fields, harvest them. Robust to nested structures.
                try:
                    parsed = json.loads(block.text)
                    self._collect_doc_ids(parsed)
                except (json.JSONDecodeError, TypeError):
                    pass
        return "\n".join(text_chunks) or "(empty)"

    def _collect_doc_ids(self, obj: Any) -> None:
        if isinstance(obj, dict):
            doc_id = obj.get("doc_id")
            if isinstance(doc_id, str):
                # Pair the doc_id with its title from the SAME dict. MCP tool
                # results (kb_search etc.) emit doc_id + doc_title together;
                # fall back to "title" then "". First-seen title wins — if a
                # later result re-mentions the doc with a title where the
                # first had none, upgrade the stored value.
                title = obj.get("doc_title") or obj.get("title") or ""
                if doc_id not in self._cited or (title and not self._cited[doc_id]):
                    self._cited[doc_id] = title
            for v in obj.values():
                self._collect_doc_ids(v)
        elif isinstance(obj, list):
            for v in obj:
                self._collect_doc_ids(v)

    def run_question(self, question: str, question_id: str, corpus: str) -> BaselineResult:
        started = time.time()
        tools = self._list_tools()
        messages: list[dict] = [{"role": "user", "content": question}]
        tool_call_count = 0
        tool_transcript: list[dict] = []
        resp = None

        while True:
            resp = self._client.messages.create(
                model=self._model,
                max_tokens=8000,
                system=DEFAULT_SYSTEM_PROMPT,
                tools=tools,
                messages=messages,
            )
            messages.append({"role": "assistant", "content": resp.content})

            if resp.stop_reason == "end_turn":
                break
            if resp.stop_reason != "tool_use":
                break  # safety

            # Execute each tool_use block, send results back as user message
            tool_results: list[dict] = []
            for block in resp.content:
                if getattr(block, "type", None) == "tool_use":
                    tool_call_count += 1
                    out = self._exec_tool(block.name, block.input)
                    tool_transcript.append(
                        {
                            "tool": block.name,
                            "args": dict(block.input),
                            "result_summary": out[:600],
                        }
                    )
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": out,
                        }
                    )
            messages.append({"role": "user", "content": tool_results})

        # Final answer is the last text block in the assistant turn — scan from
        # the end so a leading non-text block (e.g. tool_use) is skipped.
        final = ""
        if resp and resp.content:
            for block in reversed(resp.content):
                if hasattr(block, "text"):
                    final = block.text
                    break

        return BaselineResult(
            question_id=question_id,
            question=question,
            answer=final,
            cited_doc_ids=list(self._cited.keys()),
            cited_doc_titles=list(self._cited.values()),
            tool_call_count=tool_call_count,
            tool_transcript=tool_transcript,
            elapsed_seconds=time.time() - started,
            model=self._model,
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )

    @staticmethod
    def write(result: BaselineResult, results_dir: Path, corpus: str) -> Path:
        """Write a BaselineResult JSON under results_dir/baselines/<corpus>/.

        Kept as a @staticmethod (not a free function) for shim compatibility:
        the sweep.py phase-1 baseline calls `BaselineGenerator.write(...)`,
        which the shim re-exports as `AnthropicProvider.write`.
        """
        out = results_dir / "baselines" / corpus / f"{result.question_id}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(dataclasses.asdict(result), indent=2))
        return out
```

- [ ] **Step 4: Run the new test + verify existing baseline tests still pass via direct import (shim comes in Task 3)**

```bash
cd /Users/alex/mcp-gateway && uv run --project scripts/test_corpora --extra test pytest scripts/test_corpora/tests/test_providers_base.py -v
```

Expected: 4 passed.

```bash
cd /Users/alex/mcp-gateway && uv run --project scripts/test_corpora --extra test pytest scripts/test_corpora/tests/test_baseline.py -v
```

Expected: still passes — claude_baseline.py is unchanged in this task; the new file is additive.

- [ ] **Step 5: Lint + format**

```bash
cd /Users/alex/mcp-gateway && uv run ruff check scripts/test_corpora/runner/providers/anthropic_provider.py && uv run ruff format --check scripts/test_corpora/runner/providers/anthropic_provider.py
```

Expected: clean. If `format --check` fails, run `uv run ruff format scripts/test_corpora/runner/providers/anthropic_provider.py` and re-check.

- [ ] **Step 6: Commit**

```bash
cd /Users/alex/mcp-gateway && git add scripts/test_corpora/runner/providers/anthropic_provider.py scripts/test_corpora/tests/test_providers_base.py && git commit -m "feat(eval): AnthropicProvider — Sonnet + MCP, satisfies Provider Protocol" -m "Body lifted from claude_baseline.py verbatim (Task 3 collapses claude_baseline.py to a shim re-exporting AnthropicProvider as BaselineGenerator). Constructor takes mcp_session as keyword-only with client=None default — when client is None, anthropic.Anthropic() is constructed lazily (reads ANTHROPIC_API_KEY)." -m "Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Shim `claude_baseline.py` to re-export from new package

**Files:**
- Modify: `scripts/test_corpora/runner/claude_baseline.py` (collapse 195 lines → ~12-line shim)

- [ ] **Step 1: Verify the existing baseline tests still pass before the change (regression baseline)**

```bash
cd /Users/alex/mcp-gateway && uv run --project scripts/test_corpora --extra test pytest scripts/test_corpora/tests/test_baseline.py -v
```

Expected: all baseline tests pass (they're hitting the pre-shim implementation).

- [ ] **Step 2: Rewrite `claude_baseline.py` as a shim**

Overwrite `scripts/test_corpora/runner/claude_baseline.py` with:

```python
# scripts/test_corpora/runner/claude_baseline.py
"""Back-compat shim — the real code lives in providers/anthropic_provider.py.

`BaselineGenerator` is exported as an alias for `AnthropicProvider` so that
`sweep.py`'s 30+ import sites (and `tests/test_baseline.py`) continue to
work unchanged. A follow-up PR can migrate `sweep.py` to use
`make_provider()` directly; that's a larger blast radius — deferred.

See docs/superpowers/specs/2026-05-23-answer-eval-multi-model-design.md
for the design rationale.
"""

from __future__ import annotations

from scripts.test_corpora.runner.providers.anthropic_provider import (
    AnthropicProvider as BaselineGenerator,
)
from scripts.test_corpora.runner.providers.base import (
    DEFAULT_SYSTEM_PROMPT as SYSTEM_PROMPT,
    BaselineResult,
)

__all__ = ["BaselineGenerator", "BaselineResult", "SYSTEM_PROMPT"]
```

- [ ] **Step 3: Run baseline tests + new providers tests to verify nothing broke**

```bash
cd /Users/alex/mcp-gateway && uv run --project scripts/test_corpora --extra test pytest scripts/test_corpora/tests/test_baseline.py scripts/test_corpora/tests/test_providers_base.py -v
```

Expected: all green. The existing tests now exercise `AnthropicProvider` via the shim's `BaselineGenerator` alias — no behavioral change.

- [ ] **Step 4: Run the broader test suite to catch any indirect import breakage**

```bash
cd /Users/alex/mcp-gateway && uv run --project scripts/test_corpora --extra test pytest scripts/test_corpora/tests/ -q
```

Expected: same pass/fail count as before (1 pre-existing failure for `test_entity_overlap_english` is the spaCy-model-missing baseline; everything else green).

- [ ] **Step 5: Lint + format**

```bash
cd /Users/alex/mcp-gateway && uv run ruff check scripts/test_corpora/runner/claude_baseline.py && uv run ruff format --check scripts/test_corpora/runner/claude_baseline.py
```

Expected: clean.

- [ ] **Step 6: Commit**

```bash
cd /Users/alex/mcp-gateway && git add scripts/test_corpora/runner/claude_baseline.py && git commit -m "refactor(eval): collapse claude_baseline.py to a re-export shim" -m "All real code now lives in providers/anthropic_provider.py (Task 2). sweep.py's 30+ import sites (and tests/test_baseline.py) continue to work unchanged. A follow-up PR can migrate sweep.py to make_provider() directly; that's a larger blast radius — deferred." -m "Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: `make_provider` factory in `providers/factory.py`

**Files:**
- Create: `scripts/test_corpora/runner/providers/factory.py`
- Create: `scripts/test_corpora/tests/test_providers_factory.py`

- [ ] **Step 1: Write the failing tests for prefix-based dispatch**

Create `scripts/test_corpora/tests/test_providers_factory.py`:

```python
# scripts/test_corpora/tests/test_providers_factory.py
"""make_provider() — string-prefix dispatch on model name."""
from unittest.mock import MagicMock

import pytest

from scripts.test_corpora.runner.providers.anthropic_provider import AnthropicProvider
from scripts.test_corpora.runner.providers.factory import make_provider
from scripts.test_corpora.runner.providers.openai_provider import OpenAIProvider


def test_factory_dispatches_claude_to_anthropic():
    """Any name starting with 'claude-' → AnthropicProvider."""
    p = make_provider("claude-sonnet-4-6", mcp_session=MagicMock())
    assert isinstance(p, AnthropicProvider)


def test_factory_dispatches_claude_3_5_to_anthropic():
    """Legacy claude- names still dispatch correctly."""
    p = make_provider("claude-3-5-sonnet-20241022", mcp_session=MagicMock())
    assert isinstance(p, AnthropicProvider)


def test_factory_dispatches_gpt_to_openai():
    """Any name starting with 'gpt-' → OpenAIProvider."""
    p = make_provider("gpt-4o", mcp_session=MagicMock())
    assert isinstance(p, OpenAIProvider)


def test_factory_dispatches_o1_to_openai():
    """Any name starting with 'o1-' → OpenAIProvider."""
    p = make_provider("o1-preview", mcp_session=MagicMock())
    assert isinstance(p, OpenAIProvider)


def test_factory_dispatches_o3_to_openai():
    """Any name starting with 'o3-' → OpenAIProvider."""
    p = make_provider("o3-mini", mcp_session=MagicMock())
    assert isinstance(p, OpenAIProvider)


def test_factory_raises_value_error_on_unknown_model():
    """An unknown prefix raises ValueError with the supported list in the message."""
    with pytest.raises(ValueError, match=r"unknown model.*supported prefixes.*claude-.*gpt-"):
        make_provider("llama-3.1-70b", mcp_session=MagicMock())


def test_factory_forwards_doc_ids_seen_kwarg_to_anthropic():
    """doc_ids_seen pre-seeds the provider's cited-doc map for test scenarios."""
    p = make_provider("claude-sonnet-4-6", mcp_session=MagicMock(), doc_ids_seen=["doc-a", "doc-b"])
    assert isinstance(p, AnthropicProvider)
    # cited starts pre-seeded with the two doc ids (titles default to "")
    assert list(p._cited.keys()) == ["doc-a", "doc-b"]


def test_factory_forwards_doc_ids_seen_kwarg_to_openai():
    """doc_ids_seen works the same way for the OpenAI provider."""
    p = make_provider("gpt-4o", mcp_session=MagicMock(), doc_ids_seen=["doc-x"])
    assert isinstance(p, OpenAIProvider)
    assert list(p._cited.keys()) == ["doc-x"]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd /Users/alex/mcp-gateway && uv run --project scripts/test_corpora --extra test pytest scripts/test_corpora/tests/test_providers_factory.py -v
```

Expected: ImportError on `providers.factory` AND on `providers.openai_provider` (OpenAI provider isn't created until Task 5; that import failure is expected here — fix in Task 5).

- [ ] **Step 3: Implement `providers/factory.py`**

Create `scripts/test_corpora/runner/providers/factory.py`:

```python
# scripts/test_corpora/runner/providers/factory.py
"""make_provider — single dispatch point that maps model name → Provider.

String matching by prefix:
  claude-*           -> AnthropicProvider
  gpt-* / o1-* / o3-* -> OpenAIProvider

Unknown prefixes raise ValueError with the supported list. The factory
lazily constructs the underlying client (anthropic.Anthropic() or
openai.OpenAI()) inside the matched branch via the provider's own
constructor — both read API keys from the standard env vars
(ANTHROPIC_API_KEY, OPENAI_API_KEY).
"""

from __future__ import annotations

from typing import Any

from scripts.test_corpora.runner.providers.anthropic_provider import AnthropicProvider
from scripts.test_corpora.runner.providers.base import Provider
from scripts.test_corpora.runner.providers.openai_provider import OpenAIProvider


_ANTHROPIC_PREFIXES = ("claude-",)
_OPENAI_PREFIXES = ("gpt-", "o1-", "o3-")


def make_provider(
    model: str,
    *,
    mcp_session: Any,
    doc_ids_seen: list[str] | None = None,
) -> Provider:
    """Construct a Provider for `model` wired to `mcp_session`.

    `doc_ids_seen` pre-seeds the cited-doc map (used by tests + by callers
    that want to anchor citations to a known starting set).
    """
    if model.startswith(_ANTHROPIC_PREFIXES):
        return AnthropicProvider(mcp_session=mcp_session, model=model, doc_ids_seen=doc_ids_seen)
    if model.startswith(_OPENAI_PREFIXES):
        return OpenAIProvider(mcp_session=mcp_session, model=model, doc_ids_seen=doc_ids_seen)
    supported = ", ".join(_ANTHROPIC_PREFIXES + _OPENAI_PREFIXES)
    raise ValueError(f"unknown model {model!r}; supported prefixes: {supported}")
```

- [ ] **Step 4: Run factory tests — they will still fail on the OpenAIProvider import**

```bash
cd /Users/alex/mcp-gateway && uv run --project scripts/test_corpora --extra test pytest scripts/test_corpora/tests/test_providers_factory.py -v
```

Expected: ModuleNotFoundError on `providers.openai_provider`. Defer green test until Task 5. (Skipping ahead to make factory work without OpenAI provider would entail dead code in factory.py — better to keep the import + let Task 5 close it.)

- [ ] **Step 5: Commit**

```bash
cd /Users/alex/mcp-gateway && git add scripts/test_corpora/runner/providers/factory.py scripts/test_corpora/tests/test_providers_factory.py && git commit -m "feat(eval): make_provider factory + factory tests (OpenAI provider Task 5)" -m "Dispatch by model-name prefix: claude-* -> AnthropicProvider, gpt-*/o1-*/o3-* -> OpenAIProvider, unknown -> ValueError with supported list. Factory tests created here but fail until Task 5 lands OpenAIProvider; left intentionally red so the next commit goes green." -m "Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: OpenAIProvider in `providers/openai_provider.py`

**Files:**
- Modify: `scripts/test_corpora/pyproject.toml` (add `openai>=1.50`)
- Create: `scripts/test_corpora/runner/providers/openai_provider.py`
- Create: `scripts/test_corpora/tests/test_openai_provider.py`

- [ ] **Step 1: Add openai dependency**

Edit `scripts/test_corpora/pyproject.toml`. Find the `dependencies = [...]` block and add `"openai>=1.50",` alphabetically near `"anthropic>=0.43",`:

```toml
dependencies = [
    "httpx>=0.28",
    "anthropic>=0.43",
    "openai>=1.50",
    "mcp>=1.9",
    "pyyaml>=6",
    "tenacity>=9",
    "pypdfium2>=4",
    "Pillow>=12",
    "spacy>=3.7",
    "huggingface_hub>=0.25",
    "pyarrow>=21",
]
```

Then sync:

```bash
cd /Users/alex/mcp-gateway && uv sync --project scripts/test_corpora --extra test 2>&1 | tail -5
```

Expected: openai installed successfully.

- [ ] **Step 2: Write the failing tests for the OpenAI provider's tool loop**

Create `scripts/test_corpora/tests/test_openai_provider.py`:

```python
# scripts/test_corpora/tests/test_openai_provider.py
"""OpenAIProvider — gpt-4o + Harbor Clerk MCP via OpenAI's chat/completions
tool_calls loop. Mirrors AnthropicProvider's shape but speaks OpenAI's API.
"""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from scripts.test_corpora.runner.providers.base import BaselineResult
from scripts.test_corpora.runner.providers.openai_provider import OpenAIProvider


def _mk_resp(*, finish_reason: str, text: str = "", tool_calls=None):
    """Build a minimal OpenAI-style ChatCompletion response object.

    OpenAI SDK returns `resp.choices[0].finish_reason` and
    `resp.choices[0].message.{content, tool_calls}`. tool_calls is a list of
    objects with `.id`, `.type='function'`, `.function.name`, `.function.arguments`
    (JSON string).
    """
    msg = SimpleNamespace(content=text, tool_calls=tool_calls)
    choice = SimpleNamespace(finish_reason=finish_reason, message=msg)
    return SimpleNamespace(choices=[choice])


def _mk_tool_call(*, id: str, name: str, args: dict):
    fn = SimpleNamespace(name=name, arguments=json.dumps(args))
    return SimpleNamespace(id=id, type="function", function=fn)


def test_openai_provider_parses_simple_end_turn_answer():
    """A single response with finish_reason='stop' returns its content as the final answer."""
    client = MagicMock()
    client.chat.completions.create.return_value = _mk_resp(
        finish_reason="stop", text="The answer is 42."
    )
    p = OpenAIProvider(mcp_session=None, model="gpt-4o", client=client)
    res = p.run_question(question="What is the answer?", question_id="q1", corpus="cuad")
    assert isinstance(res, BaselineResult)
    assert res.answer == "The answer is 42."
    assert res.tool_call_count == 0
    assert res.cited_doc_ids == []
    assert res.model == "gpt-4o"


def test_openai_provider_tool_loop_captures_cited_docs():
    """A tool_calls round followed by an end-turn round — cited_doc_ids
    are harvested from the MCP result and the final answer is the second
    response's content."""
    mcp = MagicMock()
    mcp.list_tools.return_value = [
        SimpleNamespace(name="kb_search", description="search", inputSchema={"type": "object"})
    ]
    # First MCP call returns one block with JSON containing a doc_id+doc_title pair
    mcp.call_tool.return_value = SimpleNamespace(
        content=[SimpleNamespace(text='{"doc_id": "doc-abc", "doc_title": "Acme Contract", "snippet": "..."}')]
    )

    client = MagicMock()
    client.chat.completions.create.side_effect = [
        _mk_resp(
            finish_reason="tool_calls",
            tool_calls=[_mk_tool_call(id="call-1", name="kb_search", args={"query": "acme"})],
        ),
        _mk_resp(finish_reason="stop", text="Per doc-abc, the answer is found."),
    ]
    p = OpenAIProvider(mcp_session=mcp, model="gpt-4o", client=client)
    res = p.run_question(question="Find acme.", question_id="q2", corpus="cuad")
    assert res.tool_call_count == 1
    assert res.cited_doc_ids == ["doc-abc"]
    assert res.cited_doc_titles == ["Acme Contract"]
    assert res.answer == "Per doc-abc, the answer is found."


def test_openai_provider_transcript_records_each_call():
    """tool_transcript has one entry per tool_call with tool, args, result_summary."""
    mcp = MagicMock()
    mcp.list_tools.return_value = [
        SimpleNamespace(name="kb_search", description="", inputSchema={"type": "object"})
    ]
    mcp.call_tool.return_value = SimpleNamespace(
        content=[SimpleNamespace(text='{"doc_id": "x"}')]
    )
    client = MagicMock()
    client.chat.completions.create.side_effect = [
        _mk_resp(
            finish_reason="tool_calls",
            tool_calls=[_mk_tool_call(id="c1", name="kb_search", args={"query": "alpha"})],
        ),
        _mk_resp(finish_reason="stop", text="done"),
    ]
    p = OpenAIProvider(mcp_session=mcp, model="gpt-4o", client=client)
    res = p.run_question(question="q", question_id="q3", corpus="cuad")
    assert len(res.tool_transcript) == 1
    entry = res.tool_transcript[0]
    assert entry["tool"] == "kb_search"
    assert entry["args"] == {"query": "alpha"}
    assert entry["result_summary"].startswith('{"doc_id":')


def test_openai_provider_treats_length_finish_as_end_turn():
    """finish_reason='length' (response truncated) returns the partial answer
    with a logged warning rather than crashing. PR-B's _score() in the judge
    set this defensive precedent — Anthropic's API rarely hits this; OpenAI's
    max_tokens semantics are subtly different and we may hit it."""
    client = MagicMock()
    client.chat.completions.create.return_value = _mk_resp(
        finish_reason="length", text="The answer begins with the very long preamble"
    )
    p = OpenAIProvider(mcp_session=None, model="gpt-4o", client=client)
    res = p.run_question(question="q", question_id="q4", corpus="cuad")
    assert res.answer.startswith("The answer begins")
    assert res.tool_call_count == 0


def test_openai_provider_falls_back_when_mcp_returns_empty_content():
    """When a tool result has no text content, the loop sends '(empty)' so the
    model doesn't choke on a literal empty string in the tool response."""
    mcp = MagicMock()
    mcp.list_tools.return_value = [
        SimpleNamespace(name="kb_search", description="", inputSchema={"type": "object"})
    ]
    mcp.call_tool.return_value = SimpleNamespace(content=[])  # empty
    client = MagicMock()
    client.chat.completions.create.side_effect = [
        _mk_resp(
            finish_reason="tool_calls",
            tool_calls=[_mk_tool_call(id="c1", name="kb_search", args={"q": "x"})],
        ),
        _mk_resp(finish_reason="stop", text="nothing found"),
    ]
    p = OpenAIProvider(mcp_session=mcp, model="gpt-4o", client=client)
    res = p.run_question(question="q", question_id="q5", corpus="cuad")
    assert res.tool_transcript[0]["result_summary"] == "(empty)"
    # Verify the tool result message sent back to the model contained "(empty)"
    call_args_list = client.chat.completions.create.call_args_list
    assert len(call_args_list) == 2
    second_call_messages = call_args_list[1].kwargs["messages"]
    tool_msg = next(m for m in second_call_messages if m.get("role") == "tool")
    assert tool_msg["content"] == "(empty)"
```

- [ ] **Step 3: Run tests to verify they fail with ImportError**

```bash
cd /Users/alex/mcp-gateway && uv run --project scripts/test_corpora --extra test pytest scripts/test_corpora/tests/test_openai_provider.py -v
```

Expected: ImportError on `providers.openai_provider`.

- [ ] **Step 4: Implement `providers/openai_provider.py`**

Create `scripts/test_corpora/runner/providers/openai_provider.py`:

```python
# scripts/test_corpora/runner/providers/openai_provider.py
"""OpenAIProvider — gpt-* (and o1-*/o3-*) + Harbor Clerk MCP.

Structurally mirrors AnthropicProvider but speaks OpenAI's chat.completions
tool_calls protocol:
  1. Send user message + tools list.
  2. Receive a response with `finish_reason`:
     - "stop"        -> done, return final assistant text
     - "tool_calls"  -> execute each tool, send results back, loop
     - "length"      -> response truncated; treat as end-of-turn (logged warn)
  3. Tool result messages have role="tool", tool_call_id=<call id>.

Constructor accepts client=None for symmetry with AnthropicProvider; when
None, openai.OpenAI() reads OPENAI_API_KEY from env.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import openai

from scripts.test_corpora.runner.providers.base import (
    DEFAULT_SYSTEM_PROMPT,
    BaselineResult,
)

log = logging.getLogger("openai_provider")


class OpenAIProvider:
    """Runs a gpt-* (or o1-*/o3-*) model + Harbor Clerk MCP for one question."""

    def __init__(
        self,
        *,
        mcp_session: Any,
        model: str = "gpt-4o",
        client: openai.OpenAI | None = None,
        doc_ids_seen: list[str] | None = None,
    ):
        self._client = client if client is not None else openai.OpenAI()
        self._mcp = mcp_session
        self._model = model
        self._cited: dict[str, str] = {did: "" for did in doc_ids_seen} if doc_ids_seen else {}

    def _list_tools(self) -> list[dict]:
        """Discover MCP tools and convert to OpenAI's `tools` schema.

        OpenAI shape: [{"type": "function", "function": {"name", "description",
        "parameters"}}]. The MCP tool's `inputSchema` slots directly into
        `function.parameters` (both are JSON Schema).
        """
        if self._mcp is None:
            return []
        tools = self._mcp.list_tools()
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description or "",
                    "parameters": t.inputSchema,
                },
            }
            for t in tools
        ]

    def _exec_tool(self, name: str, args: dict) -> str:
        """Execute one MCP tool call, capture any doc_ids in the result."""
        result = self._mcp.call_tool(name, args)
        text_chunks: list[str] = []
        for block in result.content:
            if hasattr(block, "text"):
                text_chunks.append(block.text)
                try:
                    parsed = json.loads(block.text)
                    self._collect_doc_ids(parsed)
                except (json.JSONDecodeError, TypeError):
                    pass
        return "\n".join(text_chunks) or "(empty)"

    def _collect_doc_ids(self, obj: Any) -> None:
        if isinstance(obj, dict):
            doc_id = obj.get("doc_id")
            if isinstance(doc_id, str):
                title = obj.get("doc_title") or obj.get("title") or ""
                if doc_id not in self._cited or (title and not self._cited[doc_id]):
                    self._cited[doc_id] = title
            for v in obj.values():
                self._collect_doc_ids(v)
        elif isinstance(obj, list):
            for v in obj:
                self._collect_doc_ids(v)

    def run_question(self, question: str, question_id: str, corpus: str) -> BaselineResult:
        started = time.time()
        tools = self._list_tools()
        messages: list[dict] = [
            {"role": "system", "content": DEFAULT_SYSTEM_PROMPT},
            {"role": "user", "content": question},
        ]
        tool_call_count = 0
        tool_transcript: list[dict] = []
        final_text = ""

        while True:
            kwargs: dict[str, Any] = {
                "model": self._model,
                "max_tokens": 8000,
                "messages": messages,
            }
            if tools:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = "auto"
            resp = self._client.chat.completions.create(**kwargs)
            choice = resp.choices[0]
            msg = choice.message
            finish = choice.finish_reason

            # Persist the assistant message before loop-deciding so subsequent
            # tool results pair with it correctly. OpenAI requires the
            # assistant message that contained the tool_calls to be in the
            # history before the tool-result messages.
            assistant_msg: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
            if msg.tool_calls:
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    }
                    for tc in msg.tool_calls
                ]
            messages.append(assistant_msg)

            if finish == "tool_calls" and msg.tool_calls:
                for tc in msg.tool_calls:
                    tool_call_count += 1
                    try:
                        args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                    except json.JSONDecodeError:
                        args = {}
                    out = self._exec_tool(tc.function.name, args)
                    tool_transcript.append(
                        {
                            "tool": tc.function.name,
                            "args": args,
                            "result_summary": out[:600],
                        }
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": out,
                        }
                    )
                continue  # loop to next assistant turn

            # finish in {"stop", "length", "content_filter", "function_call", ...}
            if finish == "length":
                log.warning(
                    "openai response truncated (finish_reason=length) — returning partial answer"
                )
            final_text = msg.content or ""
            break

        return BaselineResult(
            question_id=question_id,
            question=question,
            answer=final_text,
            cited_doc_ids=list(self._cited.keys()),
            cited_doc_titles=list(self._cited.values()),
            tool_call_count=tool_call_count,
            tool_transcript=tool_transcript,
            elapsed_seconds=time.time() - started,
            model=self._model,
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        )
```

- [ ] **Step 5: Run OpenAI provider tests + factory tests + base tests — all should now pass**

```bash
cd /Users/alex/mcp-gateway && uv run --project scripts/test_corpora --extra test pytest scripts/test_corpora/tests/test_openai_provider.py scripts/test_corpora/tests/test_providers_factory.py scripts/test_corpora/tests/test_providers_base.py -v
```

Expected: 5 openai + 8 factory + 4 base = 17 passed.

- [ ] **Step 6: Lint + format**

```bash
cd /Users/alex/mcp-gateway && uv run ruff check scripts/test_corpora/runner/providers/ scripts/test_corpora/tests/test_openai_provider.py scripts/test_corpora/tests/test_providers_factory.py && uv run ruff format --check scripts/test_corpora/runner/providers/ scripts/test_corpora/tests/test_openai_provider.py scripts/test_corpora/tests/test_providers_factory.py
```

Expected: clean. Fix and re-check if needed.

- [ ] **Step 7: Commit**

```bash
cd /Users/alex/mcp-gateway && git add scripts/test_corpora/pyproject.toml scripts/test_corpora/uv.lock scripts/test_corpora/runner/providers/openai_provider.py scripts/test_corpora/tests/test_openai_provider.py && git commit -m "feat(eval): OpenAIProvider — gpt-4o + MCP, satisfies Provider Protocol" -m "Mirrors AnthropicProvider's shape: same BaselineResult, same MCP tool execution path, same cited-doc harvesting. Adapts to OpenAI's chat.completions tool_calls protocol (role=tool messages with tool_call_id, parameters schema instead of input_schema). finish_reason='length' treated as soft end-of-turn with a logged warning (defensive precedent set by PR-B's _score() helper)." -m "openai>=1.50 added to scripts/test_corpora/pyproject.toml." -m "Closes the factory tests that were left red in Task 4." -m "Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Populate `providers/__init__.py` with the public API

**Files:**
- Modify: `scripts/test_corpora/runner/providers/__init__.py`

- [ ] **Step 1: Write the smoke test for the package's public exports**

Append to `scripts/test_corpora/tests/test_providers_base.py`:

```python
def test_providers_package_exports_canonical_api():
    """The providers package re-exports the canonical API for callers."""
    from scripts.test_corpora.runner import providers

    assert providers.BaselineResult.__name__ == "BaselineResult"
    assert providers.DEFAULT_SYSTEM_PROMPT  # non-empty string
    assert callable(providers.make_provider)
    assert providers.Provider.__name__ == "Provider"
    assert providers.AnthropicProvider.__name__ == "AnthropicProvider"
    assert providers.OpenAIProvider.__name__ == "OpenAIProvider"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/alex/mcp-gateway && uv run --project scripts/test_corpora --extra test pytest scripts/test_corpora/tests/test_providers_base.py::test_providers_package_exports_canonical_api -v
```

Expected: AttributeError on `providers.BaselineResult` (the __init__.py is empty).

- [ ] **Step 3: Populate `providers/__init__.py`**

Overwrite `scripts/test_corpora/runner/providers/__init__.py`:

```python
# scripts/test_corpora/runner/providers/__init__.py
"""Multi-provider abstraction for the answer-eval baseline.

Public API:
  Provider              — typing.Protocol every provider satisfies
  BaselineResult        — universal dataclass returned by every provider
  DEFAULT_SYSTEM_PROMPT — shared system prompt across providers
  AnthropicProvider     — Sonnet (or any claude-* model) + MCP
  OpenAIProvider        — gpt-4o (or any gpt-*/o1-*/o3-* model) + MCP
  make_provider         — factory: dispatch by model-name prefix

See docs/superpowers/specs/2026-05-23-answer-eval-multi-model-design.md.
"""

from scripts.test_corpora.runner.providers.anthropic_provider import AnthropicProvider
from scripts.test_corpora.runner.providers.base import (
    DEFAULT_SYSTEM_PROMPT,
    BaselineResult,
    Provider,
)
from scripts.test_corpora.runner.providers.factory import make_provider
from scripts.test_corpora.runner.providers.openai_provider import OpenAIProvider

__all__ = [
    "AnthropicProvider",
    "BaselineResult",
    "DEFAULT_SYSTEM_PROMPT",
    "OpenAIProvider",
    "Provider",
    "make_provider",
]
```

- [ ] **Step 4: Run smoke test + all package tests**

```bash
cd /Users/alex/mcp-gateway && uv run --project scripts/test_corpora --extra test pytest scripts/test_corpora/tests/test_providers_base.py scripts/test_corpora/tests/test_providers_factory.py scripts/test_corpora/tests/test_openai_provider.py -v
```

Expected: all 18 passed (4 base + 1 new export test + 8 factory + 5 openai).

- [ ] **Step 5: Commit**

```bash
cd /Users/alex/mcp-gateway && git add scripts/test_corpora/runner/providers/__init__.py scripts/test_corpora/tests/test_providers_base.py && git commit -m "feat(eval): populate providers/__init__.py with canonical public API" -m "Re-exports Provider, BaselineResult, DEFAULT_SYSTEM_PROMPT, AnthropicProvider, OpenAIProvider, and make_provider so callers can do 'from scripts.test_corpora.runner.providers import make_provider'." -m "Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: Wire factory into `answer_eval._live_capture_fn`

**Files:**
- Modify: `scripts/test_corpora/runner/answer_eval.py:231-269` (the `_live_capture_fn` function)

- [ ] **Step 1: Read the current implementation**

```bash
cd /Users/alex/mcp-gateway && cat -n scripts/test_corpora/runner/answer_eval.py | sed -n '231,269p'
```

Expected: shows the current `_live_capture_fn` that imports `BaselineGenerator` directly and constructs `anthropic.Anthropic()`.

- [ ] **Step 2: Rewrite `_live_capture_fn` to use `make_provider`**

Edit `scripts/test_corpora/runner/answer_eval.py` — find the function `_live_capture_fn` and replace its body:

```python
def _live_capture_fn(*, api_base: str, corpus: str, model: str, insecure: bool) -> Callable[[GTItem], dict]:
    """Build the production capture function: a model+MCP run per item.

    Uses make_provider() to dispatch on model name (claude-* -> AnthropicProvider,
    gpt-* / o1-* / o3-* -> OpenAIProvider). The MCP session MUST be
    authenticated with a corpus-scoped API key so HC's search is restricted to
    `corpus`: set HC_API_KEY to that scoped key. Falls back to a
    HC_USERNAME/HC_PASSWORD login, which yields an UNSCOPED (full-index)
    session — correct only when `corpus` is the lone corpus loaded.

    The provider's underlying API client (anthropic.Anthropic() or
    openai.OpenAI()) is lazily constructed inside the provider; both read
    their API keys from env (ANTHROPIC_API_KEY / OPENAI_API_KEY).
    """
    from scripts.test_corpora.runner.client import HarborClerkClient, SyncMcpSession
    from scripts.test_corpora.runner.providers import make_provider

    token = os.environ.get("HC_API_KEY")
    if not token:
        user, password = os.environ.get("HC_USERNAME"), os.environ.get("HC_PASSWORD")
        if not (user and password):
            raise RuntimeError(
                "answer-eval needs HC_API_KEY (a corpus-scoped key) or HC_USERNAME + HC_PASSWORD in the environment"
            )
        hc = HarborClerkClient(api_base, verify=not insecure)
        hc.login(user, password)
        token = hc.get_bearer_token()
        if not token:
            raise RuntimeError("HC login did not yield a bearer token — check HC_USERNAME / HC_PASSWORD")
        log.warning("HC_API_KEY unset — using an unscoped login; search is NOT corpus-restricted")

    mcp_url = os.environ.get("HC_MCP_URL") or f"{api_base}/mcp/mcp"
    mcp = SyncMcpSession(url=mcp_url, headers={"Authorization": f"Bearer {token}"})

    def capture(item: GTItem) -> dict:
        provider = make_provider(model, mcp_session=mcp)
        res = provider.run_question(question=item.question, question_id=item.id, corpus=corpus)
        return dataclasses.asdict(res)

    return capture
```

- [ ] **Step 3: Verify the existing answer-eval tests still pass**

```bash
cd /Users/alex/mcp-gateway && uv run --project scripts/test_corpora --extra test pytest scripts/test_corpora/tests/test_answer_eval.py -v
```

Expected: all green. The tests inject `capture_fn` directly, so they bypass `_live_capture_fn` — this change doesn't affect them. The change is exercised by Task 10's live run.

- [ ] **Step 4: Lint + format**

```bash
cd /Users/alex/mcp-gateway && uv run ruff check scripts/test_corpora/runner/answer_eval.py && uv run ruff format --check scripts/test_corpora/runner/answer_eval.py
```

Expected: clean.

- [ ] **Step 5: Commit**

```bash
cd /Users/alex/mcp-gateway && git add scripts/test_corpora/runner/answer_eval.py && git commit -m "feat(eval): answer-eval _live_capture_fn uses make_provider() dispatch" -m "Replaces the direct BaselineGenerator(client=anthropic.Anthropic(), ...) wiring with make_provider(model, mcp_session=mcp). Now answer-eval supports any model name the factory recognizes (claude-* and gpt-*/o1-*/o3-*) — the underlying API client is constructed lazily inside the provider." -m "Existing unit tests inject capture_fn directly so they bypass this code path; live exercise comes from the gpt-4o run against synthetic." -m "Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: Provider-aware retry in `sweep._with_anthropic_retry`

**Files:**
- Modify: `scripts/test_corpora/runner/sweep.py:99-145` (the `_with_anthropic_retry` function and constants)
- Modify: `scripts/test_corpora/tests/test_sweep_overload_retry.py` (extend to cover OpenAI exceptions)

- [ ] **Step 1: Inspect current retry helper + its existing tests**

```bash
cd /Users/alex/mcp-gateway && cat -n scripts/test_corpora/runner/sweep.py | sed -n '85,150p'
```

```bash
cd /Users/alex/mcp-gateway && cat -n scripts/test_corpora/tests/test_sweep_overload_retry.py | head -80
```

- [ ] **Step 2: Write the failing tests for OpenAI exception classes**

Append to `scripts/test_corpora/tests/test_sweep_overload_retry.py`:

```python
def test_retry_handles_openai_rate_limit_error():
    """OpenAI's RateLimitError triggers backoff via _with_provider_retry."""
    from unittest.mock import MagicMock

    import openai

    from scripts.test_corpora.runner.sweep import _with_provider_retry

    # Build a minimal openai.RateLimitError (it requires message + response + body)
    fake_resp = MagicMock()
    fake_resp.status_code = 429
    err = openai.RateLimitError("rate limited", response=fake_resp, body=None)

    call_count = {"n": 0}

    def fn():
        call_count["n"] += 1
        if call_count["n"] < 2:
            raise err
        return "ok"

    sleeps = []
    result = _with_provider_retry(fn, client_kind="openai", sleep=sleeps.append, max_total_seconds=3600)
    assert result == "ok"
    assert call_count["n"] == 2
    assert len(sleeps) == 1  # one backoff before the successful retry


def test_retry_handles_openai_api_status_error_503():
    """OpenAI's APIStatusError 503 (overloaded) triggers backoff."""
    from unittest.mock import MagicMock

    import openai

    from scripts.test_corpora.runner.sweep import _with_provider_retry

    fake_resp = MagicMock()
    fake_resp.status_code = 503
    err = openai.APIStatusError("overloaded", response=fake_resp, body=None)

    call_count = {"n": 0}

    def fn():
        call_count["n"] += 1
        if call_count["n"] < 2:
            raise err
        return "ok"

    sleeps = []
    result = _with_provider_retry(fn, client_kind="openai", sleep=sleeps.append, max_total_seconds=3600)
    assert result == "ok"
    assert call_count["n"] == 2


def test_retry_with_anthropic_kind_still_works():
    """Back-compat: _with_provider_retry(client_kind='anthropic') matches the
    legacy _with_anthropic_retry behavior."""
    from anthropic._exceptions import RateLimitError as AnthropicRateLimitError

    from scripts.test_corpora.runner.sweep import _with_provider_retry

    call_count = {"n": 0}

    def fn():
        call_count["n"] += 1
        if call_count["n"] < 2:
            # AnthropicRateLimitError requires (message, *, response, body) too
            from unittest.mock import MagicMock

            fake_resp = MagicMock()
            fake_resp.status_code = 429
            raise AnthropicRateLimitError("rate limited", response=fake_resp, body=None)
        return "ok"

    sleeps = []
    result = _with_provider_retry(fn, client_kind="anthropic", sleep=sleeps.append, max_total_seconds=3600)
    assert result == "ok"


def test_with_anthropic_retry_legacy_alias_still_callable():
    """The old _with_anthropic_retry name remains as a back-compat alias."""
    from scripts.test_corpora.runner.sweep import _with_anthropic_retry

    # Just verify it's callable and forwards to _with_provider_retry
    result = _with_anthropic_retry(lambda: "ok")
    assert result == "ok"
```

- [ ] **Step 3: Run tests to verify the new ones fail**

```bash
cd /Users/alex/mcp-gateway && uv run --project scripts/test_corpora --extra test pytest scripts/test_corpora/tests/test_sweep_overload_retry.py -v
```

Expected: existing tests pass; the four new tests fail with `ImportError: cannot import name '_with_provider_retry'`.

- [ ] **Step 4: Generalize the retry helper in `sweep.py`**

Edit `scripts/test_corpora/runner/sweep.py`. Find lines around 94-145 (the constants + `_with_anthropic_retry` function) and replace with:

```python
ANTHROPIC_RETRY_BASE_SECONDS = 60
ANTHROPIC_RETRY_MAX_DELAY_SECONDS = 600
ANTHROPIC_RETRY_BUDGET_SECONDS = 3600

# Per-provider exception tuples for the unified retry helper. Imports are
# inside the helper to keep openai optional at module import time (the
# package is in scripts/test_corpora/pyproject.toml but isn't imported
# until a provider asks for it).


def _retryable_exceptions(client_kind: str) -> tuple[type[BaseException], ...]:
    """Return the exception classes treated as transient for `client_kind`."""
    if client_kind == "anthropic":
        return (OverloadedError, RateLimitError)
    if client_kind == "openai":
        import openai

        # openai.RateLimitError covers 429; openai.APIStatusError covers other
        # transient statuses (502/503). We narrow to "transient" inside the
        # retry loop based on .status_code.
        return (openai.RateLimitError, openai.APIStatusError)
    raise ValueError(f"unknown client_kind {client_kind!r}; supported: anthropic, openai")


def _with_provider_retry(
    fn,
    *args,
    client_kind: str = "anthropic",
    sleep=time.sleep,
    max_total_seconds: int = ANTHROPIC_RETRY_BUDGET_SECONDS,
):
    """Call ``fn(*args)``, retrying on transient API errors from `client_kind`.

    `client_kind` ∈ {"anthropic", "openai"} — picks the exception classes to
    catch. Backoff schedule is the same across providers: 60s, 120s, 240s,
    capped at 600s/attempt, with a cumulative budget of `max_total_seconds`
    (~1 hr default). On budget exhaustion the original exception is re-raised
    so the caller can leave the unit PENDING for a later --resume.

    ``sleep`` is injectable so tests need not wait in real time.
    """
    transient = _retryable_exceptions(client_kind)
    attempt = 0
    waited = 0.0
    while True:
        try:
            return fn(*args)
        except transient as exc:
            # For openai.APIStatusError, narrow to truly transient statuses;
            # a 400 bad-request shouldn't be retried.
            status = getattr(exc, "status_code", None)
            if client_kind == "openai" and status is not None and status not in (429, 500, 502, 503, 504):
                raise
            if waited >= max_total_seconds:
                raise
            delay = min(ANTHROPIC_RETRY_BASE_SECONDS * 2**attempt, ANTHROPIC_RETRY_MAX_DELAY_SECONDS)
            delay = min(delay, max_total_seconds - waited)
            log.warning(
                "%s API returned HTTP %s — backing off %.0fs before retry "
                "(attempt %d, %.0fs of %ds budget used)",
                client_kind,
                status,
                delay,
                attempt + 1,
                waited,
                max_total_seconds,
            )
            sleep(delay)
            waited += delay
            attempt += 1


def _with_anthropic_retry(
    fn,
    *args,
    sleep=time.sleep,
    max_total_seconds: int = ANTHROPIC_RETRY_BUDGET_SECONDS,
):
    """Back-compat alias for callers that haven't yet switched to
    _with_provider_retry. Functionally identical to
    _with_provider_retry(client_kind='anthropic', ...).
    """
    return _with_provider_retry(
        fn,
        *args,
        client_kind="anthropic",
        sleep=sleep,
        max_total_seconds=max_total_seconds,
    )
```

- [ ] **Step 5: Run the overload-retry test file**

```bash
cd /Users/alex/mcp-gateway && uv run --project scripts/test_corpora --extra test pytest scripts/test_corpora/tests/test_sweep_overload_retry.py -v
```

Expected: all tests (existing + 4 new) pass.

- [ ] **Step 6: Run the full test suite to catch any regressions in sweep callers**

```bash
cd /Users/alex/mcp-gateway && uv run --project scripts/test_corpora --extra test pytest scripts/test_corpora/tests/ -q
```

Expected: same pass count as the pre-PR baseline plus the new tests (4 base + 8 factory + 5 openai + 4 retry = 21 new tests), and the same 1 pre-existing `test_entity_overlap_english` failure.

- [ ] **Step 7: Lint + format**

```bash
cd /Users/alex/mcp-gateway && uv run ruff check scripts/test_corpora/runner/sweep.py scripts/test_corpora/tests/test_sweep_overload_retry.py && uv run ruff format --check scripts/test_corpora/runner/sweep.py scripts/test_corpora/tests/test_sweep_overload_retry.py
```

Expected: clean.

- [ ] **Step 8: Commit**

```bash
cd /Users/alex/mcp-gateway && git add scripts/test_corpora/runner/sweep.py scripts/test_corpora/tests/test_sweep_overload_retry.py && git commit -m "feat(eval): provider-aware retry in sweep — handles openai exceptions" -m "Generalizes _with_anthropic_retry to _with_provider_retry(client_kind=...) so OpenAI's RateLimitError (429) and APIStatusError (502/503/504) get the same exponential backoff. _with_anthropic_retry stays as a back-compat alias — callers that target only Anthropic keep working unchanged." -m "Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: Full-suite regression sanity check

**No new files or code — just verification.**

- [ ] **Step 1: Run the full test_corpora suite**

```bash
cd /Users/alex/mcp-gateway && uv run --project scripts/test_corpora --extra test pytest scripts/test_corpora/tests/ -q 2>&1 | tail -10
```

Expected: One pre-existing failure (`test_entity_overlap_english` — missing spaCy model in test venv); everything else green. Roughly 21 new tests for PR-C land on top of the 317 baseline.

- [ ] **Step 2: Root-level ruff check + format check on all changed files**

```bash
cd /Users/alex/mcp-gateway && uv run ruff check scripts/test_corpora/ && uv run ruff format --check scripts/test_corpora/
```

Expected: clean across the whole `scripts/test_corpora/` tree.

- [ ] **Step 3: No commit needed — this is a verification gate**

If any of the above failed, return to the relevant earlier task and fix; don't proceed to Task 10 (live run) on a yellow signal.

---

## Task 10: Live end-to-end run — gpt-4o against synthetic

**Prerequisites:**
- HC must be up + responsive at `https://localhost:8100`.
- `OPENAI_API_KEY` env var must be set to a valid OpenAI key.
- `HC_API_KEY` must be a Synthetic-scoped HC API key.
- PR-B's frozen `scripts/test_corpora/groundtruth/synthetic.yaml` must be on disk (it ships in PR-B's branch; this branch is based on main so it WON'T have it yet — fetch it from PR-B's branch first).

- [ ] **Step 1: Verify HC is reachable**

```bash
cd /Users/alex/mcp-gateway && curl -sk https://localhost:8100/api/system/health | head -50
```

Expected: JSON health response. If HC is down, restart the menubar app and re-run.

- [ ] **Step 2: Fetch the synthetic.yaml from PR-B's branch**

PR-B's frozen synthetic.yaml isn't on this branch yet. Cherry-pick it (or copy from the worktree) so the live eval has ground truth:

```bash
cd /Users/alex/mcp-gateway && git fetch origin feat/answer-eval-synthetic --quiet && git checkout origin/feat/answer-eval-synthetic -- scripts/test_corpora/groundtruth/synthetic.yaml && ls -la scripts/test_corpora/groundtruth/synthetic.yaml
```

Expected: file present, ~29 items long.

- [ ] **Step 3: Commit the ground-truth fetch on this branch**

```bash
cd /Users/alex/mcp-gateway && git add scripts/test_corpora/groundtruth/synthetic.yaml && git commit -m "chore(eval): pull synthetic.yaml from PR-B branch for live gpt-4o run" -m "PR-B (feat/answer-eval-synthetic) is the canonical source of this file; we copy it here so PR-C's live validation has ground truth to score against. When PR-B merges into main, this commit becomes a no-op and can be rebased away." -m "Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

- [ ] **Step 4: Run the live eval against gpt-4o (LLM spend authorized)**

The user authorized LLM spend for this run. Substitute the env vars (DON'T commit them — the values live only in your shell):

```bash
cd /Users/alex/mcp-gateway && OPENAI_API_KEY="$OPENAI_API_KEY" \
    HC_API_KEY="$SYNTHETIC_HC_API_KEY" \
    uv run --project scripts/test_corpora python -m scripts.test_corpora.runner.sweep \
    --mode answer-eval \
    --corpora synthetic \
    --models gpt-4o \
    --label pr-c-synthetic-gpt4o \
    --workdir "$HOME/Library/Application Support/Harbor Clerk/test-corpora" \
    --api-base https://localhost:8100 \
    --insecure 2>&1 | tee /tmp/pr-c-gpt4o-run.log
```

Expected: 29 items captured + judged. Each item logs `correctness=N grounded=N complete=N`; final line is `OVERALL n=29 correctness=... groundedness=... completeness=...`.

If the run dies partway, re-run the same command — captures and verdicts are reused by default (the answer-eval runner reads existing `captures/<corpus>/<model>/<id>.json` and skips re-capture).

- [ ] **Step 5: Save the summary table for the PR comment**

```bash
cd /Users/alex/mcp-gateway && cat "$HOME/Library/Application Support/Harbor Clerk/test-corpora/answer-eval/reports/pr-c-synthetic-gpt4o/summary.json"
```

Expected: JSON with `overall` + `by_type` sub-blocks of `n / correctness / groundedness / completeness`.

- [ ] **Step 6: Optional — also re-run Sonnet for head-to-head**

If the user wants a head-to-head comparison (LLM spend already authorized), run the same command with `--models claude-sonnet-4-6 --label pr-c-synthetic-sonnet`. Outputs land in a sibling directory. Skip for now if you want to ship PR-C faster — Sonnet's PR-B numbers are already in PR #386's description.

---

## Task 11: Self-review, fresh-eyes code review, open PR, post results

- [ ] **Step 1: Diff summary against `main`**

```bash
cd /Users/alex/mcp-gateway && git fetch origin main --quiet && git log --oneline origin/main..HEAD && echo "---" && git diff --stat origin/main..HEAD
```

Expected: ~8 commits, touching the files listed in the spec's File Structure section.

- [ ] **Step 2: Final lint + format pass on all changed files**

```bash
cd /Users/alex/mcp-gateway && uv run ruff check scripts/test_corpora/ && uv run ruff format --check scripts/test_corpora/
```

Expected: clean.

- [ ] **Step 3: Dispatch fresh-eyes code review subagent** (per MEMORY.md standing directive — minimal prompt, no carve-outs)

Use the Agent tool with `subagent_type: feature-dev:code-reviewer` and a minimal prompt: "Review the diff `git diff origin/main..HEAD` on branch `feat/answer-eval-multi-model`. Spec is at `docs/superpowers/specs/2026-05-23-answer-eval-multi-model-design.md`. Identify bugs, security issues, design problems with ≥80 confidence."

Address ≥80-confidence findings inline before opening the PR.

- [ ] **Step 4: Push the branch**

```bash
cd /Users/alex/mcp-gateway && git push -u origin feat/answer-eval-multi-model 2>&1 | tail -5
```

Already pushed in the brainstorm phase; this is a no-op `Everything up-to-date` or a fast-forward push of the new commits.

- [ ] **Step 5: Open the PR**

```bash
cd /Users/alex/mcp-gateway && gh pr create --base main --head feat/answer-eval-multi-model --title "feat(eval): answer-eval phase 2c — multi-model via Provider protocol" --body "$(cat <<'EOF'
## Summary

**Phase 2c / PR-C** of the answer-eval work — adds a second baseline model (`gpt-4o`) to `--mode answer-eval` via a pluggable Provider protocol. Spec: `docs/superpowers/specs/2026-05-23-answer-eval-multi-model-design.md`. Plan: `docs/superpowers/plans/2026-05-23-answer-eval-multi-model.md`.

Zero changes to the judge, runner loop, ground-truth schemas, or `--label`/`--workdir` machinery — additive only.

## What's in it

- **`providers/` package** — `Provider` Protocol + `BaselineResult` dataclass + `DEFAULT_SYSTEM_PROMPT` in `base.py`; concrete `AnthropicProvider` and `OpenAIProvider`; `make_provider(model, *, mcp_session)` factory dispatching by model-name prefix (`claude-*` → Anthropic, `gpt-*` / `o1-*` / `o3-*` → OpenAI).
- **`claude_baseline.py` collapsed to a 12-line shim** re-exporting `AnthropicProvider as BaselineGenerator` — `sweep.py`'s 30+ import sites stay untouched.
- **`answer_eval._live_capture_fn` uses `make_provider`** — pass any supported model name via `--models`.
- **Provider-aware retry** — `_with_provider_retry(fn, *, client_kind=...)` generalizes `_with_anthropic_retry` to handle OpenAI's `RateLimitError` (429) + `APIStatusError` (502/503/504). Legacy `_with_anthropic_retry` kept as an alias.
- **21 new tests** (4 base + 8 factory + 5 openai + 4 retry); pre-existing 317 still pass.

## Test plan

- [x] `uv run --project scripts/test_corpora --extra test pytest scripts/test_corpora/tests/` — all green except the pre-existing `test_entity_overlap_english` failure (spaCy model missing in test venv).
- [x] Root-level `ruff check` + `ruff format --check` clean on every changed file.
- [x] Built TDD with per-task spec + code-quality reviews and a final unconstrained fresh-eyes review.
- [x] **Live end-to-end run** — `gpt-4o` against synthetic (29 items). Results below.

## First-run results (live)

REPLACE-WITH-METRICS-FROM-STEP-5

## Comparison with Sonnet (PR-B baseline)

For head-to-head context, here are PR-B's first-run Sonnet numbers against the same 30-item synthetic set (one item was dropped during PR-B's post-review fix — gpt-4o ran against the 29-item version):

| | n | correctness | groundedness | completeness |
|---|--:|--:|--:|--:|
| Sonnet (PR-B)  | 30 | 3.33 | 3.57 | 2.73 |
| GPT-4o (this PR) | 29 | TBD | TBD | TBD |

Headline read: REPLACE-WITH-OBSERVATION.

## Known limitations / follow-ups

- The shim is intentional for this PR — a follow-up can migrate `sweep.py`'s `BaselineGenerator(...)` call sites to `make_provider(model)` directly, but that's a larger blast radius.
- Cross-provider judge bias: Sonnet judging gpt-4o introduces a same-family-vs-cross-family asymmetry. Future "swap the judge to gpt-4o" experiment for sensitivity analysis.
- Lifting the Provider protocol into `harbor_clerk` proper (for the "bring your own cloud LLM" use case in HC chat/research) is a separate effort.
- Prompt-tuning experiment is next on the queue — addresses the negative-hedging finding now confirmed across CUAD, Enron, and synthetic.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

Then capture the PR URL from the output.

- [ ] **Step 6: Edit the PR body with the actual metrics from Step 5**

Replace `REPLACE-WITH-METRICS-FROM-STEP-5` and the `TBD` cells with the values from `summary.json`. Use `gh pr edit <N> --body "$(cat <<EOF ... EOF)"` or `gh pr comment` if simpler.

- [ ] **Step 7: Mark PR-C done in the task tracker.**

Mark task BC2 (PR-C spec + plan + execution) completed.

---

## Self-Review Notes (for the agentic worker)

1. **Spec coverage:** Every section/requirement in `2026-05-23-answer-eval-multi-model-design.md` has a corresponding task:
   - Provider Protocol + BaselineResult → Task 1
   - AnthropicProvider → Task 2
   - claude_baseline shim → Task 3
   - make_provider factory → Task 4
   - OpenAIProvider + openai dep → Task 5
   - providers/__init__.py exports → Task 6
   - answer_eval wiring → Task 7
   - sweep retry generalization → Task 8
   - Validation + ruff → Task 9
   - Live gpt-4o run → Task 10
   - PR + fresh-eyes review → Task 11

2. **Placeholder scan:** No `TBD` / "TODO" / "implement later" in any code step. The PR body in Task 11 has two `REPLACE-WITH-...` placeholders by design — they get filled in Step 6 of Task 11 with real numbers from the live run.

3. **Type consistency:**
   - `BaselineResult` field set is identical in Task 1's definition, Task 2's import, Task 5's import, and Task 6's re-export.
   - `make_provider`'s signature `(model, *, mcp_session, doc_ids_seen=None)` is consistent in Task 4 (definition), Task 6 (re-export), and Task 7 (call site).
   - `_with_provider_retry`'s `client_kind` parameter values are consistent (`"anthropic"` / `"openai"`) in Task 8's definition and Task 8's tests.
   - `AnthropicProvider`/`OpenAIProvider` constructors both take `(*, mcp_session, model=..., client=None, doc_ids_seen=None)` — kwargs-only, same shape, swappable through the factory.

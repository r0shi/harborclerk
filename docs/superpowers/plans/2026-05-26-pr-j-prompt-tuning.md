# PR-J: MCP Docstring Tuning + Judge Render Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Strengthen `kb_search` / `kb_batch_search` MCP docstrings to address the two failure populations from the 2026-05-05-prod sweep (negative hedging + find-iteration short-stop), and fold in PR-G's deferred `render_prompt()` extraction so `cross_judge` stops duplicating `.format()` logic.

**Architecture:** Two docstring sections per search tool get appended bullets — surgical, no logic changes. `render_prompt()` becomes a module-level function in `answer_judge.py`, used by both `AnswerJudge.judge_answer` and `cross_judge._build_prompt`. Two new scripts under `scripts/test_corpora/runner/` (`sample_pr_j.py`, `rerun_pr_j.py`) drive a small before/after experiment; results land in the PR description.

**Tech Stack:** Python 3.12 / FastMCP / pytest. No new deps, no DB migration, no frontend changes.

**Spec:** `docs/superpowers/specs/2026-05-26-pr-j-prompt-tuning-design.md` (committed in `c8097c7`).

**Worktree:** `.worktrees/pr-j-prompt-tuning` on branch `feat/pr-j-prompt-tuning`.

---

## File map

| File | Action | Purpose |
|---|---|---|
| `src/harbor_clerk/mcp_server.py` | modify | Append to 4 docstring sections (2 per tool) |
| `tests/test_mcp_tool_descriptions.py` | modify | Add regression assertions for new docstring text |
| `scripts/test_corpora/runner/answer_judge.py` | modify | Extract `render_prompt()`; slim `AnswerJudge.judge_answer` |
| `scripts/test_corpora/runner/cross_judge.py` | modify | `_build_prompt` delegates to `render_prompt()` |
| `scripts/test_corpora/tests/test_answer_judge.py` | modify | Add `render_prompt()` unit tests |
| `scripts/test_corpora/tests/test_cross_judge.py` | modify | Add parity test (`_build_prompt` == `render_prompt`) |
| `scripts/test_corpora/runner/sample_pr_j.py` | create | Generate `pr_j_populations.json` from existing captures + ground-truth |
| `scripts/test_corpora/tests/test_sample_pr_j.py` | create | Unit tests for population filtering |
| `scripts/test_corpora/runner/rerun_pr_j.py` | create | Re-run baselines on a population, save captures under a labeled subdir |
| `scripts/test_corpora/tests/test_rerun_pr_j.py` | create | Smoke test with a mock provider |
| `scripts/test_corpora/runner/pr_j_populations.json` | create | Frozen sample (committed; reproducible) |

---

## Task 1: `kb_search` docstring — find-all iteration bullet + anti-pattern decline bullet

**Files:**
- Modify: `src/harbor_clerk/mcp_server.py:648-661` (the two sections inside `kb_search.__doc__`)
- Test: `tests/test_mcp_tool_descriptions.py` (add 3 new test functions)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mcp_tool_descriptions.py`:

```python
# ── PR-J: kb_search reinforcement ─────────────────────────────────────


def test_kb_search_iterate_calls_out_find_all_pagination():
    """For 'list/find all/enumerate' questions, the model must drain `has_more`
    via offset pagination rather than summarizing one page."""
    d = _doc(kb_search)
    assert "find all" in d or "enumerate" in d, "missing find-all iteration cue"
    # The drain instruction
    assert "offset" in d
    # Honesty cue for capped iteration
    assert "partial" in d or "cap" in d


def test_kb_search_decline_calls_out_anti_pattern_hedging():
    """PR-D added 'don't substitute closest match'; PR-J adds an explicit
    ANTI-PATTERN paragraph naming the hedge phrases models actually emit."""
    d = _doc(kb_search)
    assert "anti-pattern" in d, "missing explicit ANTI-PATTERN marker"
    # Pin two of the canonical hedge phrases we saw in the failure population
    assert "you may be interested" in d
    assert "closest match" in d
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/alex/mcp-gateway/.worktrees/pr-j-prompt-tuning && uv run pytest tests/test_mcp_tool_descriptions.py::test_kb_search_iterate_calls_out_find_all_pagination tests/test_mcp_tool_descriptions.py::test_kb_search_decline_calls_out_anti_pattern_hedging -v`

Expected: 2 FAIL — assertions trip on the absent text.

- [ ] **Step 3: Append the find-all bullet to "When to iterate"**

In `src/harbor_clerk/mcp_server.py`, the `kb_search` docstring currently ends "When to iterate" with the kb_read_passages bullet. After it, add a 4th bullet:

```python
    When to iterate:
      - One query returned ambiguous results across multiple docs → call `kb_batch_search`
        with varied query angles, OR use `metadata_filter` (from the discriminator_hint)
        to pin the right doc
      - `has_more` is true and you don't have the answer yet → paginate with `offset`
      - The top hit's chunk text doesn't fully answer the question → call
        `kb_read_passages` on the chunk_id to verify the surrounding text
      - The question asks to "list", "find all", "enumerate", or otherwise expects a
        complete set, AND `has_more` is true → ONE page is not the answer. Drain via
        `offset` pagination (or widen with `kb_batch_search`) until you've covered
        the relevant set. If you cap at a maximum, say so explicitly ("reviewed the
        top 50 of 287 matches") — don't present a partial set as if it were complete.
```

- [ ] **Step 4: Append the ANTI-PATTERN bullet to "How to decline"**

The current "How to decline" section is a single bullet. Add a second:

```python
    How to decline:
      - If retrieved chunks DON'T contain the answer the question asks for (e.g. the
        question mentions an invoice number / contract / person that doesn't appear in
        any retrieved doc), the information is NOT in the corpus — say so plainly. Do
        NOT report a "closest match" as a substitute. Adjacent or partial matches are
        not answers.
      - ANTI-PATTERN — do NOT pad a decline with adjacent-document suggestions.
        Phrases like "however, you may be interested in...", "the closest match
        is...", "while X isn't in the corpus, here's Y..." defeat the purpose of
        declining. If the answer isn't there, the decline IS the answer — stop
        there. The user asked a specific question; an adjacent document is not a
        partial-credit response.
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /Users/alex/mcp-gateway/.worktrees/pr-j-prompt-tuning && uv run pytest tests/test_mcp_tool_descriptions.py -v`

Expected: ALL pass (the 2 new + the existing PR-D ones still hold — the new text doesn't break "decline" / "iterate" / "has_more" / "metadata_filter" cues).

- [ ] **Step 6: Commit**

```bash
cd /Users/alex/mcp-gateway/.worktrees/pr-j-prompt-tuning
git add src/harbor_clerk/mcp_server.py tests/test_mcp_tool_descriptions.py
git commit -m "$(cat <<'EOF'
feat(mcp): strengthen kb_search decline + iterate guidance (PR-J §1)

Appends two bullets to the kb_search docstring targeting the failure
populations from the 2026-05-05-prod sweep:
  - "When to iterate" gets a find-all/enumerate pagination cue (drain via
    offset, report partial caps honestly).
  - "How to decline" gets an explicit ANTI-PATTERN paragraph naming the
    canonical hedge phrases ("you may be interested in...", "the closest
    match is...").

Builds on top of PR-D's existing decline + iterate sections. Regression
tests in test_mcp_tool_descriptions.py pin the cues against drift.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: `kb_batch_search` docstring — find-all parallel bullet + anti-pattern pointer

**Files:**
- Modify: `src/harbor_clerk/mcp_server.py:872-890` (sections inside `kb_batch_search.__doc__`)
- Test: `tests/test_mcp_tool_descriptions.py` (add 2 new test functions)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mcp_tool_descriptions.py`:

```python
def test_kb_batch_search_use_calls_out_find_all_parallel_path():
    """If a single kb_search left has_more=true on an enumeration question,
    kb_batch_search is the parallel widening alternative to offset pagination."""
    d = _doc(kb_batch_search)
    assert "enumeration" in d or "find every" in d or "list all" in d
    # Cross-reference the alternative to serial pagination
    assert "offset" in d or "serial" in d or "parallel" in d


def test_kb_batch_search_decline_references_kb_search_anti_pattern():
    """The kb_search ANTI-PATTERN applies per-query in batch responses too."""
    d = _doc(kb_batch_search)
    assert "anti-pattern" in d
    # The pointer to kb_search's wording
    assert "kb_search" in d
    assert "you may be interested" in d
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/alex/mcp-gateway/.worktrees/pr-j-prompt-tuning && uv run pytest tests/test_mcp_tool_descriptions.py::test_kb_batch_search_use_calls_out_find_all_parallel_path tests/test_mcp_tool_descriptions.py::test_kb_batch_search_decline_references_kb_search_anti_pattern -v`

Expected: 2 FAIL.

- [ ] **Step 3: Append find-all bullet to "When to use it"**

The current "When to use it" section has 2 bullets ("One kb_search returned ambiguous..." and "You need to check several related facts..."). Add a 3rd:

```python
    When to use it:
      - One kb_search returned ambiguous results from multiple docs → run 2-3
        varied queries here to see which doc consistently ranks first across
        angles (docs appearing in multiple batch queries are strongly
        corroborated as the right match)
      - You need to check several related facts in one go without serial
        round-trips
      - The question expects an enumeration ("list all X", "find every Y") and a
        single kb_search left `has_more=true` → run varied query angles here in
        parallel rather than serial-paginating with `offset`.
```

- [ ] **Step 4: Append anti-pattern pointer to "How to decline"**

The current "How to decline" is a single paragraph. Append:

```python
    How to decline:
      Same as kb_search — if NONE of your queries returned a doc matching the
      question's identifier (invoice number / contract / person / etc.), the
      information is NOT in the corpus. Say so plainly rather than reporting
      adjacent matches as substitutes.

      Same anti-pattern as kb_search — don't pad declines with "you may be
      interested in" suggestions across the per-query responses.
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /Users/alex/mcp-gateway/.worktrees/pr-j-prompt-tuning && uv run pytest tests/test_mcp_tool_descriptions.py -v`

Expected: ALL pass.

- [ ] **Step 6: Commit**

```bash
cd /Users/alex/mcp-gateway/.worktrees/pr-j-prompt-tuning
git add src/harbor_clerk/mcp_server.py tests/test_mcp_tool_descriptions.py
git commit -m "$(cat <<'EOF'
feat(mcp): mirror PR-J decline + iterate guidance to kb_batch_search (PR-J §2)

Mirrors the kb_search reinforcement to its batch sibling:
  - "When to use it" gets a find-all/enumeration parallel-widening bullet
    (the alternative to offset-paginating with kb_search).
  - "How to decline" gets a one-liner pointing back at kb_search's
    ANTI-PATTERN, applied per-query.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Extract `render_prompt()` in `answer_judge.py` + wire `AnswerJudge.judge_answer`

**Files:**
- Modify: `scripts/test_corpora/runner/answer_judge.py:87-152` (insert function before `AnswerVerdict`; slim `judge_answer`)
- Test: `scripts/test_corpora/tests/test_answer_judge.py` (add `render_prompt()` tests)

- [ ] **Step 1: Write the failing tests**

Append to `scripts/test_corpora/tests/test_answer_judge.py`:

```python
# ── PR-J: render_prompt() module-level function ───────────────────────


def test_render_prompt_lookup_includes_answer_key_and_qtype():
    from scripts.test_corpora.runner.answer_judge import render_prompt

    out = render_prompt(
        question="What is the governing law?",
        model_answer="Delaware.",
        cited="- contract.pdf",
        answer_key="Delaware",
        qtype="lookup",
    )
    assert "GROUND-TRUTH ANSWER KEY" in out
    assert "Delaware" in out
    assert "QUESTION TYPE: lookup" in out
    assert "What is the governing law?" in out


def test_render_prompt_find_includes_count_and_sample():
    from scripts.test_corpora.runner.answer_judge import render_prompt

    out = render_prompt(
        question="List every email from Houston",
        model_answer="3 emails found",
        cited="- email1.eml\n- email2.eml",
        answer_key={"count": 87, "all": [], "sample": ["e1.eml", "e2.eml"]},
        qtype="find",
    )
    assert "QUESTION TYPE: find" in out
    assert "count: 87" in out
    assert "e1.eml" in out
    assert "e2.eml" in out
    # find prompt sets completeness=0 deterministically
    assert "SET TO 0" in out


def test_render_prompt_negative_renders_NONE_for_missing_key():
    from scripts.test_corpora.runner.answer_judge import render_prompt

    out = render_prompt(
        question="Does this mention a non-compete?",
        model_answer="No mention.",
        cited="(no passages cited)",
        answer_key=None,
        qtype="negative",
    )
    assert "NONE" in out
    assert "QUESTION TYPE: negative" in out


def test_render_prompt_empty_model_answer_renders_placeholder():
    from scripts.test_corpora.runner.answer_judge import render_prompt

    out = render_prompt(
        question="q",
        model_answer="",
        cited="",
        answer_key="k",
        qtype="lookup",
    )
    # Both placeholders should appear
    assert "(empty)" in out
    assert "(no passages cited)" in out


def test_render_prompt_find_empty_sample_renders_empty_placeholder():
    """find-negative items (sample=[]) shouldn't crash — should render '(empty)'."""
    from scripts.test_corpora.runner.answer_judge import render_prompt

    out = render_prompt(
        question="q",
        model_answer="a",
        cited="c",
        answer_key={"count": 0, "all": [], "sample": []},
        qtype="find",
    )
    assert "count: 0" in out
    assert "(empty)" in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/alex/mcp-gateway/.worktrees/pr-j-prompt-tuning && uv run pytest scripts/test_corpora/tests/test_answer_judge.py::test_render_prompt_lookup_includes_answer_key_and_qtype -v`

Expected: FAIL with `ImportError: cannot import name 'render_prompt'`.

- [ ] **Step 3: Add the `render_prompt()` function**

In `scripts/test_corpora/runner/answer_judge.py`, insert this function between the `_PROMPT_FIND` constant (ends at line ~84) and the `@dataclasses.dataclass` decorator on `AnswerVerdict` (line ~87):

```python
def render_prompt(
    *,
    question: str,
    model_answer: str,
    cited: str,
    answer_key: str | dict | None,
    qtype: str,
) -> str:
    """Render the judge prompt for a single capture.

    Single source of truth for _PROMPT / _PROMPT_FIND .format() calls — used
    by both AnswerJudge.judge_answer and cross_judge._build_prompt so that any
    future prompt-text change lands in one place.
    """
    if qtype == "find":
        ak = answer_key if isinstance(answer_key, dict) else {"count": 0, "all": [], "sample": []}
        sample = ak.get("sample") or []
        rendered_sample = "\n".join(f"- {s}" for s in sample) or "(empty)"
        return _PROMPT_FIND.format(
            question=question,
            count=ak.get("count", 0),
            sample_size=len(sample),
            rendered_sample=rendered_sample,
            model_answer=model_answer or "(empty)",
            cited=cited or "(no passages cited)",
        )
    return _PROMPT.format(
        question=question,
        answer_key="NONE" if answer_key is None else answer_key,
        qtype=qtype,
        model_answer=model_answer or "(empty)",
        cited=cited or "(no passages cited)",
    )
```

- [ ] **Step 4: Refactor `AnswerJudge.judge_answer` to delegate**

In the same file, replace the body of `judge_answer` (currently lines ~118-152). The current method is:

```python
    def judge_answer(
        self, *, question: str, model_answer: str, cited: str, answer_key: str | dict | None, qtype: str
    ) -> AnswerVerdict:
        if qtype == "find":
            ak = answer_key if isinstance(answer_key, dict) else {"count": 0, "all": [], "sample": []}
            sample = ak.get("sample") or []
            rendered_sample = "\n".join(f"- {s}" for s in sample) or "(empty)"
            prompt = _PROMPT_FIND.format(
                question=question,
                count=ak.get("count", 0),
                sample_size=len(sample),
                rendered_sample=rendered_sample,
                model_answer=model_answer or "(empty)",
                cited=cited or "(no passages cited)",
            )
        else:
            prompt = _PROMPT.format(
                question=question,
                answer_key="NONE" if answer_key is None else answer_key,
                qtype=qtype,
                model_answer=model_answer or "(empty)",
                cited=cited or "(no passages cited)",
            )
        msg = self._client.messages.create(
            model=self._model,
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        data = _extract_json(msg.content[0].text)
        return AnswerVerdict(
            correctness=_score(data, "correctness"),
            groundedness=_score(data, "groundedness"),
            completeness=_score(data, "completeness"),
            rationale=str(data.get("rationale", "")),
        )
```

Replace with:

```python
    def judge_answer(
        self, *, question: str, model_answer: str, cited: str, answer_key: str | dict | None, qtype: str
    ) -> AnswerVerdict:
        prompt = render_prompt(
            question=question,
            model_answer=model_answer,
            cited=cited,
            answer_key=answer_key,
            qtype=qtype,
        )
        msg = self._client.messages.create(
            model=self._model,
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        data = _extract_json(msg.content[0].text)
        return AnswerVerdict(
            correctness=_score(data, "correctness"),
            groundedness=_score(data, "groundedness"),
            completeness=_score(data, "completeness"),
            rationale=str(data.get("rationale", "")),
        )
```

- [ ] **Step 5: Run all answer_judge tests (new + existing must pass)**

Run: `cd /Users/alex/mcp-gateway/.worktrees/pr-j-prompt-tuning && uv run pytest scripts/test_corpora/tests/test_answer_judge.py -v`

Expected: ALL pass. The existing tests (test_judge_parses_scores, test_judge_handles_negative_item, test_judge_find_type_renders_count_and_sample_in_prompt, etc.) still pass because the behavior is unchanged — same prompt text, same parsing, same return shape.

- [ ] **Step 6: Commit**

```bash
cd /Users/alex/mcp-gateway/.worktrees/pr-j-prompt-tuning
git add scripts/test_corpora/runner/answer_judge.py scripts/test_corpora/tests/test_answer_judge.py
git commit -m "$(cat <<'EOF'
refactor(eval): extract render_prompt() in answer_judge (PR-J §3)

Pulls _PROMPT / _PROMPT_FIND .format() logic out of AnswerJudge.judge_answer
into a module-level render_prompt() function. Behavior unchanged — pure
refactor — but cross_judge.py can now call render_prompt() directly in §4
instead of duplicating the format dispatch.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Wire `cross_judge._build_prompt` to `render_prompt()`

**Files:**
- Modify: `scripts/test_corpora/runner/cross_judge.py:23-28` (imports) and `cross_judge.py:58-80` (`_build_prompt`)
- Test: `scripts/test_corpora/tests/test_cross_judge.py` (add parity test)

- [ ] **Step 1: Write the failing parity test**

Append to `scripts/test_corpora/tests/test_cross_judge.py`:

```python
# ── PR-J: _build_prompt delegates to render_prompt ────────────────────


def test_build_prompt_delegates_to_render_prompt():
    """A capture-shaped dict + qtype + answer_key passed through _build_prompt
    must produce byte-identical output to a direct render_prompt() call."""
    from scripts.test_corpora.runner.answer_judge import render_prompt
    from scripts.test_corpora.runner.cross_judge import _build_prompt

    cap = {
        "question": "What's the governing law?",
        "answer": "Delaware",
        "cited_doc_titles": ["contract.pdf", "amendment.pdf"],
    }
    via_wrapper = _build_prompt(cap, qtype="lookup", answer_key="Delaware")
    via_render = render_prompt(
        question="What's the governing law?",
        model_answer="Delaware",
        cited="- contract.pdf\n- amendment.pdf",
        answer_key="Delaware",
        qtype="lookup",
    )
    assert via_wrapper == via_render


def test_build_prompt_find_qtype_matches_render_prompt():
    """Same parity check for the find branch."""
    from scripts.test_corpora.runner.answer_judge import render_prompt
    from scripts.test_corpora.runner.cross_judge import _build_prompt

    cap = {
        "question": "List Houston emails",
        "answer": "3 found",
        "cited_doc_titles": ["e1.eml"],
    }
    ak = {"count": 87, "all": [], "sample": ["e1.eml", "e2.eml"]}
    via_wrapper = _build_prompt(cap, qtype="find", answer_key=ak)
    via_render = render_prompt(
        question="List Houston emails",
        model_answer="3 found",
        cited="- e1.eml",
        answer_key=ak,
        qtype="find",
    )
    assert via_wrapper == via_render
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/alex/mcp-gateway/.worktrees/pr-j-prompt-tuning && uv run pytest scripts/test_corpora/tests/test_cross_judge.py::test_build_prompt_delegates_to_render_prompt scripts/test_corpora/tests/test_cross_judge.py::test_build_prompt_find_qtype_matches_render_prompt -v`

Expected: tests may PASS *coincidentally* (because `_build_prompt` already produces the same text). That's fine — we keep them as regression locks. If they pass, skip Step 3's "verify failure" expectation and proceed to Step 4 (the actual refactor); the tests will still serve as the parity assertion.

- [ ] **Step 3: Update imports in `cross_judge.py`**

In `scripts/test_corpora/runner/cross_judge.py`, the current import block (lines ~23-28) is:

```python
from scripts.test_corpora.runner.answer_judge import (
    _PROMPT,
    _PROMPT_FIND,
    _extract_json,
    _score,
)
```

Replace with:

```python
from scripts.test_corpora.runner.answer_judge import (
    _extract_json,
    _score,
    render_prompt,
)
```

- [ ] **Step 4: Refactor `_build_prompt` to a thin wrapper**

The current `_build_prompt` (lines ~58-80) is:

```python
def _build_prompt(capture: dict, *, qtype: str, answer_key: Any) -> str:
    """Render the same prompt AnswerJudge.judge_answer would build,
    given the capture + ground-truth answer_key + qtype."""
    cited = "\n".join(f"- {t}" for t in (capture.get("cited_doc_titles") or [])) or "(no passages cited)"
    if qtype == "find":
        ak = answer_key if isinstance(answer_key, dict) else {"count": 0, "all": [], "sample": []}
        sample = ak.get("sample") or []
        rendered_sample = "\n".join(f"- {s}" for s in sample) or "(empty)"
        return _PROMPT_FIND.format(
            question=capture.get("question", ""),
            count=ak.get("count", 0),
            sample_size=len(sample),
            rendered_sample=rendered_sample,
            model_answer=capture.get("answer") or "(empty)",
            cited=cited,
        )
    return _PROMPT.format(
        question=capture.get("question", ""),
        answer_key="NONE" if answer_key is None else answer_key,
        qtype=qtype,
        model_answer=capture.get("answer") or "(empty)",
        cited=cited,
    )
```

Replace with:

```python
def _build_prompt(capture: dict, *, qtype: str, answer_key: Any) -> str:
    """Render the judge prompt from a capture dict.

    Extracts the wire-shape fields (question / answer / cited_doc_titles) and
    delegates to answer_judge.render_prompt() — single source of truth for the
    _PROMPT / _PROMPT_FIND format dispatch.
    """
    cited = "\n".join(f"- {t}" for t in (capture.get("cited_doc_titles") or [])) or "(no passages cited)"
    return render_prompt(
        question=capture.get("question", ""),
        model_answer=capture.get("answer") or "(empty)",
        cited=cited,
        answer_key=answer_key,
        qtype=qtype,
    )
```

- [ ] **Step 5: Run the full cross_judge test suite**

Run: `cd /Users/alex/mcp-gateway/.worktrees/pr-j-prompt-tuning && uv run pytest scripts/test_corpora/tests/test_cross_judge.py -v`

Expected: ALL pass. Notably `test_rejudge_with_find_qtype_uses_find_prompt` still passes — it asserts "QUESTION TYPE: find" appears in the prompt, which is unchanged.

- [ ] **Step 6: Commit**

```bash
cd /Users/alex/mcp-gateway/.worktrees/pr-j-prompt-tuning
git add scripts/test_corpora/runner/cross_judge.py scripts/test_corpora/tests/test_cross_judge.py
git commit -m "$(cat <<'EOF'
refactor(eval): cross_judge._build_prompt delegates to render_prompt (PR-J §4)

cross_judge no longer duplicates the _PROMPT / _PROMPT_FIND .format()
dispatch — it calls answer_judge.render_prompt() with the capture's
fields. Drops the _PROMPT / _PROMPT_FIND imports. Behavior unchanged;
parity tests in test_cross_judge.py lock the equivalence.

Closes the PR-G follow-up parked for "the next prompt-tuning PR."

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: `sample_pr_j.py` — generate `pr_j_populations.json` from existing captures

**Files:**
- Create: `scripts/test_corpora/runner/sample_pr_j.py`
- Create: `scripts/test_corpora/tests/test_sample_pr_j.py`

**Purpose:** Inspect existing baseline captures (filtered by qtype + answer-pattern heuristics) and emit a frozen JSON describing the populations the experiment will re-run. Committed to the repo so anyone can reproduce.

- [ ] **Step 1: Write the failing tests**

Create `scripts/test_corpora/tests/test_sample_pr_j.py`:

```python
"""Unit tests for scripts/test_corpora/runner/sample_pr_j.py."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


HEDGE_MARKERS = ("however", "you may be interested", "closest match")


def _write_capture(dir_: Path, qid: str, *, answer: str, cited_titles: list[str]) -> None:
    dir_.mkdir(parents=True, exist_ok=True)
    payload = {
        "question_id": qid,
        "question": "q",
        "answer": answer,
        "cited_doc_ids": ["x"] * len(cited_titles),
        "cited_doc_titles": cited_titles,
        "tool_call_count": 1,
        "tool_transcript": [],
        "elapsed_seconds": 0.0,
        "model": "claude-sonnet-4-6",
        "timestamp": "2026-05-25T00:00:00Z",
    }
    (dir_ / f"{qid}.json").write_text(json.dumps(payload))


def _write_gt(path: Path, items: list[dict]) -> None:
    """Minimal ground-truth YAML stub the sampler can parse."""
    import yaml

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"questions": items}))


def test_negatives_hedged_filter_catches_hedge_markers(tmp_path):
    from scripts.test_corpora.runner.sample_pr_j import sample_populations

    cap_dir = tmp_path / "captures" / "enron" / "claude-sonnet-4-6"
    _write_capture(cap_dir, "n-clean", answer="Not present in the corpus.", cited_titles=[])
    _write_capture(
        cap_dir,
        "n-hedged",
        answer="Bitcoin is not in the corpus; however, you may be interested in emails about Enron.",
        cited_titles=["enron1.eml"],
    )
    gt_dir = tmp_path / "groundtruth"
    _write_gt(
        gt_dir / "enron.yaml",
        [
            {"id": "n-clean", "qtype": "negative", "question": "q", "answer_key": None},
            {"id": "n-hedged", "qtype": "negative", "question": "q", "answer_key": None},
        ],
    )

    out = sample_populations(
        captures_root=tmp_path / "captures",
        groundtruth_root=gt_dir,
        corpora=("enron",),
        model="claude-sonnet-4-6",
        max_per_population=10,
    )
    qids = {item["qid"] for item in out["negatives_hedged"]}
    assert qids == {"n-hedged"}, "clean decline should NOT be in the hedged population"


def test_finds_short_filter_requires_truth_count_and_short_citation(tmp_path):
    from scripts.test_corpora.runner.sample_pr_j import sample_populations

    cap_dir = tmp_path / "captures" / "enron" / "claude-sonnet-4-6"
    _write_capture(cap_dir, "f-deep", answer="here", cited_titles=[f"e{i}.eml" for i in range(20)])
    _write_capture(cap_dir, "f-short", answer="here", cited_titles=[f"e{i}.eml" for i in range(5)])
    _write_capture(cap_dir, "f-smalltruth", answer="here", cited_titles=["e1.eml"])
    gt_dir = tmp_path / "groundtruth"
    _write_gt(
        gt_dir / "enron.yaml",
        [
            {
                "id": "f-deep",
                "qtype": "find",
                "question": "q",
                "answer_key": {"count": 60, "all": [], "sample": []},
            },
            {
                "id": "f-short",
                "qtype": "find",
                "question": "q",
                "answer_key": {"count": 75, "all": [], "sample": []},
            },
            {
                "id": "f-smalltruth",
                "qtype": "find",
                "question": "q",
                "answer_key": {"count": 5, "all": [], "sample": []},
            },
        ],
    )

    out = sample_populations(
        captures_root=tmp_path / "captures",
        groundtruth_root=gt_dir,
        corpora=("enron",),
        model="claude-sonnet-4-6",
        max_per_population=10,
    )
    qids = {item["qid"] for item in out["finds_short"]}
    assert qids == {"f-short"}, "deep-citation OR small-truth must be excluded"


def test_max_per_population_caps_the_sample(tmp_path):
    """Sampling is bounded — large filtering populations get capped deterministically."""
    from scripts.test_corpora.runner.sample_pr_j import sample_populations

    cap_dir = tmp_path / "captures" / "enron" / "claude-sonnet-4-6"
    for i in range(30):
        _write_capture(
            cap_dir,
            f"n-h{i}",
            answer=f"not in corpus, however item {i} you may be interested in...",
            cited_titles=["x.eml"],
        )
    gt_dir = tmp_path / "groundtruth"
    _write_gt(
        gt_dir / "enron.yaml",
        [{"id": f"n-h{i}", "qtype": "negative", "question": "q", "answer_key": None} for i in range(30)],
    )

    out = sample_populations(
        captures_root=tmp_path / "captures",
        groundtruth_root=gt_dir,
        corpora=("enron",),
        model="claude-sonnet-4-6",
        max_per_population=10,
        seed=42,
    )
    assert len(out["negatives_hedged"]) == 10


def test_write_populations_emits_json_with_required_keys(tmp_path):
    from scripts.test_corpora.runner.sample_pr_j import sample_populations, write_populations

    populations = {"negatives_hedged": [], "finds_short": []}
    out_path = tmp_path / "pr_j_populations.json"
    write_populations(populations, out_path, model="claude-sonnet-4-6", corpora=("enron",))

    loaded = json.loads(out_path.read_text())
    assert "model" in loaded
    assert "corpora" in loaded
    assert "generated_at" in loaded
    assert loaded["negatives_hedged"] == []
    assert loaded["finds_short"] == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/alex/mcp-gateway/.worktrees/pr-j-prompt-tuning && uv run pytest scripts/test_corpora/tests/test_sample_pr_j.py -v`

Expected: FAIL with `ImportError: No module named 'scripts.test_corpora.runner.sample_pr_j'`.

- [ ] **Step 3: Create `sample_pr_j.py`**

Create `scripts/test_corpora/runner/sample_pr_j.py`:

```python
"""Sample the two PR-J failure populations from existing baseline captures.

Inputs:
  - captures_root: <workdir>/answer-eval/captures/  (per default_workdir())
  - groundtruth_root: scripts/test_corpora/groundtruth/  (the curated yaml set)

Output: scripts/test_corpora/runner/pr_j_populations.json — a frozen sample
that pins which (qid, corpus) pairs the rerun script will re-run.

Two populations:
  - negatives_hedged: qtype=="negative", baseline answer contains hedging
    markers (e.g., "however", "you may be interested", "closest match").
  - finds_short: qtype=="find", truth-doc count ≥ TRUTH_COUNT_THRESHOLD, and
    the baseline cited_doc_titles length ≤ CITATION_SHORT_THRESHOLD.

The script is deterministic — same captures + same seed → same population.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import random
import sys
from pathlib import Path

import yaml

log = logging.getLogger("sample_pr_j")

HEDGE_MARKERS: tuple[str, ...] = (
    "however",
    "you may be interested",
    "closest match",
)
TRUTH_COUNT_THRESHOLD = 50
CITATION_SHORT_THRESHOLD = 10


def _load_groundtruth(groundtruth_root: Path, corpus: str) -> dict[str, dict]:
    """Load <corpus>.yaml → {qid: {qtype, answer_key, ...}}."""
    yaml_path = groundtruth_root / f"{corpus}.yaml"
    if not yaml_path.exists():
        log.warning("ground-truth missing for corpus=%s at %s", corpus, yaml_path)
        return {}
    data = yaml.safe_load(yaml_path.read_text()) or {}
    items = data.get("questions") or []
    return {it["id"]: it for it in items}


def _iter_captures(captures_root: Path, *, corpus: str, model: str):
    """Yield each capture dict under captures_root/<corpus>/<model>/*.json."""
    cap_dir = captures_root / corpus / model
    if not cap_dir.exists():
        log.warning("no captures dir at %s", cap_dir)
        return
    for path in sorted(cap_dir.glob("*.json")):
        try:
            yield json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            log.warning("skipping unparseable capture %s: %s", path, exc)


def _is_hedged(answer: str) -> bool:
    """Case-insensitive scan for any HEDGE_MARKERS substring."""
    if not answer:
        return False
    lower = answer.lower()
    return any(marker in lower for marker in HEDGE_MARKERS)


def sample_populations(
    *,
    captures_root: Path,
    groundtruth_root: Path,
    corpora: tuple[str, ...],
    model: str,
    max_per_population: int,
    seed: int = 42,
) -> dict[str, list[dict]]:
    """Walk all captures across corpora; bucket into the two populations.

    Returns: {"negatives_hedged": [...], "finds_short": [...]}; each entry is
      {"qid": ..., "corpus": ..., "reason": <bookkeeping>}.
    """
    negatives_hedged: list[dict] = []
    finds_short: list[dict] = []

    for corpus in corpora:
        gt = _load_groundtruth(groundtruth_root, corpus)
        for cap in _iter_captures(captures_root, corpus=corpus, model=model):
            qid = cap.get("question_id")
            gt_item = gt.get(qid)
            if not gt_item:
                continue
            qtype = gt_item.get("qtype")
            answer = cap.get("answer") or ""
            cited_titles = cap.get("cited_doc_titles") or []

            if qtype == "negative" and _is_hedged(answer):
                negatives_hedged.append(
                    {
                        "qid": qid,
                        "corpus": corpus,
                        "reason": "hedge-marker-in-answer",
                    }
                )
            elif qtype == "find":
                ak = gt_item.get("answer_key") or {}
                truth_count = (ak or {}).get("count", 0)
                if truth_count >= TRUTH_COUNT_THRESHOLD and len(cited_titles) <= CITATION_SHORT_THRESHOLD:
                    finds_short.append(
                        {
                            "qid": qid,
                            "corpus": corpus,
                            "reason": f"truth={truth_count}, cited={len(cited_titles)}",
                        }
                    )

    rng = random.Random(seed)
    if len(negatives_hedged) > max_per_population:
        negatives_hedged = rng.sample(negatives_hedged, max_per_population)
    if len(finds_short) > max_per_population:
        finds_short = rng.sample(finds_short, max_per_population)

    # Sort by qid for stable ordering (random.sample is itself deterministic on
    # seeded input, but we serialize sorted for git-diff readability).
    negatives_hedged.sort(key=lambda d: d["qid"])
    finds_short.sort(key=lambda d: d["qid"])

    return {"negatives_hedged": negatives_hedged, "finds_short": finds_short}


def write_populations(
    populations: dict[str, list[dict]],
    out_path: Path,
    *,
    model: str,
    corpora: tuple[str, ...],
) -> None:
    """Serialize with provenance metadata."""
    payload = {
        "model": model,
        "corpora": list(corpora),
        "generated_at": dt.datetime.utcnow().isoformat() + "Z",
        "hedge_markers": list(HEDGE_MARKERS),
        "truth_count_threshold": TRUTH_COUNT_THRESHOLD,
        "citation_short_threshold": CITATION_SHORT_THRESHOLD,
        **populations,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2) + "\n")


def _default_workdir() -> Path:
    """Resolve the workdir the same way audit_answer_eval.py does."""
    import os

    env = os.environ.get("HARBOR_CLERK_WORKDIR")
    if env:
        return Path(env)
    # macOS native default
    return Path.home() / "Library" / "Application Support" / "Harbor Clerk" / "test-corpora"


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s sample_pr_j: %(message)s")
    p = argparse.ArgumentParser(description="Generate pr_j_populations.json from existing captures.")
    p.add_argument("--workdir", type=Path, default=None, help="Override HARBOR_CLERK_WORKDIR")
    p.add_argument(
        "--model",
        default="claude-sonnet-4-6",
        help="Baseline model whose captures we sample from",
    )
    p.add_argument(
        "--corpora",
        nargs="+",
        default=["cuad", "enron", "synthetic"],
        help="Corpora to scan",
    )
    p.add_argument("--max-per-population", type=int, default=25)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument(
        "--out",
        type=Path,
        default=Path("scripts/test_corpora/runner/pr_j_populations.json"),
    )
    args = p.parse_args(argv)

    workdir = args.workdir or _default_workdir()
    captures_root = workdir / "answer-eval" / "captures"
    groundtruth_root = Path("scripts/test_corpora/groundtruth")

    populations = sample_populations(
        captures_root=captures_root,
        groundtruth_root=groundtruth_root,
        corpora=tuple(args.corpora),
        model=args.model,
        max_per_population=args.max_per_population,
        seed=args.seed,
    )
    write_populations(populations, args.out, model=args.model, corpora=tuple(args.corpora))

    log.info(
        "wrote %s — negatives_hedged=%d, finds_short=%d",
        args.out,
        len(populations["negatives_hedged"]),
        len(populations["finds_short"]),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd /Users/alex/mcp-gateway/.worktrees/pr-j-prompt-tuning && uv run pytest scripts/test_corpora/tests/test_sample_pr_j.py -v`

Expected: ALL pass.

- [ ] **Step 5: Generate the real `pr_j_populations.json` against the user's captures**

Run: `cd /Users/alex/mcp-gateway/.worktrees/pr-j-prompt-tuning && uv run python -m scripts.test_corpora.runner.sample_pr_j --model claude-sonnet-4-6 --corpora cuad enron synthetic --out scripts/test_corpora/runner/pr_j_populations.json`

Expected: log output `wrote scripts/test_corpora/runner/pr_j_populations.json — negatives_hedged=N, finds_short=M`. If both counts are 0, investigate (HARBOR_CLERK_WORKDIR not pointing at captures, or no captures yet). Pinch-point: a real-data review of the populations the sampler picked is worthwhile before committing.

- [ ] **Step 6: Commit script + tests + frozen populations**

```bash
cd /Users/alex/mcp-gateway/.worktrees/pr-j-prompt-tuning
git add scripts/test_corpora/runner/sample_pr_j.py scripts/test_corpora/tests/test_sample_pr_j.py scripts/test_corpora/runner/pr_j_populations.json
git commit -m "$(cat <<'EOF'
feat(eval): sample_pr_j.py — derive PR-J failure populations from captures (PR-J §5)

New script samples two populations from existing baseline captures:
  - negatives_hedged: qtype=negative + hedge markers in answer
  - finds_short: qtype=find + truth_count >= 50 + cited_titles <= 10

Output committed as pr_j_populations.json so the experiment is reproducible.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: `rerun_pr_j.py` — re-run baselines on the population and save under a labeled subdir

**Files:**
- Create: `scripts/test_corpora/runner/rerun_pr_j.py`
- Create: `scripts/test_corpora/tests/test_rerun_pr_j.py`

**Purpose:** Read `pr_j_populations.json`, look up each qid in the ground truth, run the corresponding question through the configured provider, and save the capture under `<workdir>/answer-eval/captures/pr-j-prompt-tuning/<label>/<model>/<qid>.json`. The `<label>` lets us tag BEFORE (PR-D baseline) and AFTER (PR-J strengthened) runs separately.

- [ ] **Step 1: Write the failing tests**

Create `scripts/test_corpora/tests/test_rerun_pr_j.py`:

```python
"""Unit tests for scripts/test_corpora/runner/rerun_pr_j.py.

The provider call is mocked — we don't make real LLM calls in CI.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _populations_file(tmp_path: Path) -> Path:
    """Frozen populations stub with two qids."""
    pop = {
        "model": "claude-sonnet-4-6",
        "corpora": ["enron"],
        "generated_at": "2026-05-26T00:00:00Z",
        "hedge_markers": ["however"],
        "truth_count_threshold": 50,
        "citation_short_threshold": 10,
        "negatives_hedged": [{"qid": "n-1", "corpus": "enron", "reason": "test"}],
        "finds_short": [{"qid": "f-1", "corpus": "enron", "reason": "test"}],
    }
    p = tmp_path / "pop.json"
    p.write_text(json.dumps(pop))
    return p


def _gt_file(tmp_path: Path) -> Path:
    """Stub enron.yaml with the two qids."""
    import yaml

    gt = {
        "questions": [
            {"id": "n-1", "qtype": "negative", "question": "Q1?", "answer_key": None},
            {
                "id": "f-1",
                "qtype": "find",
                "question": "Q2?",
                "answer_key": {"count": 60, "all": [], "sample": ["a.eml"]},
            },
        ],
    }
    gt_dir = tmp_path / "groundtruth"
    gt_dir.mkdir()
    p = gt_dir / "enron.yaml"
    p.write_text(yaml.safe_dump(gt))
    return p


def test_rerun_saves_one_capture_per_qid_under_labeled_dir(tmp_path):
    from scripts.test_corpora.runner.providers.base import BaselineResult
    from scripts.test_corpora.runner.rerun_pr_j import rerun_populations

    pop_path = _populations_file(tmp_path)
    _gt_file(tmp_path)

    def _fake_provider_factory(model: str, *, mcp_session):
        mock = MagicMock()

        def _run(question: str, question_id: str, corpus: str) -> BaselineResult:
            return BaselineResult(
                question_id=question_id,
                question=question,
                answer=f"answer for {question_id}",
                cited_doc_ids=[],
                cited_doc_titles=[],
                tool_call_count=1,
                tool_transcript=[],
                elapsed_seconds=0.1,
                model=model,
                timestamp="2026-05-26T00:00:00Z",
            )

        mock.run_question.side_effect = _run
        return mock

    captures_root = tmp_path / "captures"
    rerun_populations(
        populations_path=pop_path,
        groundtruth_root=tmp_path / "groundtruth",
        captures_root=captures_root,
        label="after-pr-j",
        model="claude-sonnet-4-6",
        provider_factory=_fake_provider_factory,
        mcp_session=None,
    )

    n_cap = captures_root / "pr-j-prompt-tuning" / "after-pr-j" / "claude-sonnet-4-6" / "n-1.json"
    f_cap = captures_root / "pr-j-prompt-tuning" / "after-pr-j" / "claude-sonnet-4-6" / "f-1.json"
    assert n_cap.exists(), "negatives capture not written"
    assert f_cap.exists(), "finds capture not written"

    n_data = json.loads(n_cap.read_text())
    assert n_data["question_id"] == "n-1"
    assert n_data["answer"] == "answer for n-1"


def test_rerun_skips_qids_missing_from_groundtruth(tmp_path, caplog):
    from scripts.test_corpora.runner.providers.base import BaselineResult
    from scripts.test_corpora.runner.rerun_pr_j import rerun_populations

    pop = {
        "model": "claude-sonnet-4-6",
        "corpora": ["enron"],
        "generated_at": "2026-05-26T00:00:00Z",
        "negatives_hedged": [{"qid": "missing-from-gt", "corpus": "enron", "reason": "x"}],
        "finds_short": [],
    }
    pop_path = tmp_path / "pop.json"
    pop_path.write_text(json.dumps(pop))
    (tmp_path / "groundtruth").mkdir()
    (tmp_path / "groundtruth" / "enron.yaml").write_text("questions: []\n")

    factory_called = [False]

    def _fake_factory(model, *, mcp_session):
        factory_called[0] = True
        return MagicMock()

    rerun_populations(
        populations_path=pop_path,
        groundtruth_root=tmp_path / "groundtruth",
        captures_root=tmp_path / "captures",
        label="after",
        model="claude-sonnet-4-6",
        provider_factory=_fake_factory,
        mcp_session=None,
    )
    # No captures should exist since the qid isn't resolvable
    assert not list((tmp_path / "captures").rglob("*.json"))


def test_rerun_provider_exception_records_error_capture(tmp_path):
    """A provider crash on one item must not abort the whole run; the failure
    is recorded in a capture with an `error` field."""
    from scripts.test_corpora.runner.rerun_pr_j import rerun_populations

    pop_path = _populations_file(tmp_path)
    _gt_file(tmp_path)

    def _failing_factory(model, *, mcp_session):
        m = MagicMock()
        m.run_question.side_effect = RuntimeError("provider boom")
        return m

    captures_root = tmp_path / "captures"
    rerun_populations(
        populations_path=pop_path,
        groundtruth_root=tmp_path / "groundtruth",
        captures_root=captures_root,
        label="after",
        model="claude-sonnet-4-6",
        provider_factory=_failing_factory,
        mcp_session=None,
    )
    n_cap = captures_root / "pr-j-prompt-tuning" / "after" / "claude-sonnet-4-6" / "n-1.json"
    assert n_cap.exists()
    data = json.loads(n_cap.read_text())
    assert "error" in data
    assert "provider boom" in data["error"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/alex/mcp-gateway/.worktrees/pr-j-prompt-tuning && uv run pytest scripts/test_corpora/tests/test_rerun_pr_j.py -v`

Expected: FAIL with `ImportError: No module named 'scripts.test_corpora.runner.rerun_pr_j'`.

- [ ] **Step 3: Create `rerun_pr_j.py`**

Create `scripts/test_corpora/runner/rerun_pr_j.py`:

```python
"""Re-run baselines on the PR-J populations under a labeled capture subdir.

Reads pr_j_populations.json, looks up each qid in <corpus>.yaml, runs the
question through the configured provider, and persists the capture under
  <captures_root>/pr-j-prompt-tuning/<label>/<model>/<qid>.json

Typical use:
  # 1) Generate the populations once.
  uv run python -m scripts.test_corpora.runner.sample_pr_j

  # 2) BEFORE: run on PR-D (no PR-J changes — check out main).
  git checkout main
  uv run python -m scripts.test_corpora.runner.rerun_pr_j --label before-pr-j

  # 3) AFTER: run on PR-J (this branch).
  git checkout feat/pr-j-prompt-tuning
  uv run python -m scripts.test_corpora.runner.rerun_pr_j --label after-pr-j

  # 4) Diff the captures and report deltas in the PR description.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("rerun_pr_j")


def _default_workdir() -> Path:
    env = os.environ.get("HARBOR_CLERK_WORKDIR")
    if env:
        return Path(env)
    return Path.home() / "Library" / "Application Support" / "Harbor Clerk" / "test-corpora"


def _load_groundtruth(groundtruth_root: Path, corpus: str) -> dict[str, dict]:
    yaml_path = groundtruth_root / f"{corpus}.yaml"
    if not yaml_path.exists():
        log.warning("missing groundtruth %s", yaml_path)
        return {}
    data = yaml.safe_load(yaml_path.read_text()) or {}
    return {it["id"]: it for it in (data.get("questions") or [])}


def _save_capture(out_dir: Path, qid: str, payload: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{qid}.json").write_text(json.dumps(payload, indent=2) + "\n")


def rerun_populations(
    *,
    populations_path: Path,
    groundtruth_root: Path,
    captures_root: Path,
    label: str,
    model: str,
    provider_factory: Callable[..., Any],
    mcp_session: Any,
) -> None:
    """Drive the experiment.

    provider_factory(model, *, mcp_session) → object with
      run_question(question, question_id, corpus) -> BaselineResult.
    """
    pop = json.loads(Path(populations_path).read_text())
    items: list[dict] = []
    items.extend(pop.get("negatives_hedged") or [])
    items.extend(pop.get("finds_short") or [])

    if not items:
        log.warning("populations file %s has no items", populations_path)
        return

    # Group by corpus to load ground truth once per corpus.
    by_corpus: dict[str, list[dict]] = {}
    for it in items:
        by_corpus.setdefault(it["corpus"], []).append(it)

    out_dir = captures_root / "pr-j-prompt-tuning" / label / model
    provider = provider_factory(model, mcp_session=mcp_session)

    for corpus, group in by_corpus.items():
        gt = _load_groundtruth(groundtruth_root, corpus)
        for item in group:
            qid = item["qid"]
            gt_item = gt.get(qid)
            if not gt_item:
                log.warning("qid %s not found in %s ground truth — skipping", qid, corpus)
                continue
            question = gt_item.get("question", "")
            try:
                result = provider.run_question(question, qid, corpus)
                _save_capture(out_dir, qid, dataclasses.asdict(result))
                log.info("captured %s (corpus=%s, label=%s)", qid, corpus, label)
            except Exception as exc:
                log.warning("provider failure on %s: %s", qid, exc)
                _save_capture(
                    out_dir,
                    qid,
                    {
                        "question_id": qid,
                        "corpus": corpus,
                        "error": str(exc),
                        "model": model,
                    },
                )


def _make_mcp_session(*, api_base: str, insecure: bool) -> Any:
    """Construct the MCP session the same way answer_eval._live_capture_fn does.

    Resolves auth from env: prefer HC_API_KEY; fall back to HC_USERNAME +
    HC_PASSWORD via HarborClerkClient.login(). Imported lazily so unit tests
    can run without the MCP stack.
    """
    from scripts.test_corpora.runner.client import HarborClerkClient, SyncMcpSession

    token = os.environ.get("HC_API_KEY")
    if not token:
        user, password = os.environ.get("HC_USERNAME"), os.environ.get("HC_PASSWORD")
        if not (user and password):
            raise RuntimeError(
                "rerun_pr_j needs HC_API_KEY (corpus-scoped) or HC_USERNAME + HC_PASSWORD in the environment"
            )
        hc = HarborClerkClient(api_base, verify=not insecure)
        hc.login(user, password)
        token = hc.get_bearer_token()

    mcp_url = os.environ.get("HC_MCP_URL") or f"{api_base}/mcp/mcp"
    return SyncMcpSession(url=mcp_url, headers={"Authorization": f"Bearer {token}"})


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s rerun_pr_j: %(message)s")
    p = argparse.ArgumentParser(description="Re-run baselines on PR-J populations under a labeled subdir.")
    p.add_argument("--workdir", type=Path, default=None)
    p.add_argument("--label", required=True, help="e.g. before-pr-j / after-pr-j")
    p.add_argument("--model", default="claude-sonnet-4-6")
    p.add_argument(
        "--api-base",
        default=os.environ.get("HC_API_BASE", "https://localhost"),
        help="HC HTTPS base URL (env: HC_API_BASE, default https://localhost)",
    )
    p.add_argument("--insecure", action="store_true", help="Skip TLS verify (self-signed dev cert)")
    p.add_argument(
        "--populations",
        type=Path,
        default=Path("scripts/test_corpora/runner/pr_j_populations.json"),
    )
    p.add_argument(
        "--groundtruth-root",
        type=Path,
        default=Path("scripts/test_corpora/groundtruth"),
    )
    args = p.parse_args(argv)

    workdir = args.workdir or _default_workdir()
    captures_root = workdir / "answer-eval" / "captures"

    # Lazy imports kept here so the test suite can mock provider_factory.
    from scripts.test_corpora.runner.providers import make_provider

    mcp_session = _make_mcp_session(api_base=args.api_base, insecure=args.insecure)

    # Wrap make_provider to reset the per-question `_cited` dict between items
    # (same pattern as answer_eval._live_capture_fn). Without this, cited_doc_ids
    # accumulate across the batch and contaminate later captures.
    def _factory(model: str, *, mcp_session):
        provider = make_provider(model, mcp_session=mcp_session)
        original_run = provider.run_question

        def _run(question: str, question_id: str, corpus: str):
            provider._cited = {}
            return original_run(question=question, question_id=question_id, corpus=corpus)

        provider.run_question = _run
        return provider

    rerun_populations(
        populations_path=args.populations,
        groundtruth_root=args.groundtruth_root,
        captures_root=captures_root,
        label=args.label,
        model=args.model,
        provider_factory=_factory,
        mcp_session=mcp_session,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd /Users/alex/mcp-gateway/.worktrees/pr-j-prompt-tuning && uv run pytest scripts/test_corpora/tests/test_rerun_pr_j.py -v`

Expected: ALL pass.

- [ ] **Step 5: Commit**

```bash
cd /Users/alex/mcp-gateway/.worktrees/pr-j-prompt-tuning
git add scripts/test_corpora/runner/rerun_pr_j.py scripts/test_corpora/tests/test_rerun_pr_j.py
git commit -m "$(cat <<'EOF'
feat(eval): rerun_pr_j.py — re-run baselines on PR-J populations (PR-J §6)

Reads pr_j_populations.json, runs each qid through the configured provider,
saves captures under <captures_root>/pr-j-prompt-tuning/<label>/<model>/.

The --label flag lets the BEFORE and AFTER runs land in sibling subdirs
so the comparison is a straightforward file diff.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Run the BEFORE/AFTER experiment (manual; deferred to post-merge if needed)

**No code changes — this is the runbook.** The PR ships with the docstring changes + refactor + scripts; the actual experiment can run before or after the PR merges depending on calendar. Document the runbook in `docs/superpowers/specs/2026-05-26-pr-j-prompt-tuning-design.md` §4 (already there).

- [ ] **Step 1: Verify captures exist for the configured model + corpora**

Run: `ls "$HARBOR_CLERK_WORKDIR/answer-eval/captures" 2>/dev/null || ls "$HOME/Library/Application Support/Harbor Clerk/test-corpora/answer-eval/captures"`

Expected: corpus directories listed (`cuad`, `enron`, `synthetic`).

- [ ] **Step 2: Run BEFORE — against `main` (pre-PR-J)**

```bash
cd /Users/alex/mcp-gateway   # parent repo, NOT the worktree
git fetch origin
git checkout main
uv run python -m scripts.test_corpora.runner.sample_pr_j --model claude-sonnet-4-6 --corpora cuad enron synthetic
uv run python -m scripts.test_corpora.runner.rerun_pr_j --label before-pr-j --model claude-sonnet-4-6
```

Note: this requires `sample_pr_j.py` and `rerun_pr_j.py` to be on `main` — they aren't yet. So the BEFORE run actually happens *after* the PR merges, against the prior commit (the parent of PR-J's squash commit). Capture the SHA of that commit when the time comes.

Expected: log shows `captured <qid> ...` lines for each population item. Captures land under `<captures_root>/pr-j-prompt-tuning/before-pr-j/claude-sonnet-4-6/`.

- [ ] **Step 3: Run AFTER — against PR-J**

```bash
cd /Users/alex/mcp-gateway
git checkout feat/pr-j-prompt-tuning   # or main if PR-J merged
uv run python -m scripts.test_corpora.runner.rerun_pr_j --label after-pr-j --model claude-sonnet-4-6
```

Expected: captures land under `<captures_root>/pr-j-prompt-tuning/after-pr-j/claude-sonnet-4-6/`.

- [ ] **Step 4: Hand-scan deltas**

```bash
cd /Users/alex/mcp-gateway
python3 - <<'PY'
import json
from pathlib import Path
import os

root = Path(os.environ.get("HARBOR_CLERK_WORKDIR",
    Path.home() / "Library" / "Application Support" / "Harbor Clerk" / "test-corpora"))
base = root / "answer-eval" / "captures" / "pr-j-prompt-tuning"
before = base / "before-pr-j" / "claude-sonnet-4-6"
after = base / "after-pr-j" / "claude-sonnet-4-6"

HEDGE = ("however", "you may be interested", "closest match")
def hedged(text): return any(m in text.lower() for m in HEDGE) if text else False

print("QID                              | before-hedged | after-hedged | before-cited | after-cited")
print("-" * 105)
for qid_path in sorted(before.glob("*.json")):
    qid = qid_path.stem
    b = json.loads(qid_path.read_text())
    a_path = after / qid_path.name
    if not a_path.exists():
        continue
    a = json.loads(a_path.read_text())
    print(f"{qid:32s} | {str(hedged(b.get('answer',''))):14s}| {str(hedged(a.get('answer',''))):13s}| "
          f"{len(b.get('cited_doc_titles',[])):13d}| {len(a.get('cited_doc_titles',[])):11d}")
PY
```

Expected: a table showing per-qid hedging status and citation count. Aggregate hedge-count and cited-count deltas, paste into the PR description.

- [ ] **Step 5: Re-judge AFTER captures (optional, for correctness deltas)**

Run: `cd /Users/alex/mcp-gateway && uv run python -m scripts.test_corpora.audit_answer_eval --label after-pr-j --baseline-model claude-sonnet-4-6`

Expected: `audit.md` lands under `<workdir>/answer-eval/reports/after-pr-j/`. Read its judge-score deltas and append to the PR description.

- [ ] **Step 6: Edit the PR description with experiment results**

```bash
gh pr view feat/pr-j-prompt-tuning --json url -q .url   # then edit via the UI
# OR
gh pr edit feat/pr-j-prompt-tuning --body "$(cat <<'EOF'
...prepend existing summary...

## Experiment

| Population | Before (PR-D) | After (PR-J) | Delta |
|---|---|---|---|
| negatives-hedged | hedged=N/25 | hedged=M/25 | M-N |
| finds-short | mean cited=X.X | mean cited=Y.Y | Y-X |

Token spend: $X.XX
EOF
)"
```

---

## Task 8: Full regression + ruff + fresh-eyes review + open PR

**Files:** none (verification + git ops).

- [ ] **Step 1: Run the full Python test suite**

Run: `cd /Users/alex/mcp-gateway/.worktrees/pr-j-prompt-tuning && uv run pytest -q`

Expected: ALL pass. New tests (5 in test_mcp_tool_descriptions, 5 in test_answer_judge, 2 in test_cross_judge, 4 in test_sample_pr_j, 3 in test_rerun_pr_j) included.

- [ ] **Step 2: Ruff lint + format**

Run: `cd /Users/alex/mcp-gateway/.worktrees/pr-j-prompt-tuning && uv run ruff check . && uv run ruff format --check .`

Expected: both pass clean.

- [ ] **Step 3: Frontend not touched — skip frontend checks**

No frontend changes in PR-J. Skip the `frontend/` lint/typecheck.

- [ ] **Step 4: Dispatch fresh-eyes code-reviewer (per standing directive)**

Per MEMORY.md "FRESH-EYES REVIEW BEFORE MERGING SUBSTANTIVE PRs" directive: dispatch a `feature-dev:code-reviewer` subagent with a minimal prompt against the branch tip.

```
Prompt template (use Agent tool with subagent_type=feature-dev:code-reviewer):

"Review the branch feat/pr-j-prompt-tuning (worktree
.worktrees/pr-j-prompt-tuning) against main. Report findings ≥80 confidence.
No focus areas — full pass."
```

Address ≥80-confidence findings before opening the PR. If the reviewer surfaces a real issue, fix it on the branch + commit + re-run regression (Step 1).

- [ ] **Step 5: Push the branch**

```bash
cd /Users/alex/mcp-gateway/.worktrees/pr-j-prompt-tuning
git push -u origin feat/pr-j-prompt-tuning
```

- [ ] **Step 6: Open the PR**

```bash
cd /Users/alex/mcp-gateway/.worktrees/pr-j-prompt-tuning
gh pr create --title "feat(mcp): strengthen kb_search decline + iterate guidance (PR-J)" --body "$(cat <<'EOF'
## Summary
- **`kb_search` docstring** gains an ANTI-PATTERN decline bullet (no more "you may be interested" hedging) and a find-all iterate bullet (drain `has_more` via `offset` for enumeration questions).
- **`kb_batch_search` docstring** mirrors both, with a parallel-widening alternative for find-all.
- **`render_prompt()` extracted** in `scripts/test_corpora/runner/answer_judge.py`; `cross_judge._build_prompt` delegates instead of duplicating the `.format()` dispatch. Closes the PR-G follow-up parked for "the next prompt-tuning PR."
- **Experiment scripts** (`sample_pr_j.py`, `rerun_pr_j.py`, frozen `pr_j_populations.json`) so before/after deltas are reproducible.

## Spec
`docs/superpowers/specs/2026-05-26-pr-j-prompt-tuning-design.md`

## Experiment
_To run post-merge — see Task 7 in the implementation plan. Results will be edited into this PR description._

## Test plan
- [x] Python tests pass (`uv run pytest -q`)
- [x] Ruff check + format clean
- [x] Fresh-eyes code review dispatched, ≥80-confidence findings addressed
- [ ] Manual experiment run (Task 7 runbook) — token spend logged in PR

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 7: Enable auto-merge --squash (per established workflow)**

```bash
gh pr merge --auto --squash
```

(Auto-merge waits for CI + branch protection; do not pass `--admin`.)

- [ ] **Step 8: Update memory `pr_followups.md`**

In `/Users/alex/.claude/projects/-Users-alex-mcp-gateway/memory/pr_followups.md`, add a PR-J entry listing:
- ✅ `cross_judge.py` prompt-render duplication (from PR-G) — closed by this PR
- Pending: post-merge experiment run + result write-up
- Pending (deferred per §6 of spec): per-provider seasoning, pagination depth bounds, CLI/SKILL mirror — only if experiment shows the docstring fix is insufficient

Commit message: `docs(memory): pr_followups updates for PR-J merge`.

---

## Self-review

**1. Spec coverage:**

| Spec section | Tasks |
|---|---|
| §1 kb_search docstring | Task 1 |
| §2 kb_batch_search docstring | Task 2 |
| §3.1 `render_prompt()` extraction | Task 3 |
| §3.2 AnswerJudge.judge_answer slim | Task 3 |
| §3.3 cross_judge wrapper | Task 4 |
| §4 experiment methodology | Tasks 5 + 6 + 7 |
| §5 tests — docstring assertions | Task 1 + Task 2 |
| §5 tests — render_prompt equivalence | Task 3 |
| §5 tests — `_build_prompt` parity | Task 4 |
| §5.4 verify commands | Task 8 |
| §6 out-of-scope follow-ups | Task 8 Step 8 (pr_followups) |

No gaps.

**2. Placeholder scan:**

- ✅ No "TBD", "TODO", "fill in details".
- ✅ All steps show actual code, not just descriptions.
- ✅ Commands are exact (paths, flags, expected output described).
- ⚠️ Task 7 contains "if the time comes" / "depending on calendar" language — that's intentional, since the experiment is a separate operational step. Acceptable.
- ⚠️ Task 7 Step 6 PR description template has `$X.XX` for token spend — placeholder filled at run-time, by design.

**3. Type consistency:**

- `render_prompt()` signature consistent across Tasks 3, 4 (same kwargs).
- `sample_populations()` returns `{"negatives_hedged": [...], "finds_short": [...]}` — same key names used in `rerun_pr_j.py`'s `rerun_populations()` reading code.
- `provider_factory(model, *, mcp_session)` signature consistent with the existing `make_provider` in `providers/factory.py`.
- `BaselineResult` import path consistent — `scripts.test_corpora.runner.providers.base` (matches the actual module).

No type drift detected.

---

## Notes for the executing engineer

- **`_PROMPT` placeholder verbiage in tests:** tests assert literal strings like `"count: 87"` and `"SET TO 0"`. These match the existing prompt text in `answer_judge.py` (`_PROMPT_FIND` template). Don't change the prompt wording in this PR; the experiment is testing the **agent** prompt (MCP docstrings), not the **judge** prompt.
- **Captures path differs by OS:** macOS native default is `~/Library/Application Support/Harbor Clerk/test-corpora`; Docker is wherever `HARBOR_CLERK_WORKDIR` is set. Both `sample_pr_j.py` and `rerun_pr_j.py` resolve via `_default_workdir()` so they work for both.
- **`pr_j_populations.json` may be empty if no qualifying captures exist** on the machine running Task 5 Step 5. If so, the script logs `negatives_hedged=0, finds_short=0` — investigate before committing the empty file (probably wrong `--corpora` or `--model`, or no captures for those combos).
- **Don't run the experiment from inside the worktree** — it touches captures under workdir, which is shared between worktrees. Run from the parent repo (`cd /Users/alex/mcp-gateway`) with the appropriate commit checked out.

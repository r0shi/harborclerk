# PR-J: MCP docstring tuning + judge-prompt render refactor

**Status:** spec
**Branch:** `feat/pr-j-prompt-tuning`
**Worktree:** `.worktrees/pr-j-prompt-tuning`
**Predecessors:** PR-D (#396, MCP tool descriptions + discriminator_hint), PR-G (#403, harness audit + cross-judge)
**Memory anchors:** `pr_followups.md` entries from PR #387 (negative hedging) and PR #403 (find-iteration); `project_research_engine_defects.md` (closed via #369 — separate engine work).

---

## Background

The `2026-05-05-prod` sweep flagged two systematic failures in *baseline* answers (Sonnet 4.6 + GPT-4o, the "gold" teachers our local-model evals are scored against):

1. **Negative hedging.** When the question's identifier (contract, person, invoice number) is genuinely absent from the corpus, the baselines correctly say so — and then pad the decline with "however, you may be interested in document X, which discusses related…". The judge sees an attempt to substitute adjacent docs for an answer that should be empty, and downgrades correctness.

2. **Find-iteration short-stop.** When the question expects an enumeration ("list every email mentioning Houston", truth-doc count ≥ 50), baselines often issue one `kb_search` call, summarize the top 10 hits, and stop — leaving 80%+ of the answer set on the table. `has_more=true` and `total_candidates: 287` in the tool response are ignored.

PR-D (merged 2026-05-24) added "How to decline" and "When to iterate" sections to the `kb_search` and `kb_batch_search` MCP tool docstrings. Those sections cover the basic pattern ("adjacent or partial matches are not answers") but were drafted before this evidence landed, so they don't call out the two specific anti-patterns above.

PR-G (merged 2026-05-25) noted that `cross_judge._build_prompt` duplicates the `.format()` logic from `AnswerJudge.judge_answer` and parked a refactor for "next prompt-tuning PR."

PR-J is that PR.

## Goal

Three concrete changes, one PR:

1. **Strengthen `kb_search` MCP docstring** — add explicit anti-pattern callouts to "How to decline" and a 4th bullet to "When to iterate" targeting find-all questions.
2. **Mirror to `kb_batch_search`** — same patterns apply through the batch path.
3. **Extract `render_prompt()`** — single source of truth for `_PROMPT` / `_PROMPT_FIND` rendering, called by both `AnswerJudge` and `cross_judge`.

Plus a small before/after experiment to confirm the docstring changes move the needle on the two failure populations (or not — null result is also a result).

## Non-goals

- **`DEFAULT_SYSTEM_PROMPT` in `scripts/test_corpora/runner/providers/base.py`** — eval-only layer, untouched. The MCP docstring is the production surface; if strengthening it doesn't fix the failure, layering a second harness-only prompt on top is just hiding the gap.
- **`skills/harbor-clerk/SKILL.md` and CLI help text** — already neutral pointers ("the model figures out which kb_* tool to call"). MCP docstrings reach AI agents via the protocol; we don't need to duplicate guidance.
- **The other 14 MCP tool docstrings** — only the two search tools where the failure populations live.
- **Local-model re-runs** — out of scope. Cost ($2 cap) goes to Sonnet + GPT-4o, the teachers the eval scores against. Local-model improvement is a separate evaluation track.
- **Research-engine defects** — closed via PR #369 (May 20). Unrelated to docstring tuning.

---

## §1 — `kb_search` docstring strengthening

**File:** `src/harbor_clerk/mcp_server.py` (function `kb_search`, lines ~619-689)

### 1.1 "When to iterate" — append 4th bullet

Current section ends with the "top hit doesn't fully answer" bullet. Append:

```
      - The question asks to "list", "find all", "enumerate", or otherwise expects
        a complete set, AND `has_more` is true → ONE page is not the answer. Drain
        via `offset` pagination (or widen with `kb_batch_search`) until you've
        covered the relevant set. If you cap at a maximum, say so explicitly
        ("reviewed the top 50 of 287 matches") — don't present a partial set as
        if it were complete.
```

### 1.2 "How to decline" — append anti-pattern paragraph

Current section is one bullet. Append a second bullet:

```
      - ANTI-PATTERN — do NOT pad a decline with adjacent-document suggestions.
        Phrases like "however, you may be interested in...", "the closest match
        is...", "while X isn't in the corpus, here's Y..." defeat the purpose
        of declining. If the answer isn't there, the decline IS the answer —
        stop there. The user asked a specific question; an adjacent document is
        not a partial-credit response.
```

## §2 — `kb_batch_search` docstring strengthening

**File:** `src/harbor_clerk/mcp_server.py` (function `kb_batch_search`, lines ~849-894)

### 2.1 "When to use it" — append find-all bullet

Append to the existing list:

```
      - The question expects an enumeration ("list all X", "find every Y") and a
        single kb_search left `has_more=true` → run varied query angles here in
        parallel rather than serial-paginating with `offset`.
```

### 2.2 "How to decline" — append anti-pattern one-liner

Append to the existing paragraph:

```
      Same anti-pattern as kb_search — don't pad declines with "you may be
      interested in" suggestions across the per-query responses.
```

## §3 — `render_prompt()` extraction

**Files:**
- `scripts/test_corpora/runner/answer_judge.py` (modify)
- `scripts/test_corpora/runner/cross_judge.py` (modify)

### 3.1 New module-level function

In `answer_judge.py`, between the `_PROMPT_FIND` constant and the `AnswerVerdict` dataclass:

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

### 3.2 `AnswerJudge.judge_answer` collapses

Body becomes:

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
    return AnswerVerdict(...)  # unchanged
```

### 3.3 `cross_judge._build_prompt` becomes a thin wrapper

```python
from scripts.test_corpora.runner.answer_judge import (
    _extract_json,
    _score,
    render_prompt,  # NEW import
)


def _build_prompt(capture: dict, *, qtype: str, answer_key: Any) -> str:
    cited = "\n".join(f"- {t}" for t in (capture.get("cited_doc_titles") or [])) or "(no passages cited)"
    return render_prompt(
        question=capture.get("question", ""),
        model_answer=capture.get("answer") or "(empty)",
        cited=cited,
        answer_key=answer_key,
        qtype=qtype,
    )
```

(Drops `_PROMPT`, `_PROMPT_FIND` from the import list since the wrapper doesn't reference them directly anymore.)

## §4 — Experiment (before/after)

**Goal:** confirm the docstring changes change baseline behavior on the two flagged populations. Null result is also publishable — it tells us the gap is elsewhere.

### 4.1 Populations

Drawn from prior `2026-05-05-prod` capture set. Two slices:

- **negatives-hedged**: ~25 items where qtype=="negative" AND the Sonnet or GPT-4o baseline answer contains hedging markers ("however", "you may be interested", "closest match"). Sampled proportionally across CUAD / Enron / synthetic.
- **finds-short**: ~25 items where qtype=="find" AND truth-doc count ≥ 50 AND the baseline's `cited_doc_titles` length ≤ 10. Same provenance.

Sample list pinned in `scripts/test_corpora/runner/pr_j_populations.json` (one-time generation script lives in `scripts/test_corpora/runner/sample_pr_j.py`, kept in-repo so future re-runs are reproducible).

### 4.2 Run matrix

```
2 models × 2 populations × 1 round = ~100 baseline calls
```

Models: `claude-sonnet-4-6`, `gpt-4o`. Captures saved under `workdir/answer-eval/captures/pr-j-prompt-tuning/<model>/<qid>.json`.

### 4.3 Scoring

Re-judge with `claude-sonnet-4-6` (same judge as the existing eval) using the unchanged `_PROMPT` / `_PROMPT_FIND` (we're testing the agent prompt, not the judge prompt). Per-population mean correctness + groundedness, plus a hand-pattern scan:

- `negatives-hedged`: count answers that still contain hedging markers
- `finds-short`: count answers with `cited_doc_titles` length > 10 (proxy for "did the agent iterate")

### 4.4 Cost

~$2 token budget (small samples, cached system prompts). Logged in the design doc when run.

### 4.5 Reporting

Markdown table in PR description:

| population        | baseline (PR-D) | strengthened (PR-J) | delta |
|-------------------|-----------------|---------------------|-------|
| negatives-hedged  | corr=X.X  hedged=N/25 | corr=Y.Y  hedged=M/25 | … |
| finds-short       | corr=X.X  cited≤10=N/25 | corr=Y.Y  cited≤10=M/25 | … |

## §5 — Testing

### 5.1 Docstring sections — golden-text regression

`tests/test_mcp_tool_docstrings.py` already asserts presence of "How to decline" / "When to iterate" headers (added in PR-D). Extend it:

```python
def test_kb_search_decline_calls_out_anti_pattern():
    doc = mcp_server.kb_search.__doc__ or ""
    assert "ANTI-PATTERN" in doc
    assert "adjacent-document suggestions" in doc

def test_kb_search_iterate_calls_out_find_all():
    doc = mcp_server.kb_search.__doc__ or ""
    assert "find all" in doc.lower() or "enumerate" in doc.lower()
    assert "drain via `offset`" in doc or "drain via offset" in doc.lower()

def test_kb_batch_search_decline_references_kb_search_pattern():
    doc = mcp_server.kb_batch_search.__doc__ or ""
    assert "Same anti-pattern as kb_search" in doc
```

These pin the prose strongly enough that an inadvertent revert breaks CI but loosely enough that copyediting doesn't.

### 5.2 `render_prompt()` — equivalence tests

`tests/test_answer_judge.py` (new file):

```python
def test_render_prompt_lookup_matches_inline_format():
    """Locked-in: render_prompt for non-find qtype produces the same string
    as the pre-extraction inline .format() call."""
    out = render_prompt(
        question="What is the term?",
        model_answer="24 months",
        cited="- contract.pdf",
        answer_key="24 months",
        qtype="lookup",
    )
    assert "GROUND-TRUTH ANSWER KEY" in out
    assert "24 months" in out
    assert "QUESTION TYPE: lookup" in out

def test_render_prompt_find_uses_sample_rendering():
    out = render_prompt(
        question="List every email from Houston",
        model_answer="3 emails found",
        cited="- email1\n- email2",
        answer_key={"count": 87, "all": [...], "sample": ["e1", "e2"]},
        qtype="find",
    )
    assert "count: 87" in out or "count is 87" in out.lower() or "(count: 87)" in out
    assert "completeness=0" in out or "SET TO 0" in out

def test_render_prompt_negative_renders_NONE_key():
    out = render_prompt(
        question="Does this mention a non-compete?",
        model_answer="No mention",
        cited="(no passages cited)",
        answer_key=None,
        qtype="negative",
    )
    assert "NONE" in out
```

### 5.3 `cross_judge._build_prompt` parity

Add to `tests/test_cross_judge.py`:

```python
def test_build_prompt_delegates_to_render_prompt():
    """Capture-shaped dict → same prompt text as direct render_prompt call."""
    cap = {
        "question": "Q",
        "answer": "A",
        "cited_doc_titles": ["doc1", "doc2"],
    }
    via_wrapper = _build_prompt(cap, qtype="lookup", answer_key="K")
    via_render = render_prompt(
        question="Q",
        model_answer="A",
        cited="- doc1\n- doc2",
        answer_key="K",
        qtype="lookup",
    )
    assert via_wrapper == via_render
```

### 5.4 Verification commands

```bash
# Python tests for §1, §2, §3 (the docstring + refactor pieces)
cd /Users/alex/mcp-gateway/.worktrees/pr-j-prompt-tuning
uv run pytest tests/test_mcp_tool_docstrings.py tests/test_answer_judge.py tests/test_cross_judge.py -v
uv run ruff check src/harbor_clerk/mcp_server.py scripts/test_corpora/runner/
uv run ruff format --check src/harbor_clerk/mcp_server.py scripts/test_corpora/runner/

# §4 experiment (manual, after the above lands clean)
uv run python -m scripts.test_corpora.runner.sample_pr_j --out scripts/test_corpora/runner/pr_j_populations.json
uv run python -m scripts.test_corpora.runner.rerun_pr_j --captures workdir/answer-eval/captures/pr-j-prompt-tuning/
```

## §6 — Out-of-scope follow-ups (capture in `pr_followups.md`)

- **If experiment shows no movement on `negatives-hedged`** — investigate model-specific behavior (maybe Sonnet 4.6 ignores docstring text differently than GPT-4o). Possible next PR: per-provider system-prompt seasoning at the MCP-tool docstring layer.
- **If `finds-short` improves but agents now over-paginate** — add `kb_search` runtime stats around pagination depth; bound it via docstring guidance ("don't iterate past 100 hits without a refined query").
- **CLI help re-narrative** — `cli/help/search.txt` currently has terse `kb_search`-oriented text; consider mirroring the iterate/decline language in a future docs-only PR if AI agents start using the CLI path instead of MCP.
- **`SKILL.md`** — same; only revisit if there's evidence agents read it instead of MCP descriptions.

---

## Architecture summary

| Layer | Touched? | Why |
|---|---|---|
| `DEFAULT_SYSTEM_PROMPT` (eval-only) | No | Production fix should land at MCP, not the harness |
| `kb_search` docstring | Yes (§1) | Where AI agents actually read tool guidance |
| `kb_batch_search` docstring | Yes (§2) | Sibling tool, same failure modes |
| `cli/help/search.txt` | No | Operational CLI, not the agent path |
| `skills/harbor-clerk/SKILL.md` | No | Already neutral; agents read MCP first |
| `answer_judge.py` | Yes (§3) | Extract `render_prompt()` |
| `cross_judge.py` | Yes (§3) | Delegate to `render_prompt()` |

## Tech stack

- Python 3.12 / FastMCP / pytest
- No new dependencies
- No DB migrations
- No frontend changes

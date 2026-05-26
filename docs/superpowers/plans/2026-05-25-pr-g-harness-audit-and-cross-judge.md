# PR-G: Harness Audit + Cross-Judge Sensitivity — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a standalone `audit_answer_eval.py` script that produces `audit.json` + `audit.md` from existing answer-eval captures and verdicts, with optional cross-judge re-scoring via gpt-4o.

**Architecture:** Three new files in `scripts/test_corpora/`. `runner/audit.py` exposes pure functions (`tool_use_stats`, `failure_correlation`, `citation_hygiene`) over already-loaded capture + verdict dicts. `runner/cross_judge.py` adds a `JudgeProvider` Protocol with an `OpenAIJudgeProvider` impl, a `rejudge_with()` function, and a pure `compare_judges()` stats function. The thin CLI `audit_answer_eval.py` handles I/O and report rendering.

**Tech Stack:** Python 3.12, `openai` SDK (already a dep, see PR-C), pytest + pytest-asyncio (already present), no scipy (Spearman + Cohen's kappa hand-rolled).

**Spec:** `docs/superpowers/specs/2026-05-25-pr-g-harness-audit-and-cross-judge-design.md`

---

## File Structure

**New files:**
- `scripts/test_corpora/runner/audit.py` — three pure analysis functions
- `scripts/test_corpora/runner/cross_judge.py` — `JudgeProvider` Protocol + `OpenAIJudgeProvider` + `rejudge_with` + `compare_judges`
- `scripts/test_corpora/audit_answer_eval.py` — CLI orchestrator
- `scripts/test_corpora/tests/test_audit.py` — fixture-driven unit tests for audit functions
- `scripts/test_corpora/tests/test_cross_judge.py` — MockProvider tests for rejudge + pure-data tests for compare_judges
- `scripts/test_corpora/tests/test_audit_cli.py` — CLI parsing + report rendering tests (uses tmp_path fixtures)

**Modified files:** none.

**Data contract reminders (verified against the live corpus):**

- Capture file at `<workdir>/answer-eval/captures/<corpus>/<model>/<qid>.json`:
  ```python
  {
    "question_id": str, "question": str, "answer": str,
    "cited_doc_ids": list[str], "cited_doc_titles": list[str],
    "tool_call_count": int,
    "tool_transcript": list[{"tool": str, "args": dict, "result_summary": str}],
    "elapsed_seconds": float, "model": str, "timestamp": str,
  }
  ```
  `result_summary` is a JSON-encoded string containing the tool's response (we treat it as opaque text for UUID extraction).

- Per-item verdict file at `<workdir>/answer-eval/verdicts/<corpus>/<model>/<qid>.json`:
  ```python
  {"correctness": int, "groundedness": int, "completeness": int,
   "rationale": str, "source": dict}
  ```
  Note: qid is **not** in the file; it's the filename stem.

- Aggregated `<workdir>/answer-eval/reports/<label>/detail.json`:
  ```python
  [{"id": str, "type": str, "correctness": int, ..., "rationale": str, "source": dict}, ...]
  ```
  The CLI orchestrator prefers `detail.json` as the verdict source (qid already joined). Falls back to per-item files if `detail.json` is missing.

- `_PROMPT` and `_PROMPT_FIND` are module-level constants in `scripts/test_corpora/runner/answer_judge.py`. Same for the private `_extract_json` + `_score` helpers. Import them; do not duplicate.

---

## Task 1: `runner/audit.py` — `tool_use_stats`

**Files:**
- Create: `scripts/test_corpora/runner/audit.py`
- Test:   `scripts/test_corpora/tests/test_audit.py`

- [ ] **Step 1: Write the failing tests**

```python
# scripts/test_corpora/tests/test_audit.py
"""Unit tests for scripts/test_corpora/runner/audit.py.

Fixture-driven: every test constructs the minimum capture/verdict shape
its function needs. No live LLM calls, no DB.
"""

from __future__ import annotations

from scripts.test_corpora.runner.audit import tool_use_stats


def _cap(qid: str, tools: list[str], **overrides) -> dict:
    """Build a minimum-shape capture for tests."""
    base = {
        "question_id": qid,
        "question": "q",
        "answer": "a",
        "cited_doc_ids": [],
        "cited_doc_titles": [],
        "tool_call_count": len(tools),
        "tool_transcript": [{"tool": t, "args": {}, "result_summary": "{}"} for t in tools],
        "elapsed_seconds": 0.0,
        "model": "test-model",
        "timestamp": "2026-05-25T00:00:00Z",
    }
    base.update(overrides)
    return base


def test_tool_use_stats_empty_captures():
    result = tool_use_stats([])
    assert result == {
        "total_captures": 0,
        "tool_call_distribution": {},
        "tool_call_counts_per_tool": {},
        "captures_by_tool_count": {},
    }


def test_tool_use_stats_single_capture_single_tool():
    caps = [_cap("q1", ["kb_search"])]
    result = tool_use_stats(caps)
    assert result["total_captures"] == 1
    assert result["tool_call_distribution"] == {1: 1}
    assert result["tool_call_counts_per_tool"] == {"kb_search": 1}
    assert result["captures_by_tool_count"]["q1"] == {
        "tool_count": 1,
        "tools_used": ["kb_search"],
    }


def test_tool_use_stats_mixed_captures_distribution():
    caps = [
        _cap("q0", []),
        _cap("q1", ["kb_search"]),
        _cap("q1b", ["kb_search"]),
        _cap("q2", ["kb_search", "kb_get_document"]),
        _cap("q4plus", ["kb_search"] * 5),
    ]
    result = tool_use_stats(caps)
    assert result["total_captures"] == 5
    # 0 -> 1, 1 -> 2, 2 -> 1, "4+" -> 1
    assert result["tool_call_distribution"] == {0: 1, 1: 2, 2: 1, "4+": 1}


def test_tool_use_stats_per_tool_counts():
    caps = [
        _cap("q1", ["kb_search", "kb_search", "kb_get_document"]),
        _cap("q2", ["kb_search"]),
    ]
    result = tool_use_stats(caps)
    assert result["tool_call_counts_per_tool"] == {
        "kb_search": 3,
        "kb_get_document": 1,
    }


def test_tool_use_stats_captures_by_tool_count_records_per_qid():
    caps = [_cap("q1", ["kb_search", "kb_get_document"])]
    result = tool_use_stats(caps)
    assert result["captures_by_tool_count"]["q1"]["tool_count"] == 2
    assert result["captures_by_tool_count"]["q1"]["tools_used"] == ["kb_search", "kb_get_document"]


def test_tool_use_stats_4_plus_bucket_includes_exactly_4_and_more():
    """The "4+" bin must catch tool_count == 4 AND tool_count >= 5."""
    caps = [
        _cap("q4", ["kb_search"] * 4),
        _cap("q5", ["kb_search"] * 5),
        _cap("q10", ["kb_search"] * 10),
    ]
    result = tool_use_stats(caps)
    assert result["tool_call_distribution"] == {"4+": 3}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest scripts/test_corpora/tests/test_audit.py -v`
Expected: `ModuleNotFoundError: No module named 'scripts.test_corpora.runner.audit'`

- [ ] **Step 3: Implement `tool_use_stats`**

```python
# scripts/test_corpora/runner/audit.py
"""Harness-audit pure functions.

Read-only analysis over already-loaded answer-eval captures + verdicts.
No I/O, no LLM calls — CLI orchestrator handles those. Each function
takes loaded dicts and returns a dict suitable for JSON-encoding.

Used by: scripts/test_corpora/audit_answer_eval.py.
"""

from __future__ import annotations


def tool_use_stats(captures: list[dict]) -> dict:
    """Aggregate tool-use distribution + per-tool counts across captures.

    Returns:
      {
        "total_captures": int,
        "tool_call_distribution": {0: N, 1: N, 2: N, ..., "4+": N},
        "tool_call_counts_per_tool": {"kb_search": N, ...},
        "captures_by_tool_count": {qid: {"tool_count": N, "tools_used": [...]}}
      }

    The "4+" bin keeps the structure stable across runs (don't enumerate
    every possible call count). Per-tool counts cover every kb_* tool
    name encountered in any transcript.
    """
    distribution: dict[int | str, int] = {}
    per_tool: dict[str, int] = {}
    by_qid: dict[str, dict] = {}

    for cap in captures:
        qid = cap.get("question_id", "")
        transcript = cap.get("tool_transcript") or []
        tools_used = [t.get("tool", "") for t in transcript if isinstance(t, dict)]
        count = len(tools_used)
        bucket: int | str = "4+" if count >= 4 else count
        distribution[bucket] = distribution.get(bucket, 0) + 1
        for name in tools_used:
            if name:
                per_tool[name] = per_tool.get(name, 0) + 1
        by_qid[qid] = {"tool_count": count, "tools_used": tools_used}

    return {
        "total_captures": len(captures),
        "tool_call_distribution": distribution,
        "tool_call_counts_per_tool": per_tool,
        "captures_by_tool_count": by_qid,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest scripts/test_corpora/tests/test_audit.py -v`
Expected: 6 PASS

- [ ] **Step 5: Ruff + format**

Run: `uv run ruff check scripts/test_corpora/runner/audit.py scripts/test_corpora/tests/test_audit.py`
Expected: "All checks passed!"

Run: `uv run ruff format scripts/test_corpora/runner/audit.py scripts/test_corpora/tests/test_audit.py`

- [ ] **Step 6: Commit**

```bash
git add scripts/test_corpora/runner/audit.py scripts/test_corpora/tests/test_audit.py
git commit -m "feat(audit): tool_use_stats analysis function"
```

---

## Task 2: `runner/audit.py` — `failure_correlation`

**Files:**
- Modify: `scripts/test_corpora/runner/audit.py`
- Modify: `scripts/test_corpora/tests/test_audit.py`

- [ ] **Step 1: Write the failing tests** (append to existing file)

```python
# Append to scripts/test_corpora/tests/test_audit.py

from scripts.test_corpora.runner.audit import failure_correlation


def _verdict(qid: str, correctness: int = 5, rationale: str = "ok") -> dict:
    """Build a minimum-shape verdict for tests. qid is paired in the joined dict layer."""
    return {
        "id": qid,
        "correctness": correctness,
        "groundedness": 5,
        "completeness": 5,
        "rationale": rationale,
        "source": {},
    }


def test_failure_correlation_low_correctness_low_tool_use():
    """correctness <= 2 AND tool_count <= 1 surfaces the qid."""
    caps = [
        _cap("q-fail", ["kb_search"]),  # 1 tool call
        _cap("q-iterated", ["kb_search"] * 3),  # 3 tool calls
        _cap("q-ok", ["kb_search"]),
    ]
    verdicts = [
        _verdict("q-fail", correctness=1),
        _verdict("q-iterated", correctness=1),
        _verdict("q-ok", correctness=5),
    ]
    result = failure_correlation(caps, verdicts)
    qids = {item["qid"] for item in result["low_correctness_low_tool_use"]}
    # q-fail: correctness=1, tool_count=1 -> qualifies
    # q-iterated: correctness=1 but tool_count=3 -> does NOT qualify
    # q-ok: tool_count=1 but correctness=5 -> does NOT qualify
    assert qids == {"q-fail"}


def test_failure_correlation_earliest_latest_questions_no_by_date_tool():
    """qid contains earliest/latest/oldest/newest/first/last AND no
    kb_documents_by_date call surfaces the qid."""
    caps = [
        _cap("enron-earliest-california", ["kb_search"]),
        _cap("synth-latest-contract", ["kb_documents_by_date"]),  # has the right tool
        _cap("enron-find-something", ["kb_search"]),  # name doesn't trigger
    ]
    verdicts = [
        _verdict("enron-earliest-california", correctness=0),
        _verdict("synth-latest-contract", correctness=5),
        _verdict("enron-find-something", correctness=3),
    ]
    result = failure_correlation(caps, verdicts)
    qids = {item["qid"] for item in result["earliest_latest_questions_no_by_date_tool"]}
    assert qids == {"enron-earliest-california"}


def test_failure_correlation_ambiguous_id_questions_no_verify_tool():
    """correctness <= 2 AND rationale matches an ambiguity trigger AND no
    kb_verify_identifier call surfaces the qid."""
    caps = [
        _cap("q-ambig", ["kb_search"]),
        _cap("q-verified", ["kb_verify_identifier", "kb_search"]),
        _cap("q-not-flagged", ["kb_search"]),
    ]
    verdicts = [
        _verdict("q-ambig", correctness=1, rationale="answer is ambiguous between multiple docs"),
        _verdict("q-verified", correctness=1, rationale="answer is ambiguous"),  # has verify -> not flagged
        _verdict("q-not-flagged", correctness=1, rationale="answer is wrong"),  # no ambiguity word -> not flagged
    ]
    result = failure_correlation(caps, verdicts)
    qids = {item["qid"] for item in result["ambiguous_id_questions_no_verify_tool"]}
    assert qids == {"q-ambig"}


def test_failure_correlation_empty_lists_when_no_matches():
    """Pattern keys MUST be present with [] when nothing matches."""
    caps = [_cap("q1", ["kb_search"] * 3)]
    verdicts = [_verdict("q1", correctness=5)]
    result = failure_correlation(caps, verdicts)
    assert result["low_correctness_low_tool_use"] == []
    assert result["earliest_latest_questions_no_by_date_tool"] == []
    assert result["ambiguous_id_questions_no_verify_tool"] == []


def test_failure_correlation_handles_missing_verdict():
    """Capture without a paired verdict is skipped (not crashed)."""
    caps = [_cap("q-no-verdict", ["kb_search"]), _cap("q-paired", ["kb_search"])]
    verdicts = [_verdict("q-paired", correctness=1)]
    result = failure_correlation(caps, verdicts)
    qids = {item["qid"] for item in result["low_correctness_low_tool_use"]}
    assert qids == {"q-paired"}  # the unpaired capture was silently skipped
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest scripts/test_corpora/tests/test_audit.py -v -k failure_correlation`
Expected: 5 FAIL — `ImportError: cannot import name 'failure_correlation'`

- [ ] **Step 3: Implement `failure_correlation`** (append to `audit.py`)

```python
# Append to scripts/test_corpora/runner/audit.py

# Substrings that mark a question as "earliest/latest-style" by qid.
# Tuned to match the convention used by enron + synthetic ground-truth gen.
_EARLIEST_LATEST_TRIGGERS = ("earliest", "latest", "oldest", "newest", "first", "last")

# Substrings in the judge's rationale that suggest the model hit ambiguity.
# Lossy heuristic — false positives possible, intentional for v1.
_AMBIGUITY_TRIGGERS = ("ambiguous", "multiple", "several", "which")

# Score threshold for "low correctness". 0-5 scale; <=2 means clearly wrong.
_LOW_CORRECTNESS = 2

# Tool-call threshold for "low tool use".
_LOW_TOOL_COUNT = 1


def failure_correlation(captures: list[dict], verdicts: list[dict]) -> dict:
    """Join captures + verdicts by qid; surface actionable failure patterns.

    Each pattern key is always present in the output, with [] when no
    items match — callers can blindly index without KeyError.

    Returns:
      {
        "low_correctness_low_tool_use": [{qid, tools_used, correctness, title_fragment}, ...],
        "earliest_latest_questions_no_by_date_tool": [{qid, tools_used, correctness}, ...],
        "ambiguous_id_questions_no_verify_tool": [{qid, tools_used, correctness}, ...],
      }
    """
    cap_by_qid = {c.get("question_id"): c for c in captures}
    ver_by_qid = {v.get("id"): v for v in verdicts}

    low_corr: list[dict] = []
    earliest_latest: list[dict] = []
    ambiguous: list[dict] = []

    for qid, cap in cap_by_qid.items():
        ver = ver_by_qid.get(qid)
        if ver is None:
            continue  # capture without verdict — silently skip
        transcript = cap.get("tool_transcript") or []
        tools_used = [t.get("tool", "") for t in transcript if isinstance(t, dict)]
        tool_count = len(tools_used)
        correctness = ver.get("correctness", 0)
        rationale_lc = (ver.get("rationale") or "").lower()

        if correctness <= _LOW_CORRECTNESS and tool_count <= _LOW_TOOL_COUNT:
            low_corr.append({
                "qid": qid,
                "tools_used": tools_used,
                "correctness": correctness,
                "title_fragment": (cap.get("question") or "")[:80],
            })

        qid_lc = qid.lower()
        if any(t in qid_lc for t in _EARLIEST_LATEST_TRIGGERS) and "kb_documents_by_date" not in tools_used:
            earliest_latest.append({
                "qid": qid,
                "tools_used": tools_used,
                "correctness": correctness,
            })

        if (
            correctness <= _LOW_CORRECTNESS
            and any(t in rationale_lc for t in _AMBIGUITY_TRIGGERS)
            and "kb_verify_identifier" not in tools_used
        ):
            ambiguous.append({
                "qid": qid,
                "tools_used": tools_used,
                "correctness": correctness,
            })

    return {
        "low_correctness_low_tool_use": low_corr,
        "earliest_latest_questions_no_by_date_tool": earliest_latest,
        "ambiguous_id_questions_no_verify_tool": ambiguous,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest scripts/test_corpora/tests/test_audit.py -v`
Expected: 11 PASS (6 from Task 1 + 5 from this task)

- [ ] **Step 5: Ruff + format**

Run: `uv run ruff check scripts/test_corpora/runner/audit.py scripts/test_corpora/tests/test_audit.py`

Run: `uv run ruff format scripts/test_corpora/runner/audit.py scripts/test_corpora/tests/test_audit.py`

- [ ] **Step 6: Commit**

```bash
git add scripts/test_corpora/runner/audit.py scripts/test_corpora/tests/test_audit.py
git commit -m "feat(audit): failure_correlation joins captures + verdicts"
```

---

## Task 3: `runner/audit.py` — `citation_hygiene`

**Files:**
- Modify: `scripts/test_corpora/runner/audit.py`
- Modify: `scripts/test_corpora/tests/test_audit.py`

- [ ] **Step 1: Write the failing tests** (append)

```python
# Append to scripts/test_corpora/tests/test_audit.py

from scripts.test_corpora.runner.audit import citation_hygiene


def _cap_with_transcript_uuids(qid: str, cited: list[str], transcript_uuids: list[str]) -> dict:
    """Build a capture where the tool transcript result_summary mentions the
    given UUIDs as doc_id values (so the UUID extractor will find them)."""
    import json as _json
    transcript = [{
        "tool": "kb_search",
        "args": {},
        "result_summary": _json.dumps({"hits": [{"doc_id": u, "score": 0.9} for u in transcript_uuids]}),
    }]
    return {
        "question_id": qid,
        "question": "q",
        "answer": "an answer with some prose",
        "cited_doc_ids": cited,
        "cited_doc_titles": [],
        "tool_call_count": 1,
        "tool_transcript": transcript,
        "elapsed_seconds": 0.0,
        "model": "test",
        "timestamp": "2026-05-25T00:00:00Z",
    }


# Use real UUIDs (regex-shaped); strings differ to make assertions clearer.
_U1 = "11111111-1111-1111-1111-111111111111"
_U2 = "22222222-2222-2222-2222-222222222222"
_U3 = "33333333-3333-3333-3333-333333333333"


def test_citation_hygiene_grounded_when_cited_subset_of_seen():
    caps = [_cap_with_transcript_uuids("q1", cited=[_U1], transcript_uuids=[_U1, _U2])]
    result = citation_hygiene(caps)
    assert result["grounded_count"] == 1
    assert result["total"] == 1
    assert result["fabricated_citations"] == []
    assert result["no_citations"] == []


def test_citation_hygiene_fabricated_when_cited_not_seen():
    caps = [_cap_with_transcript_uuids("q1", cited=[_U3], transcript_uuids=[_U1, _U2])]
    result = citation_hygiene(caps)
    assert result["grounded_count"] == 0
    fab = result["fabricated_citations"]
    assert len(fab) == 1
    assert fab[0]["qid"] == "q1"
    assert _U3 in fab[0]["cited"]
    assert set(fab[0]["seen_in_transcript"]) == {_U1, _U2}


def test_citation_hygiene_no_citations_bucket():
    caps = [_cap_with_transcript_uuids("q1", cited=[], transcript_uuids=[_U1])]
    result = citation_hygiene(caps)
    assert result["grounded_count"] == 0  # no cited == no grounded claim either
    assert len(result["no_citations"]) == 1
    nc = result["no_citations"][0]
    assert nc["qid"] == "q1"
    assert nc["answer_preview"].startswith("an answer")


def test_citation_hygiene_partial_fabrication_counts_as_fabricated():
    """If ANY cited doc_id isn't in the transcript, the capture goes in
    fabricated_citations — not grounded_count."""
    caps = [_cap_with_transcript_uuids("q1", cited=[_U1, _U3], transcript_uuids=[_U1, _U2])]
    result = citation_hygiene(caps)
    assert result["grounded_count"] == 0
    assert len(result["fabricated_citations"]) == 1


def test_citation_hygiene_buckets_partition_total():
    """grounded_count + len(no_citations) + len(fabricated_citations) == total
    for any set of captures (each capture lives in exactly one bucket)."""
    caps = [
        _cap_with_transcript_uuids("q-grounded", cited=[_U1], transcript_uuids=[_U1]),
        _cap_with_transcript_uuids("q-no-cite", cited=[], transcript_uuids=[_U1]),
        _cap_with_transcript_uuids("q-fab", cited=[_U3], transcript_uuids=[_U1]),
    ]
    result = citation_hygiene(caps)
    assert result["total"] == 3
    assert result["grounded_count"] + len(result["no_citations"]) + len(result["fabricated_citations"]) == 3
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest scripts/test_corpora/tests/test_audit.py -v -k citation_hygiene`
Expected: 5 FAIL — `ImportError`

- [ ] **Step 3: Implement `citation_hygiene`** (append to `audit.py`)

```python
# Append to scripts/test_corpora/runner/audit.py

import re

# UUID v4 shape: 8-4-4-4-12 hex chars. Matches what HC's tool responses use
# for doc_id. False positives possible (a query string echoed in result_summary
# could match), false negatives possible (a tool that doesn't include doc_id);
# documented as a heuristic in the spec and surfaced in the markdown report.
_UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")


def citation_hygiene(captures: list[dict]) -> dict:
    """For each capture, compare cited_doc_ids against doc_ids seen in any
    tool transcript result_summary. Each capture lands in exactly one bucket:

      - no_citations: capture has empty cited_doc_ids
      - fabricated_citations: at least one cited doc_id is NOT in any
        result_summary's UUID set
      - grounded (count): every cited doc_id is in the seen set
    """
    no_citations: list[dict] = []
    fabricated: list[dict] = []
    grounded = 0

    for cap in captures:
        cited = cap.get("cited_doc_ids") or []
        transcript = cap.get("tool_transcript") or []
        seen_uuids: set[str] = set()
        for call in transcript:
            if not isinstance(call, dict):
                continue
            summary = call.get("result_summary") or ""
            if isinstance(summary, str):
                seen_uuids.update(_UUID_RE.findall(summary))

        if not cited:
            no_citations.append({
                "qid": cap.get("question_id", ""),
                "answer_preview": (cap.get("answer") or "")[:200],
            })
            continue

        missing = [c for c in cited if c not in seen_uuids]
        if missing:
            fabricated.append({
                "qid": cap.get("question_id", ""),
                "cited": cited,
                "seen_in_transcript": sorted(seen_uuids),
            })
        else:
            grounded += 1

    return {
        "no_citations": no_citations,
        "fabricated_citations": fabricated,
        "grounded_count": grounded,
        "total": len(captures),
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest scripts/test_corpora/tests/test_audit.py -v`
Expected: 16 PASS (11 from Tasks 1+2 + 5 from this task)

- [ ] **Step 5: Ruff + format**

Run: `uv run ruff check scripts/test_corpora/runner/audit.py scripts/test_corpora/tests/test_audit.py`

Run: `uv run ruff format scripts/test_corpora/runner/audit.py scripts/test_corpora/tests/test_audit.py`

- [ ] **Step 6: Commit**

```bash
git add scripts/test_corpora/runner/audit.py scripts/test_corpora/tests/test_audit.py
git commit -m "feat(audit): citation_hygiene partitions captures by citation quality"
```

---

## Task 4: `runner/cross_judge.py` — `JudgeProvider` + `OpenAIJudgeProvider` + `rejudge_with`

**Files:**
- Create: `scripts/test_corpora/runner/cross_judge.py`
- Test:   `scripts/test_corpora/tests/test_cross_judge.py`

> **Note on the JudgeProvider abstraction:** the spec said "via the existing PR-C `Provider` interface", but on re-reading PR-C's code the `Provider` Protocol is for baseline answering (`run_question` returns a `BaselineResult` with tools, transcript, etc.) — not for one-shot judging. We introduce a narrower `JudgeProvider` Protocol with one method: `judge(prompt: str) -> str` returns the raw model response. `OpenAIJudgeProvider` is the only v1 implementation. Future judges (local model, Gemini, etc.) plug in with a 10-line adapter.

- [ ] **Step 1: Write the failing tests**

```python
# scripts/test_corpora/tests/test_cross_judge.py
"""Unit tests for scripts/test_corpora/runner/cross_judge.py.

Uses a MockJudgeProvider to avoid live LLM calls; the live OpenAI
integration is exercised manually in the deferred live-smoke task.
"""

from __future__ import annotations

import json

import pytest

from scripts.test_corpora.runner.cross_judge import JudgeProvider, rejudge_with


class _MockJudge:
    """Returns a canned verdict JSON string for every prompt. Used to drive
    rejudge_with without an OpenAI call.

    `responses` is a list of (correctness, groundedness, completeness, rationale)
    tuples consumed in order; raises if exhausted.
    """

    def __init__(self, responses: list[tuple] | None = None):
        self._responses = responses or [(4, 4, 4, "mock")]
        self._call_count = 0
        self.calls: list[str] = []

    def judge(self, prompt: str) -> str:
        self.calls.append(prompt)
        idx = min(self._call_count, len(self._responses) - 1)
        c, g, comp, rat = self._responses[idx]
        self._call_count += 1
        return json.dumps({
            "correctness": c, "groundedness": g, "completeness": comp, "rationale": rat,
        })


def _cap(qid: str, qtype: str = "lookup") -> dict:
    """Minimum capture shape rejudge_with needs."""
    return {
        "question_id": qid,
        "question": "q?",
        "answer": "an answer",
        "cited_doc_ids": ["abc"],
        "cited_doc_titles": ["A title"],
        "tool_call_count": 1,
        "tool_transcript": [{"tool": "kb_search", "args": {}, "result_summary": "{}"}],
        "elapsed_seconds": 0.0,
        "model": "baseline-model",
        "timestamp": "2026-05-25T00:00:00Z",
        "_qtype": qtype,  # rejudge_with reads this to pick the prompt template
    }


def test_mock_judge_satisfies_protocol():
    """The MockJudge structurally matches the Protocol — runtime check."""
    assert isinstance(_MockJudge(), JudgeProvider)


def test_rejudge_with_returns_verdict_per_capture():
    caps = [_cap("q1"), _cap("q2")]
    judge = _MockJudge(responses=[(5, 5, 5, "ok"), (3, 2, 4, "partial")])
    results = rejudge_with(caps, judge, judge_model="mock-model")
    assert len(results) == 2
    assert results[0]["qid"] == "q1"
    assert results[0]["correctness"] == 5
    assert results[0]["judge_model"] == "mock-model"
    assert results[1]["qid"] == "q2"
    assert results[1]["completeness"] == 4


def test_rejudge_with_items_cap_limits_sample_size():
    caps = [_cap(f"q{i}") for i in range(10)]
    judge = _MockJudge()
    results = rejudge_with(caps, judge, judge_model="mock-model", items=3)
    assert len(results) == 3


def test_rejudge_with_items_sample_is_deterministic():
    """Same captures + same seed should give the same selection."""
    caps = [_cap(f"q{i}") for i in range(10)]
    judge_a = _MockJudge()
    judge_b = _MockJudge()
    results_a = rejudge_with(caps, judge_a, judge_model="mock", items=3, seed=42)
    results_b = rejudge_with(caps, judge_b, judge_model="mock", items=3, seed=42)
    assert [r["qid"] for r in results_a] == [r["qid"] for r in results_b]


def test_rejudge_with_provider_error_records_judge_error():
    """A provider exception on one item must not abort the whole run."""
    class _RaisingJudge:
        def __init__(self):
            self.calls = 0
        def judge(self, prompt: str) -> str:
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("simulated rate limit")
            return json.dumps({"correctness": 5, "groundedness": 5, "completeness": 5, "rationale": "ok"})

    caps = [_cap(f"q{i}") for i in range(3)]
    results = rejudge_with(caps, _RaisingJudge(), judge_model="mock")
    assert len(results) == 3
    # Item 1 succeeded.
    assert "judge_error" not in results[0]
    # Item 2 errored.
    assert "judge_error" in results[1]
    assert "simulated rate limit" in results[1]["judge_error"]
    # Item 3 succeeded after the error (no early abort).
    assert "judge_error" not in results[2]


def test_rejudge_with_find_qtype_uses_find_prompt():
    """When the capture's qtype is 'find', the prompt must come from
    _PROMPT_FIND, not _PROMPT — verifiable by inspecting the prompt the
    MockJudge received."""
    caps = [_cap("q-find", qtype="find")]
    judge = _MockJudge()
    rejudge_with(caps, judge, judge_model="mock", answer_keys={"q-find": {"count": 5, "all": [], "sample": ["a", "b"]}})
    # _PROMPT_FIND has the marker "QUESTION TYPE: find"
    assert "QUESTION TYPE: find" in judge.calls[0]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest scripts/test_corpora/tests/test_cross_judge.py -v`
Expected: `ModuleNotFoundError: No module named 'scripts.test_corpora.runner.cross_judge'`

- [ ] **Step 3: Implement `cross_judge.py` (JudgeProvider + OpenAIJudgeProvider + rejudge_with)**

```python
# scripts/test_corpora/runner/cross_judge.py
"""Cross-judge sensitivity: re-judge captures with a second judge model.

Two halves:
  - JudgeProvider Protocol + OpenAIJudgeProvider + rejudge_with(): the
    re-judge driver. JudgeProvider has one method, judge(prompt) -> str,
    so future judges (local model, Gemini, etc.) plug in with a 10-line
    adapter.
  - compare_judges(): pure stats over two verdict lists. Added in Task 5.

Reuses _PROMPT, _PROMPT_FIND, _extract_json, _score from
scripts.test_corpora.runner.answer_judge so the cross-judge rubric tracks
any future change to the Sonnet judge's prompt text.
"""

from __future__ import annotations

import logging
import random
from typing import Any, Protocol, runtime_checkable

from scripts.test_corpora.runner.answer_judge import (
    _PROMPT,
    _PROMPT_FIND,
    _extract_json,
    _score,
)

log = logging.getLogger("cross_judge")


@runtime_checkable
class JudgeProvider(Protocol):
    """One-shot judge: takes a prompt, returns the raw model response text."""

    def judge(self, prompt: str) -> str: ...


class OpenAIJudgeProvider:
    """OpenAI-backed judge using gpt-* chat completions."""

    def __init__(self, *, model: str = "gpt-4o", client: Any | None = None):
        import openai
        self._client = client or openai.OpenAI()
        self._model = model

    def judge(self, prompt: str) -> str:
        resp = self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": prompt}],
            max_completion_tokens=600,
        )
        return resp.choices[0].message.content or ""


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


def rejudge_with(
    captures: list[dict],
    judge_provider: JudgeProvider,
    *,
    judge_model: str,
    items: int | None = None,
    seed: int = 42,
    answer_keys: dict[str, Any] | None = None,
    qtypes: dict[str, str] | None = None,
) -> list[dict]:
    """Re-score `captures` (optionally a random sample of `items`) using `judge_provider`.

    Returns a list of verdict dicts, one per captured item attempted:
      {"qid", "correctness", "groundedness", "completeness", "rationale",
       "judge_model", and optionally "judge_error": "..." on provider failure}

    answer_keys: optional dict mapping qid -> ground-truth answer key.
      When absent, the per-capture key falls back to capture["_answer_key"]
      (set by the CLI when loading ground-truth), else None.
    qtypes: optional dict mapping qid -> qtype ("lookup" | "find" | "negative"
      | etc.). Falls back to capture["_qtype"], else "lookup".

    A provider exception on one item is logged + recorded as judge_error
    on that item; remaining items continue.
    """
    answer_keys = answer_keys or {}
    qtypes = qtypes or {}

    if items is not None and items < len(captures):
        rng = random.Random(seed)
        captures = rng.sample(captures, items)

    results: list[dict] = []
    for cap in captures:
        qid = cap.get("question_id", "")
        qtype = qtypes.get(qid) or cap.get("_qtype") or "lookup"
        ak = answer_keys.get(qid) if qid in answer_keys else cap.get("_answer_key")
        prompt = _build_prompt(cap, qtype=qtype, answer_key=ak)
        try:
            raw = judge_provider.judge(prompt)
            data = _extract_json(raw)
            results.append({
                "qid": qid,
                "correctness": _score(data, "correctness"),
                "groundedness": _score(data, "groundedness"),
                "completeness": _score(data, "completeness"),
                "rationale": str(data.get("rationale", "")),
                "judge_model": judge_model,
            })
        except Exception as exc:
            log.warning("rejudge failed for %s: %s", qid, exc)
            results.append({
                "qid": qid,
                "judge_model": judge_model,
                "judge_error": str(exc),
            })
    return results
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest scripts/test_corpora/tests/test_cross_judge.py -v`
Expected: 6 PASS

- [ ] **Step 5: Ruff + format**

Run: `uv run ruff check scripts/test_corpora/runner/cross_judge.py scripts/test_corpora/tests/test_cross_judge.py`

Run: `uv run ruff format scripts/test_corpora/runner/cross_judge.py scripts/test_corpora/tests/test_cross_judge.py`

- [ ] **Step 6: Commit**

```bash
git add scripts/test_corpora/runner/cross_judge.py scripts/test_corpora/tests/test_cross_judge.py
git commit -m "feat(cross-judge): JudgeProvider Protocol + OpenAIJudgeProvider + rejudge_with"
```

---

## Task 5: `runner/cross_judge.py` — `compare_judges` (pure stats)

**Files:**
- Modify: `scripts/test_corpora/runner/cross_judge.py`
- Modify: `scripts/test_corpora/tests/test_cross_judge.py`

- [ ] **Step 1: Write the failing tests** (append)

```python
# Append to scripts/test_corpora/tests/test_cross_judge.py

import math

from scripts.test_corpora.runner.cross_judge import (
    _cohens_kappa,
    _spearman,
    compare_judges,
)


def _ver(qid: str, c: int, g: int, comp: int, rat: str = "") -> dict:
    return {
        "qid": qid,
        "correctness": c,
        "groundedness": g,
        "completeness": comp,
        "rationale": rat,
    }


def test_spearman_identical_arrays_is_one():
    assert _spearman([1, 2, 3, 4, 5], [1, 2, 3, 4, 5]) == pytest.approx(1.0)


def test_spearman_reversed_arrays_is_minus_one():
    assert _spearman([1, 2, 3, 4, 5], [5, 4, 3, 2, 1]) == pytest.approx(-1.0)


def test_spearman_with_ties_uses_rank_average():
    """Hand-computed: [1, 2, 2, 3] vs [10, 20, 20, 30] -> ranks identical -> 1.0"""
    assert _spearman([1, 2, 2, 3], [10, 20, 20, 30]) == pytest.approx(1.0)


def test_cohens_kappa_perfect_agreement_is_one():
    assert _cohens_kappa([0, 1, 2, 3, 4, 5], [0, 1, 2, 3, 4, 5]) == pytest.approx(1.0)


def test_cohens_kappa_random_assignment_near_zero():
    """All A=0, B distributed across [0..5]. Observed agreement equals
    chance agreement, so kappa ~= 0."""
    a = [0] * 6
    b = [0, 1, 2, 3, 4, 5]
    # observed agreement = 1/6; chance agreement also ~1/6 (B is uniform).
    # kappa ~= 0 (allow tolerance).
    assert abs(_cohens_kappa(a, b)) < 0.3


def test_compare_judges_empty_inputs_shape():
    result = compare_judges([], [])
    assert result == {
        "n": 0,
        "judges": [None, None],
        "deltas": {},
        "spearman": {},
        "kappa": {},
        "disagreements": [],
    }


def test_compare_judges_identical_verdicts_zero_delta():
    a = [_ver("q1", 5, 5, 5), _ver("q2", 3, 4, 5)]
    b = [_ver("q1", 5, 5, 5), _ver("q2", 3, 4, 5)]
    result = compare_judges(a, b, judges=("sonnet", "gpt-4o"))
    assert result["n"] == 2
    assert result["judges"] == ["sonnet", "gpt-4o"]
    for dim in ("correctness", "groundedness", "completeness"):
        assert result["deltas"][dim]["mean"] == 0.0
        assert result["spearman"][dim] == pytest.approx(1.0)
        assert result["kappa"][dim] == pytest.approx(1.0)
    assert result["disagreements"] == []


def test_compare_judges_disagreement_surfaces_on_delta_two():
    a = [_ver("q1", 4, 5, 4, rat="a-rat"), _ver("q2", 3, 3, 3)]
    b = [_ver("q1", 2, 5, 4, rat="b-rat"), _ver("q2", 3, 3, 3)]
    result = compare_judges(a, b, judges=("sonnet", "gpt-4o"))
    assert len(result["disagreements"]) == 1
    dis = result["disagreements"][0]
    assert dis["qid"] == "q1"
    assert dis["delta_dim"] == "correctness"
    assert dis["max_delta"] == 2
    assert dis["judge_a"]["correctness"] == 4
    assert dis["judge_b"]["correctness"] == 2
    assert dis["judge_a"]["rationale"] == "a-rat"
    assert dis["judge_b"]["rationale"] == "b-rat"


def test_compare_judges_mismatched_qids_intersection_only():
    """If one judge has verdicts the other doesn't, only the intersection counts."""
    a = [_ver("q1", 5, 5, 5), _ver("q2", 3, 3, 3)]
    b = [_ver("q1", 5, 5, 5), _ver("q3", 4, 4, 4)]
    result = compare_judges(a, b)
    assert result["n"] == 1  # only q1 intersected


def test_compare_judges_skips_judge_error_items():
    """Items with judge_error in either judge are skipped."""
    a = [_ver("q1", 5, 5, 5), _ver("q2", 4, 4, 4)]
    b = [_ver("q1", 5, 5, 5), {"qid": "q2", "judge_error": "rate limit"}]
    result = compare_judges(a, b)
    assert result["n"] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest scripts/test_corpora/tests/test_cross_judge.py -v -k "compare_judges or _spearman or _cohens_kappa"`
Expected: 10 FAIL — `ImportError`

- [ ] **Step 3: Implement `compare_judges` + stats helpers** (append to `cross_judge.py`)

```python
# Append to scripts/test_corpora/runner/cross_judge.py

import math

_DIMENSIONS = ("correctness", "groundedness", "completeness")
_DELTA_DISAGREEMENT_THRESHOLD = 2


def _ranks(xs: list[float]) -> list[float]:
    """Average rank (1-based). Ties get the mean of their ranks."""
    indexed = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(indexed):
        j = i
        while j + 1 < len(indexed) and xs[indexed[j + 1]] == xs[indexed[i]]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[indexed[k]] = avg_rank
        i = j + 1
    return ranks


def _spearman(xs: list[float], ys: list[float]) -> float:
    """Spearman rank correlation. Returns 0.0 on n < 2 or zero-variance input."""
    if len(xs) != len(ys) or len(xs) < 2:
        return 0.0
    rx, ry = _ranks(xs), _ranks(ys)
    mean_x = sum(rx) / len(rx)
    mean_y = sum(ry) / len(ry)
    num = sum((a - mean_x) * (b - mean_y) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mean_x) ** 2 for a in rx) * sum((b - mean_y) ** 2 for b in ry))
    return num / den if den else 0.0


def _cohens_kappa(xs: list[int], ys: list[int]) -> float:
    """Cohen's kappa on integer 0-5 scores. Returns 0.0 on n == 0 or
    when both raters used a single bucket (no chance to disagree)."""
    if not xs:
        return 0.0
    n = len(xs)
    observed = sum(1 for a, b in zip(xs, ys) if a == b) / n
    # Marginal proportions per category.
    cats = set(xs) | set(ys)
    px = {c: xs.count(c) / n for c in cats}
    py = {c: ys.count(c) / n for c in cats}
    expected = sum(px[c] * py[c] for c in cats)
    if expected >= 1.0:
        return 0.0  # both raters constant → no chance to disagree → 0
    return (observed - expected) / (1.0 - expected)


def compare_judges(
    verdicts_a: list[dict],
    verdicts_b: list[dict],
    *,
    judges: tuple[str | None, str | None] = (None, None),
) -> dict:
    """Pure-stats comparison of two judges' verdicts.

    Joins by qid; skips items with judge_error on either side. Returns:
      {n, judges, deltas{dim: {mean, std, min, max}}, spearman{dim: r},
       kappa{dim: k}, disagreements: [...]}

    Delta sign: b - a (judge_b minus judge_a). Documented in the markdown.
    """
    a_by_qid = {v.get("qid"): v for v in verdicts_a if "judge_error" not in v}
    b_by_qid = {v.get("qid"): v for v in verdicts_b if "judge_error" not in v}
    common = sorted(set(a_by_qid) & set(b_by_qid))

    if not common:
        return {
            "n": 0,
            "judges": list(judges),
            "deltas": {},
            "spearman": {},
            "kappa": {},
            "disagreements": [],
        }

    deltas: dict[str, dict] = {}
    spearman: dict[str, float] = {}
    kappa: dict[str, float] = {}

    for dim in _DIMENSIONS:
        a_scores = [int(a_by_qid[q].get(dim, 0)) for q in common]
        b_scores = [int(b_by_qid[q].get(dim, 0)) for q in common]
        diffs = [b - a for a, b in zip(a_scores, b_scores)]
        mean = sum(diffs) / len(diffs)
        var = sum((d - mean) ** 2 for d in diffs) / len(diffs)
        deltas[dim] = {
            "mean": round(mean, 4),
            "std": round(math.sqrt(var), 4),
            "min": min(diffs),
            "max": max(diffs),
        }
        spearman[dim] = round(_spearman(a_scores, b_scores), 4)
        kappa[dim] = round(_cohens_kappa(a_scores, b_scores), 4)

    disagreements = []
    for qid in common:
        va, vb = a_by_qid[qid], b_by_qid[qid]
        per_dim_deltas = {
            dim: abs(int(vb.get(dim, 0)) - int(va.get(dim, 0)))
            for dim in _DIMENSIONS
        }
        worst_dim, worst_delta = max(per_dim_deltas.items(), key=lambda kv: kv[1])
        if worst_delta >= _DELTA_DISAGREEMENT_THRESHOLD:
            disagreements.append({
                "qid": qid,
                "judge_a": {dim: va.get(dim) for dim in (*_DIMENSIONS, "rationale")},
                "judge_b": {dim: vb.get(dim) for dim in (*_DIMENSIONS, "rationale")},
                "max_delta": worst_delta,
                "delta_dim": worst_dim,
            })

    return {
        "n": len(common),
        "judges": list(judges),
        "deltas": deltas,
        "spearman": spearman,
        "kappa": kappa,
        "disagreements": disagreements,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest scripts/test_corpora/tests/test_cross_judge.py -v`
Expected: 16 PASS (6 from Task 4 + 10 from this task)

- [ ] **Step 5: Ruff + format**

Run: `uv run ruff check scripts/test_corpora/runner/cross_judge.py scripts/test_corpora/tests/test_cross_judge.py`

Run: `uv run ruff format scripts/test_corpora/runner/cross_judge.py scripts/test_corpora/tests/test_cross_judge.py`

- [ ] **Step 6: Commit**

```bash
git add scripts/test_corpora/runner/cross_judge.py scripts/test_corpora/tests/test_cross_judge.py
git commit -m "feat(cross-judge): compare_judges pure stats (Spearman + kappa hand-rolled)"
```

---

## Task 6: `audit_answer_eval.py` — CLI orchestrator (loading + JSON output)

**Files:**
- Create: `scripts/test_corpora/audit_answer_eval.py`
- Create: `scripts/test_corpora/tests/test_audit_cli.py`

- [ ] **Step 1: Write the failing tests**

```python
# scripts/test_corpora/tests/test_audit_cli.py
"""CLI tests for scripts/test_corpora/audit_answer_eval.py.

Loading + JSON output is testable without LLM calls; cross-judge is
exercised via a MockJudgeProvider in test_cross_judge.py.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.test_corpora.audit_answer_eval import (
    discover_corpus_and_model,
    load_captures,
    load_verdicts,
    main,
)


def _write_capture(captures_dir: Path, qid: str, tools: list[str]) -> None:
    captures_dir.mkdir(parents=True, exist_ok=True)
    (captures_dir / f"{qid}.json").write_text(json.dumps({
        "question_id": qid,
        "question": "q",
        "answer": "a",
        "cited_doc_ids": [],
        "cited_doc_titles": [],
        "tool_call_count": len(tools),
        "tool_transcript": [{"tool": t, "args": {}, "result_summary": "{}"} for t in tools],
        "elapsed_seconds": 0.0,
        "model": "test-model",
        "timestamp": "2026-05-25T00:00:00Z",
    }))


def _write_verdict(verdicts_dir: Path, qid: str, correctness: int = 5) -> None:
    verdicts_dir.mkdir(parents=True, exist_ok=True)
    (verdicts_dir / f"{qid}.json").write_text(json.dumps({
        "correctness": correctness,
        "groundedness": 5,
        "completeness": 5,
        "rationale": "ok",
        "source": {},
    }))


def _write_detail(reports_dir: Path, label: str, items: list[dict]) -> None:
    d = reports_dir / label
    d.mkdir(parents=True, exist_ok=True)
    (d / "detail.json").write_text(json.dumps(items))


def test_load_captures_returns_parsed_list(tmp_path):
    cap_dir = tmp_path / "answer-eval" / "captures" / "synthetic" / "claude-sonnet-4-6"
    _write_capture(cap_dir, "q1", ["kb_search"])
    _write_capture(cap_dir, "q2", ["kb_search", "kb_get_document"])
    caps = load_captures(tmp_path, corpus="synthetic", model="claude-sonnet-4-6")
    qids = {c["question_id"] for c in caps}
    assert qids == {"q1", "q2"}


def test_load_verdicts_prefers_detail_json_when_present(tmp_path):
    detail_items = [
        {"id": "q1", "type": "lookup", "correctness": 3, "groundedness": 4, "completeness": 5, "rationale": "x", "source": {}},
    ]
    _write_detail(tmp_path / "answer-eval" / "reports", "synthetic-phase2b", detail_items)
    verdicts = load_verdicts(tmp_path, label="synthetic-phase2b", corpus="synthetic", model="claude-sonnet-4-6")
    assert len(verdicts) == 1
    assert verdicts[0]["id"] == "q1"
    assert verdicts[0]["correctness"] == 3


def test_load_verdicts_falls_back_to_per_item_files(tmp_path):
    """When detail.json is missing, load per-item verdict files and synthesize the id."""
    v_dir = tmp_path / "answer-eval" / "verdicts" / "synthetic" / "claude-sonnet-4-6"
    _write_verdict(v_dir, "q1", correctness=4)
    _write_verdict(v_dir, "q2", correctness=2)
    verdicts = load_verdicts(tmp_path, label="missing", corpus="synthetic", model="claude-sonnet-4-6")
    ids = {v["id"] for v in verdicts}
    assert ids == {"q1", "q2"}
    by_id = {v["id"]: v for v in verdicts}
    assert by_id["q1"]["correctness"] == 4


def test_discover_corpus_and_model_auto_resolves_single_choice(tmp_path):
    cap_dir = tmp_path / "answer-eval" / "captures" / "synthetic" / "claude-sonnet-4-6"
    _write_capture(cap_dir, "q1", ["kb_search"])
    corpus, model = discover_corpus_and_model(tmp_path, corpus=None, model=None)
    assert corpus == "synthetic"
    assert model == "claude-sonnet-4-6"


def test_discover_corpus_and_model_requires_choice_when_multiple(tmp_path):
    _write_capture(tmp_path / "answer-eval" / "captures" / "synthetic" / "sonnet", "q1", [])
    _write_capture(tmp_path / "answer-eval" / "captures" / "cuad" / "sonnet", "q1", [])
    with pytest.raises(SystemExit):
        discover_corpus_and_model(tmp_path, corpus=None, model=None)


def test_main_writes_audit_json_with_static_sections(tmp_path, capsys):
    cap_dir = tmp_path / "answer-eval" / "captures" / "synthetic" / "claude-sonnet-4-6"
    _write_capture(cap_dir, "q-good", ["kb_search"] * 3)
    _write_capture(cap_dir, "q-bad", ["kb_search"])  # low tool use
    v_dir = tmp_path / "answer-eval" / "verdicts" / "synthetic" / "claude-sonnet-4-6"
    _write_verdict(v_dir, "q-good", correctness=5)
    _write_verdict(v_dir, "q-bad", correctness=1)

    rc = main([
        "--workdir", str(tmp_path),
        "--label", "smoke",
    ])
    assert rc == 0
    audit_path = tmp_path / "answer-eval" / "reports" / "smoke" / "audit.json"
    assert audit_path.exists()
    audit = json.loads(audit_path.read_text())
    assert audit["label"] == "smoke"
    assert audit["corpus"] == "synthetic"
    assert audit["baseline_model"] == "claude-sonnet-4-6"
    assert audit["tool_use"]["total_captures"] == 2
    assert {item["qid"] for item in audit["failure_correlation"]["low_correctness_low_tool_use"]} == {"q-bad"}
    assert audit["citation_hygiene"]["total"] == 2
    assert audit["cross_judge"] is None  # no --cross-judge flag


def test_main_returns_1_when_no_captures(tmp_path):
    # No captures dir at all
    rc = main(["--workdir", str(tmp_path), "--label", "missing"])
    assert rc == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest scripts/test_corpora/tests/test_audit_cli.py -v`
Expected: `ModuleNotFoundError: No module named 'scripts.test_corpora.audit_answer_eval'`

- [ ] **Step 3: Implement the CLI loader + JSON writer**

```python
# scripts/test_corpora/audit_answer_eval.py
"""harness audit + optional cross-judge re-score over an answer-eval run.

Reads existing captures + verdicts (from prior sweep run) and writes:
  <workdir>/answer-eval/reports/<label>/audit.json
  <workdir>/answer-eval/reports/<label>/audit.md  (Task 7)

USAGE
  audit_answer_eval.py --label <run-label> [options]

OPTIONS
  --workdir DIR                  Default: $HARBOR_CLERK_WORKDIR
                                 or ~/Library/Application Support/Harbor Clerk
  --label LABEL                  Required. Maps to reports/<label>/.
  --corpus CORPUS                synthetic | cuad | enron. Auto-resolved if only one.
  --baseline-model MODEL         Auto-resolved if only one model dir under <corpus>.
  --cross-judge MODEL            e.g. "gpt-4o". Triggers re-judge with that judge.
  --rejudge-sample N             Re-judge only N captures (random, seeded).
  --skip-static                  Skip the static audit (cross-judge only).
  --output-dir DIR               Override default report dir location.

EXIT CODES
  0 success; 1 usage/missing input; 2 partial failure (e.g. some judge_errors).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
from pathlib import Path

from scripts.test_corpora.runner.audit import (
    citation_hygiene,
    failure_correlation,
    tool_use_stats,
)

log = logging.getLogger("audit_answer_eval")


def default_workdir() -> Path:
    env = os.environ.get("HARBOR_CLERK_WORKDIR")
    if env:
        return Path(env)
    return Path.home() / "Library" / "Application Support" / "Harbor Clerk"


def discover_corpus_and_model(
    workdir: Path, *, corpus: str | None, model: str | None
) -> tuple[str, str]:
    """Auto-resolve corpus and baseline-model when only one choice exists;
    otherwise exit(1) with a helpful listing."""
    captures_root = workdir / "answer-eval" / "captures"
    if not captures_root.exists():
        sys.stderr.write(f"audit: no captures dir at {captures_root}\n")
        raise SystemExit(1)

    if corpus is None:
        corpora = sorted([p.name for p in captures_root.iterdir() if p.is_dir()])
        if not corpora:
            sys.stderr.write(f"audit: no corpora found under {captures_root}\n")
            raise SystemExit(1)
        if len(corpora) > 1:
            sys.stderr.write(f"audit: multiple corpora found ({corpora}); pass --corpus\n")
            raise SystemExit(1)
        corpus = corpora[0]

    corpus_dir = captures_root / corpus
    if model is None:
        models = sorted([p.name for p in corpus_dir.iterdir() if p.is_dir()])
        if not models:
            sys.stderr.write(f"audit: no model dirs found under {corpus_dir}\n")
            raise SystemExit(1)
        if len(models) > 1:
            sys.stderr.write(f"audit: multiple model dirs found ({models}); pass --baseline-model\n")
            raise SystemExit(1)
        model = models[0]

    return corpus, model


def load_captures(workdir: Path, *, corpus: str, model: str) -> list[dict]:
    cap_dir = workdir / "answer-eval" / "captures" / corpus / model
    if not cap_dir.exists():
        return []
    out: list[dict] = []
    for path in sorted(cap_dir.glob("*.json")):
        try:
            out.append(json.loads(path.read_text()))
        except json.JSONDecodeError as exc:
            log.warning("skipping unparseable capture %s: %s", path.name, exc)
    return out


def load_verdicts(
    workdir: Path, *, label: str, corpus: str, model: str
) -> list[dict]:
    """Prefer detail.json (already has id field); fall back to per-item files
    (synthesize id from filename stem)."""
    detail_path = workdir / "answer-eval" / "reports" / label / "detail.json"
    if detail_path.exists():
        try:
            return json.loads(detail_path.read_text())
        except json.JSONDecodeError as exc:
            log.warning("detail.json unparseable: %s; falling back to per-item files", exc)

    v_dir = workdir / "answer-eval" / "verdicts" / corpus / model
    if not v_dir.exists():
        return []
    out: list[dict] = []
    for path in sorted(v_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text())
            data["id"] = path.stem  # synthesize qid from filename
            out.append(data)
        except json.JSONDecodeError as exc:
            log.warning("skipping unparseable verdict %s: %s", path.name, exc)
    return out


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="audit_answer_eval.py")
    p.add_argument("--workdir", type=Path, default=None)
    p.add_argument("--label", required=True)
    p.add_argument("--corpus", default=None)
    p.add_argument("--baseline-model", default=None)
    p.add_argument("--cross-judge", default=None, help="Re-judge with this judge model (e.g. gpt-4o)")
    p.add_argument("--rejudge-sample", type=int, default=None)
    p.add_argument("--skip-static", action="store_true")
    p.add_argument("--output-dir", type=Path, default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = _build_parser().parse_args(argv)
    workdir = args.workdir or default_workdir()

    try:
        corpus, model = discover_corpus_and_model(
            workdir, corpus=args.corpus, model=args.baseline_model
        )
    except SystemExit as exc:
        return int(exc.code or 1)

    captures = load_captures(workdir, corpus=corpus, model=model)
    if not captures:
        sys.stderr.write(f"audit: no captures found at {workdir}/answer-eval/captures/{corpus}/{model}\n")
        return 1

    verdicts = load_verdicts(workdir, label=args.label, corpus=corpus, model=model)

    audit: dict = {
        "label": args.label,
        "corpus": corpus,
        "baseline_model": model,
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        "tool_use": None,
        "failure_correlation": None,
        "citation_hygiene": None,
        "cross_judge": None,
    }
    if not args.skip_static:
        audit["tool_use"] = tool_use_stats(captures)
        audit["failure_correlation"] = failure_correlation(captures, verdicts)
        audit["citation_hygiene"] = citation_hygiene(captures)

    # Cross-judge wired in Task 8.

    output_dir = args.output_dir or (workdir / "answer-eval" / "reports" / args.label)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "audit.json").write_text(json.dumps(audit, indent=2, default=str))
    log.info("wrote %s", output_dir / "audit.json")
    # audit.md wired in Task 7.
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest scripts/test_corpora/tests/test_audit_cli.py -v`
Expected: 7 PASS

- [ ] **Step 5: Ruff + format**

Run: `uv run ruff check scripts/test_corpora/audit_answer_eval.py scripts/test_corpora/tests/test_audit_cli.py`

Run: `uv run ruff format scripts/test_corpora/audit_answer_eval.py scripts/test_corpora/tests/test_audit_cli.py`

- [ ] **Step 6: Commit**

```bash
git add scripts/test_corpora/audit_answer_eval.py scripts/test_corpora/tests/test_audit_cli.py
git commit -m "feat(audit-cli): orchestrator with load + JSON output (static audit)"
```

---

## Task 7: `audit_answer_eval.py` — markdown report rendering

**Files:**
- Modify: `scripts/test_corpora/audit_answer_eval.py`
- Modify: `scripts/test_corpora/tests/test_audit_cli.py`

- [ ] **Step 1: Write the failing tests** (append)

```python
# Append to scripts/test_corpora/tests/test_audit_cli.py

from scripts.test_corpora.audit_answer_eval import render_markdown


def test_render_markdown_has_top_level_header_and_sections():
    audit = {
        "label": "smoke",
        "corpus": "synthetic",
        "baseline_model": "claude-sonnet-4-6",
        "generated_at": "2026-05-25T00:00:00+00:00",
        "tool_use": {
            "total_captures": 5,
            "tool_call_distribution": {1: 2, 2: 3},
            "tool_call_counts_per_tool": {"kb_search": 10, "kb_get_document": 3},
            "captures_by_tool_count": {},
        },
        "failure_correlation": {
            "low_correctness_low_tool_use": [{"qid": "q-fail", "tools_used": ["kb_search"], "correctness": 1, "title_fragment": "what does X say?"}],
            "earliest_latest_questions_no_by_date_tool": [],
            "ambiguous_id_questions_no_verify_tool": [],
        },
        "citation_hygiene": {
            "grounded_count": 4, "total": 5,
            "no_citations": [{"qid": "q-no-cite", "answer_preview": "no citations here"}],
            "fabricated_citations": [],
        },
        "cross_judge": None,
    }
    md = render_markdown(audit)
    assert md.startswith("# Audit")
    assert "synthetic" in md
    assert "claude-sonnet-4-6" in md
    assert "## Tool-use distribution" in md
    assert "## Failure correlation" in md
    assert "## Citation hygiene" in md
    assert "q-fail" in md
    assert "q-no-cite" in md


def test_render_markdown_static_only_omits_cross_judge_section():
    audit = {
        "label": "smoke", "corpus": "synthetic", "baseline_model": "m",
        "generated_at": "...",
        "tool_use": {"total_captures": 0, "tool_call_distribution": {}, "tool_call_counts_per_tool": {}, "captures_by_tool_count": {}},
        "failure_correlation": {"low_correctness_low_tool_use": [], "earliest_latest_questions_no_by_date_tool": [], "ambiguous_id_questions_no_verify_tool": []},
        "citation_hygiene": {"grounded_count": 0, "total": 0, "no_citations": [], "fabricated_citations": []},
        "cross_judge": None,
    }
    md = render_markdown(audit)
    assert "Cross-judge" not in md


def test_render_markdown_cross_judge_section_shows_deltas_and_disagreements():
    audit = {
        "label": "smoke", "corpus": "synthetic", "baseline_model": "claude-sonnet-4-6",
        "generated_at": "...",
        "tool_use": {"total_captures": 0, "tool_call_distribution": {}, "tool_call_counts_per_tool": {}, "captures_by_tool_count": {}},
        "failure_correlation": {"low_correctness_low_tool_use": [], "earliest_latest_questions_no_by_date_tool": [], "ambiguous_id_questions_no_verify_tool": []},
        "citation_hygiene": {"grounded_count": 0, "total": 0, "no_citations": [], "fabricated_citations": []},
        "cross_judge": {
            "n": 30,
            "judges": ["claude-sonnet-4-6", "gpt-4o"],
            "deltas": {
                "correctness": {"mean": -0.13, "std": 0.84, "min": -2, "max": 2},
                "groundedness": {"mean": 0.27, "std": 0.61, "min": -1, "max": 3},
                "completeness": {"mean": -0.07, "std": 0.92, "min": -3, "max": 2},
            },
            "spearman": {"correctness": 0.78, "groundedness": 0.81, "completeness": 0.73},
            "kappa": {"correctness": 0.42, "groundedness": 0.51, "completeness": 0.38},
            "disagreements": [
                {"qid": "q-disagree", "judge_a": {"correctness": 4, "groundedness": 5, "completeness": 3, "rationale": "rat-a"}, "judge_b": {"correctness": 2, "groundedness": 5, "completeness": 2, "rationale": "rat-b"}, "max_delta": 2, "delta_dim": "correctness"},
            ],
        },
    }
    md = render_markdown(audit)
    assert "## Cross-judge" in md
    assert "gpt-4o" in md
    assert "0.78" in md  # spearman value rendered
    assert "q-disagree" in md
    assert "rat-a" in md
    assert "rat-b" in md
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest scripts/test_corpora/tests/test_audit_cli.py -v -k render_markdown`
Expected: 3 FAIL — `ImportError`

- [ ] **Step 3: Implement `render_markdown` + wire into `main`** (append to `audit_answer_eval.py`)

```python
# Append to scripts/test_corpora/audit_answer_eval.py

def render_markdown(audit: dict) -> str:
    """Render an audit dict as a readable Markdown report."""
    out: list[str] = []
    out.append(f"# Audit — {audit['label']} ({audit['baseline_model']})")
    out.append("")
    out.append(f"Generated {audit['generated_at']}")
    out.append(f"Corpus: `{audit['corpus']}`")
    out.append("")

    tu = audit.get("tool_use")
    if tu:
        out.append("## Tool-use distribution")
        out.append("")
        out.append(f"Total captures: {tu['total_captures']}")
        out.append("")
        out.append("| tool calls | count |")
        out.append("| --- | --- |")
        for bucket, count in sorted(tu["tool_call_distribution"].items(), key=lambda kv: str(kv[0])):
            out.append(f"| {bucket} | {count} |")
        out.append("")
        out.append("| tool | calls |")
        out.append("| --- | --- |")
        for tool, count in sorted(tu["tool_call_counts_per_tool"].items(), key=lambda kv: -kv[1]):
            out.append(f"| {tool} | {count} |")
        out.append("")

    fc = audit.get("failure_correlation")
    if fc:
        out.append("## Failure correlation")
        out.append("")
        _render_fc_block(out, "Low correctness + low tool use", fc["low_correctness_low_tool_use"])
        _render_fc_block(out, "Earliest/latest questions, no kb_documents_by_date", fc["earliest_latest_questions_no_by_date_tool"])
        _render_fc_block(out, "Possibly-ambiguous questions, no kb_verify_identifier", fc["ambiguous_id_questions_no_verify_tool"])

    ch = audit.get("citation_hygiene")
    if ch:
        out.append("## Citation hygiene")
        out.append("")
        out.append(f"- {ch['grounded_count']}/{ch['total']} grounded (every cited doc_id appears in some tool transcript)")
        out.append(f"- {len(ch['no_citations'])} captures with no citations")
        out.append(f"- {len(ch['fabricated_citations'])} captures with fabricated citations (cited doc_id not seen in any tool response)")
        out.append("")
        out.append("Note: \"seen in transcript\" uses a UUID-shape regex over `result_summary`; false positives/negatives possible. Treat as a heuristic.")
        out.append("")
        if ch["no_citations"]:
            out.append("### No-citation captures")
            for nc in ch["no_citations"][:20]:
                out.append(f"- `{nc['qid']}` — {nc['answer_preview'][:120]}")
            out.append("")
        if ch["fabricated_citations"]:
            out.append("### Fabricated-citation captures")
            for fab in ch["fabricated_citations"][:20]:
                out.append(f"- `{fab['qid']}` — cited {fab['cited']}, seen-in-transcript {fab['seen_in_transcript']}")
            out.append("")

    cj = audit.get("cross_judge")
    if cj:
        ja, jb = cj["judges"]
        out.append(f"## Cross-judge — {ja} vs {jb} (Δ = {jb} − {ja}; n={cj['n']})")
        out.append("")
        out.append("| dim | mean Δ | std | min | max | spearman | kappa |")
        out.append("| --- | --- | --- | --- | --- | --- | --- |")
        for dim in ("correctness", "groundedness", "completeness"):
            d = cj["deltas"][dim]
            out.append(
                f"| {dim} | {d['mean']:+.2f} | {d['std']:.2f} | {d['min']:+d} | {d['max']:+d} | {cj['spearman'][dim]:.2f} | {cj['kappa'][dim]:.2f} |"
            )
        out.append("")
        if cj["disagreements"]:
            out.append(f"### Top disagreements (|Δ| ≥ 2 on any dimension; {len(cj['disagreements'])} items)")
            out.append("")
            for dis in cj["disagreements"][:20]:
                a, b = dis["judge_a"], dis["judge_b"]
                out.append(
                    f"- `{dis['qid']}` — {dis['delta_dim']} {a[dis['delta_dim']]} ({ja}) vs {b[dis['delta_dim']]} ({jb})"
                )
                out.append(f"  - {ja}: {a.get('rationale', '')[:140]}")
                out.append(f"  - {jb}: {b.get('rationale', '')[:140]}")
            out.append("")

    return "\n".join(out)


def _render_fc_block(out: list[str], title: str, items: list[dict]) -> None:
    out.append(f"### {title} ({len(items)} items)")
    if not items:
        out.append("- (none)")
    for item in items[:20]:
        tools = ", ".join(item.get("tools_used", []))
        corr = item.get("correctness", "?")
        line = f"- `{item['qid']}` — correctness {corr}, tools [{tools}]"
        if item.get("title_fragment"):
            line += f" — {item['title_fragment']}"
        out.append(line)
    out.append("")
```

And wire markdown output into `main()` — replace the existing `# audit.md wired in Task 7.` comment block:

```python
    # In main(), after writing audit.json:
    (output_dir / "audit.md").write_text(render_markdown(audit))
    log.info("wrote %s", output_dir / "audit.md")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest scripts/test_corpora/tests/test_audit_cli.py -v`
Expected: 10 PASS (7 from Task 6 + 3 from this task)

- [ ] **Step 5: Ruff + format**

Run: `uv run ruff check scripts/test_corpora/audit_answer_eval.py scripts/test_corpora/tests/test_audit_cli.py`

Run: `uv run ruff format scripts/test_corpora/audit_answer_eval.py scripts/test_corpora/tests/test_audit_cli.py`

- [ ] **Step 6: Commit**

```bash
git add scripts/test_corpora/audit_answer_eval.py scripts/test_corpora/tests/test_audit_cli.py
git commit -m "feat(audit-cli): markdown report rendering"
```

---

## Task 8: Wire `--cross-judge` into the CLI orchestrator

**Files:**
- Modify: `scripts/test_corpora/audit_answer_eval.py`
- Modify: `scripts/test_corpora/tests/test_audit_cli.py`

- [ ] **Step 1: Write the failing tests** (append)

```python
# Append to scripts/test_corpora/tests/test_audit_cli.py

from unittest.mock import patch

from scripts.test_corpora.runner.cross_judge import JudgeProvider


class _FixedJudge:
    """Always returns the same canned verdict for any prompt."""
    def judge(self, prompt: str) -> str:
        return json.dumps({"correctness": 5, "groundedness": 4, "completeness": 5, "rationale": "ok"})


def test_main_cross_judge_invokes_rejudge_and_compare(tmp_path):
    cap_dir = tmp_path / "answer-eval" / "captures" / "synthetic" / "claude-sonnet-4-6"
    _write_capture(cap_dir, "q1", ["kb_search"])
    _write_capture(cap_dir, "q2", ["kb_search"])
    v_dir = tmp_path / "answer-eval" / "verdicts" / "synthetic" / "claude-sonnet-4-6"
    _write_verdict(v_dir, "q1", correctness=5)
    _write_verdict(v_dir, "q2", correctness=4)

    with patch("scripts.test_corpora.audit_answer_eval._build_judge_provider") as mb:
        mb.return_value = _FixedJudge()
        rc = main([
            "--workdir", str(tmp_path),
            "--label", "smoke",
            "--cross-judge", "gpt-4o",
        ])
    assert rc == 0
    audit = json.loads((tmp_path / "answer-eval" / "reports" / "smoke" / "audit.json").read_text())
    assert audit["cross_judge"] is not None
    assert audit["cross_judge"]["judges"] == ["claude-sonnet-4-6", "gpt-4o"]
    assert audit["cross_judge"]["n"] == 2

    md = (tmp_path / "answer-eval" / "reports" / "smoke" / "audit.md").read_text()
    assert "## Cross-judge" in md


def test_main_skip_static_only_runs_cross_judge(tmp_path):
    cap_dir = tmp_path / "answer-eval" / "captures" / "synthetic" / "claude-sonnet-4-6"
    _write_capture(cap_dir, "q1", ["kb_search"])
    v_dir = tmp_path / "answer-eval" / "verdicts" / "synthetic" / "claude-sonnet-4-6"
    _write_verdict(v_dir, "q1", correctness=5)

    with patch("scripts.test_corpora.audit_answer_eval._build_judge_provider") as mb:
        mb.return_value = _FixedJudge()
        rc = main([
            "--workdir", str(tmp_path), "--label", "smoke",
            "--cross-judge", "gpt-4o", "--skip-static",
        ])
    assert rc == 0
    audit = json.loads((tmp_path / "answer-eval" / "reports" / "smoke" / "audit.json").read_text())
    assert audit["tool_use"] is None
    assert audit["failure_correlation"] is None
    assert audit["citation_hygiene"] is None
    assert audit["cross_judge"] is not None


def test_main_cross_judge_requires_openai_api_key(tmp_path, monkeypatch):
    """Without --cross-judge mocked AND without OPENAI_API_KEY, exit 1 with clear message."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cap_dir = tmp_path / "answer-eval" / "captures" / "synthetic" / "claude-sonnet-4-6"
    _write_capture(cap_dir, "q1", ["kb_search"])
    rc = main([
        "--workdir", str(tmp_path), "--label", "smoke", "--cross-judge", "gpt-4o",
    ])
    assert rc == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest scripts/test_corpora/tests/test_audit_cli.py -v -k cross_judge`
Expected: 3 FAIL — `ImportError` on `_build_judge_provider` or the cross-judge section not present in the audit.

- [ ] **Step 3: Wire `--cross-judge` into `main`**

Edit `scripts/test_corpora/audit_answer_eval.py` `main` to call `rejudge_with` + `compare_judges` when `--cross-judge` is supplied. Add a `_build_judge_provider` helper that constructs the `OpenAIJudgeProvider` (so tests can monkey-patch it).

Add this helper above `main`:

```python
def _build_judge_provider(model: str):
    """Build a JudgeProvider for the named model. Test seam: patched in tests."""
    if not os.environ.get("OPENAI_API_KEY"):
        sys.stderr.write("audit: --cross-judge requires OPENAI_API_KEY in the environment\n")
        raise SystemExit(1)
    # Lazy import keeps the static-only path importable without `openai`.
    from scripts.test_corpora.runner.cross_judge import OpenAIJudgeProvider
    return OpenAIJudgeProvider(model=model)
```

Replace the `# Cross-judge wired in Task 8.` placeholder in `main` with:

```python
    # Cross-judge re-score (optional)
    if args.cross_judge:
        try:
            judge_provider = _build_judge_provider(args.cross_judge)
        except SystemExit as exc:
            return int(exc.code or 1)
        from scripts.test_corpora.runner.cross_judge import compare_judges, rejudge_with
        rejudge_results = rejudge_with(
            captures, judge_provider,
            judge_model=args.cross_judge,
            items=args.rejudge_sample,
        )
        # Reshape verdicts (from detail.json or per-item files) to the
        # compare_judges input shape: needs {"qid", correctness, groundedness,
        # completeness, rationale}.
        verdicts_a = [
            {
                "qid": v.get("id"),
                "correctness": v.get("correctness", 0),
                "groundedness": v.get("groundedness", 0),
                "completeness": v.get("completeness", 0),
                "rationale": v.get("rationale", ""),
            }
            for v in verdicts
        ]
        audit["cross_judge"] = compare_judges(
            verdicts_a, rejudge_results, judges=(model, args.cross_judge)
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest scripts/test_corpora/tests/test_audit_cli.py -v`
Expected: 13 PASS (10 from Tasks 6+7 + 3 from this task)

- [ ] **Step 5: Full-file unit run**

Run: `uv run pytest scripts/test_corpora/tests/ -v`
Expected: all PASS (16 audit + 16 cross_judge + 13 cli = 45 new)

- [ ] **Step 6: Ruff + format**

Run: `uv run ruff check scripts/test_corpora/`

Run: `uv run ruff format scripts/test_corpora/`

- [ ] **Step 7: Commit**

```bash
git add scripts/test_corpora/audit_answer_eval.py scripts/test_corpora/tests/test_audit_cli.py
git commit -m "feat(audit-cli): wire --cross-judge re-score + compare_judges into report"
```

---

## Task 9: Full-suite regression + live smoke + fresh-eyes review + open PR

**Files:** No code changes initially; review may surface fixes.

- [ ] **Step 1: Run the full Harbor Clerk test suite**

Run: `uv run pytest tests/ --ignore=tests/test_macos_smoke.py -q 2>&1 | tail -5`
Expected: All HC tests still pass (count should be ≥1137; the new test files live under `scripts/test_corpora/tests/` which has its own conftest).

- [ ] **Step 2: Run the test_corpora test suite**

Run: `uv run pytest scripts/test_corpora/tests/ -v 2>&1 | tail -10`
Expected: All tests pass (≥45 new + the existing suite).

- [ ] **Step 3: Ruff check + format check on the whole repo**

Run: `uv run ruff check . 2>&1 | tail -5`
Expected: "All checks passed!"

Run: `uv run ruff format --check . 2>&1 | tail -3`
Expected: All files formatted.

- [ ] **Step 4: Live smoke run** (manual, deferred — does NOT block PR open)

Documents the run command for the operator. Skip if no `OPENAI_API_KEY` available.

```
OPENAI_API_KEY=sk-... uv run python scripts/test_corpora/audit_answer_eval.py \
  --label synthetic-phase2b \
  --cross-judge gpt-4o \
  --rejudge-sample 3
```

Confirms end-to-end: file discovery, audit functions on real data, OpenAI provider integration, output writing, markdown rendering. Report any failures back in the conversation; if successful, attach a short excerpt of the `audit.md` to the PR description as evidence.

- [ ] **Step 5: Push the branch**

```bash
git push -u origin feat/pr-g-harness-audit
```

- [ ] **Step 6: Dispatch the fresh-eyes reviewer**

Use the `Agent` tool with:
- `subagent_type`: `feature-dev:code-reviewer`
- Minimal prompt: state the branch + the PR-G goal (harness audit + cross-judge); link the spec doc; tell the reviewer to report ≥80-confidence findings only. No focus areas, no carve-outs.

Address any ≥80-confidence findings inline; re-run the test suite + ruff after each fix; commit each fix with `fix(pr-g):` prefix.

- [ ] **Step 7: Open the PR**

```bash
gh pr create --title "feat(eval): harness audit + cross-judge sensitivity (PR-G)" \
  --body-file /tmp/pr-g-body.md
```

PR body should include:
- **Summary** (2–3 bullets: audit script + cross-judge re-score, no changes to existing eval modules)
- **Why** (eval-trust motivation: hand-inspection doesn't scale; Sonnet-only judge has bias risk)
- **What's in it** (file list grouped by module)
- **Test plan** (commands run, suite size, fresh-eyes review summary, live smoke result if it ran)
- **Spec + plan links** (this plan + the spec doc)

- [ ] **Step 8: Update `pr_followups.md` with the PR's deferred-work section**

The spec's "Out of scope / follow-ups" section ports straight into the PR body's deferred-work section. Per the standing directive, every PR with a deferred-work section also gets entries in `~/.claude/projects/-Users-alex-mcp-gateway/memory/pr_followups.md` (relevant items: auto-suggest fixes, multi-judge ensemble, sweep-time auto-emission, cross-run comparison, locally-hosted judge, self-consistency, query-diversity audit pattern, tool-misuse audit pattern, audit threshold tuning).

- [ ] **Step 9: Enable auto-merge**

```bash
gh pr merge <PR-number> --auto --squash
```

The harness won't notify on merge; the user will see it land.

---

## Self-Review Notes

Spec section-by-section:

- **§ Goal + Why**: covered by Tasks 1–8 (audit + cross-judge functional surfaces). ✓
- **§ Non-goals**: not encoded as tasks (negative space). ✓
- **§ Architecture**: Tasks 1+2+3 (`audit.py`), 4+5 (`cross_judge.py`), 6+7+8 (`audit_answer_eval.py`). Every new file in the spec's File Structure has a task. No `runner/answer_judge.py` modification (preserves spec's "no changes to existing eval modules"). ✓
- **§ `runner/audit.py`** — three functions, three tasks. Each function's output shape from the spec is asserted in the corresponding test. ✓
- **§ `runner/cross_judge.py`** — JudgeProvider Protocol + OpenAIJudgeProvider + rejudge_with in Task 4; compare_judges + stats helpers in Task 5. ✓
- **§ CLI + output format**: Task 6 (CLI + JSON), Task 7 (Markdown), Task 8 (cross-judge wiring). ✓
- **§ Testing**: each task includes ~5–10 tests; full-suite regression in Task 9. ✓
- **§ Decisions**: all 9 decisions reflected in the code:
  - One PR ✓ (single branch, single plan)
  - 3 audit patterns (tool_use_stats, failure_correlation, citation_hygiene) ✓
  - Standalone CLI script ✓
  - JSON + Markdown output ✓
  - gpt-4o only judge ✓ (OpenAIJudgeProvider is the only impl)
  - Sample = full re-judge on one (corpus, baseline) — CLI defaults to all captures, `--rejudge-sample` for partial ✓
  - Stats by hand ✓ (`_spearman`, `_cohens_kappa` in Task 5)
  - Reuse `_PROMPT` / `_PROMPT_FIND` ✓ (import in Task 4)
  - Module split ✓
- **§ Out of scope**: items deferred by absence, reflected in `pr_followups.md` step in Task 9. ✓
- **§ Open questions/risks**: doc_id regex documented as a heuristic in `audit.md` rendering (Task 7); rationale-string heuristic likewise mentioned implicitly via the "Possibly-ambiguous" section title; stats hand-roll tests added in Task 5; cross-judge cost documented in CLI docstring. ✓

**Spec deviation noted in Task 4:** the spec said "via the existing PR-C `Provider` interface" — that's wrong (PR-C's `Provider` is for baseline answering). The plan introduces a narrower `JudgeProvider` Protocol with one method. Functionally equivalent intent; cleaner separation.

**Type-consistency spot check:**
- `tool_use_stats(captures: list[dict]) -> dict` — same signature across Task 1 implementation, Task 6 CLI caller. ✓
- `failure_correlation(captures, verdicts) -> dict` — same. ✓
- `citation_hygiene(captures) -> dict` — same. ✓
- `JudgeProvider.judge(prompt: str) -> str` — same in Protocol definition (Task 4) + MockJudge tests + OpenAIJudgeProvider impl. ✓
- `rejudge_with(captures, judge_provider, *, judge_model, items=None, seed=42, answer_keys=None, qtypes=None) -> list[dict]` — same across Task 4 impl + Task 8 CLI caller. ✓
- `compare_judges(verdicts_a, verdicts_b, *, judges) -> dict` — same across Task 5 impl + Task 8 CLI caller. ✓

**Placeholder scan:** no "TBD" / "TODO" / "fill in" / "similar to" tokens in the plan. Every code-touching step shows the actual code.

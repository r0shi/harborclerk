# Enron Answer-Eval + Judge `find` Type — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend `--mode answer-eval` from CUAD (phase 1) to Enron, introducing a third question type — `find` — alongside `lookup` and `negative`, with deterministic coverage scoring for `find` items.

**Architecture:** Three additive changes to the existing answer-eval pipeline — a new generator (`generate_enron.py`) that derives ground truth by grepping `.eml` files, a judge `_PROMPT` that branches by question type, and a runner override that replaces the judge's `completeness` with a precise set-overlap percentage for `find` items. Runner CLI, sweep wiring, and capture/verdict file shapes stay unchanged (back-compat).

**Tech Stack:** Python 3.12, `pyyaml`, `anthropic`, stdlib `email`/`subprocess`, `pytest`. All work is in `scripts/test_corpora/`. Run tests with `uv run --project scripts/test_corpora --extra test pytest <path>` from the repo root.

**Spec:** `docs/superpowers/specs/2026-05-22-answer-eval-enron-design.md`.

**File map:**
- Create `scripts/test_corpora/groundtruth/generate_enron.py` — Enron ground-truth generator.
- Create `scripts/test_corpora/groundtruth/enron.yaml` — frozen ~11-item ground-truth set (generated, then committed).
- Modify `scripts/test_corpora/runner/answer_judge.py` — add `AnswerVerdict.source` field; branch `_PROMPT` by `qtype` (new `_PROMPT_FIND`).
- Modify `scripts/test_corpora/runner/answer_eval.py` — add `compute_coverage`; broaden `GTItem.answer_key` to allow dict; validate dict shape for `find` in `load_groundtruth`; add the `find`-item override in `run()`.
- Create `scripts/test_corpora/tests/test_generate_enron.py` — generator tests.
- Modify `scripts/test_corpora/tests/test_answer_judge.py` — `AnswerVerdict.source` + `find` prompt tests.
- Modify `scripts/test_corpora/tests/test_answer_eval.py` — `compute_coverage` unit tests + `find` reuse test + load-validation tests.

---

## Task 1: `compute_coverage` helper

A pure function — deterministic coverage score for `find` items, mapped to the 0–5 scale, with a special case for negatives (`truth_all == []`). Used by the runner in Task 7.

**Files:**
- Modify: `scripts/test_corpora/runner/answer_eval.py` — add `compute_coverage` after `_cited_text`.
- Modify: `scripts/test_corpora/tests/test_answer_eval.py` — add 5 unit tests.

- [ ] **Step 1: Write the failing tests** — append to `scripts/test_corpora/tests/test_answer_eval.py`:

```python
from scripts.test_corpora.runner.answer_eval import compute_coverage


def test_compute_coverage_negative_clean_decline():
    """find-negative: truth=[], cited=[] — citing nothing is correct (5)."""
    assert compute_coverage([], []) == 5


def test_compute_coverage_negative_penalizes_false_positives():
    """find-negative: each cited doc costs a point, floor 0."""
    assert compute_coverage(["x"], []) == 4
    assert compute_coverage(["x", "y", "z"], []) == 2
    assert compute_coverage(["a", "b", "c", "d", "e"], []) == 0
    assert compute_coverage(["a"] * 10, []) == 0  # floor


def test_compute_coverage_exact_match():
    """find: cited == truth -> 5."""
    assert compute_coverage(["a", "b", "c"], ["a", "b", "c"]) == 5


def test_compute_coverage_partial_overlap_uses_banker_rounding():
    """find: round(overlap / len(truth) * 5). Python 3's round is banker's."""
    # 1/2 = 0.5 -> 2.5 -> 2 (banker's: rounds to even)
    assert compute_coverage(["a"], ["a", "b"]) == 2
    # 2/4 = 0.5 -> 2.5 -> 2
    assert compute_coverage(["a", "b"], ["a", "b", "c", "d"]) == 2
    # 3/4 = 0.75 -> 3.75 -> 4
    assert compute_coverage(["a", "b", "c"], ["a", "b", "c", "d"]) == 4


def test_compute_coverage_over_cite_does_not_penalize():
    """find: extras in cited beyond truth don't reduce coverage — coverage is
    recall (|overlap| / |truth|), not precision."""
    assert compute_coverage(["a", "b", "x", "y"], ["a", "b"]) == 5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --project scripts/test_corpora --extra test pytest scripts/test_corpora/tests/test_answer_eval.py -v -k compute_coverage`
Expected: FAIL — `ImportError: cannot import name 'compute_coverage' from 'scripts.test_corpora.runner.answer_eval'`.

- [ ] **Step 3: Implement `compute_coverage`** — in `scripts/test_corpora/runner/answer_eval.py`, add immediately after the `_cited_text` function:

```python
def compute_coverage(cited: list[str], truth_all: list[str]) -> int:
    """Deterministic coverage score for `find` items, mapped to the 0–5 scale.

    For `find`-negatives (``truth_all == []``): citing nothing is correct (5);
    each false-positive citation costs 1 point, floor 0.
    For `find` items with a non-empty truth: ``|cited ∩ truth| / |truth| * 5``,
    banker's-rounded to an int. Used by ``run()`` to override the judge's
    ``completeness`` for ``find`` items — see the answer-eval phase-2a design
    (Enron).
    """
    if not truth_all:
        return max(0, 5 - len(cited))
    overlap = len(set(cited) & set(truth_all))
    return round(overlap / len(truth_all) * 5)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --project scripts/test_corpora --extra test pytest scripts/test_corpora/tests/test_answer_eval.py -v -k compute_coverage`
Expected: PASS (5 tests).

- [ ] **Step 5: Lint + commit**

```bash
uv run --project scripts/test_corpora ruff check scripts/test_corpora/runner/answer_eval.py
uv run --project scripts/test_corpora ruff format scripts/test_corpora/runner/answer_eval.py scripts/test_corpora/tests/test_answer_eval.py
git add scripts/test_corpora/runner/answer_eval.py scripts/test_corpora/tests/test_answer_eval.py
git commit -m "feat(eval): compute_coverage — set-overlap scorer for find items"
```

Append this trailer to the commit message (blank line before it):
`Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`

---

## Task 2: `AnswerVerdict.source` field

Add an optional `source: dict[str, str]` field to `AnswerVerdict` so verdict JSON files record which dimensions came from which scorer. Default empty dict; back-compat for legacy serialized verdicts.

**Files:**
- Modify: `scripts/test_corpora/runner/answer_judge.py` — add field with `default_factory=dict`.
- Modify: `scripts/test_corpora/tests/test_answer_judge.py` — 3 round-trip tests.

- [ ] **Step 1: Write the failing tests** — append to `scripts/test_corpora/tests/test_answer_judge.py`:

```python
def test_answer_verdict_source_defaults_to_empty_dict():
    """AnswerVerdict has an optional `source` field defaulting to {}."""
    v = AnswerVerdict(correctness=5, groundedness=5, completeness=5, rationale="ok")
    assert v.source == {}


def test_answer_verdict_source_round_trips_via_json():
    """AnswerVerdict(...) -> asdict -> json -> dict -> AnswerVerdict(**dict) cleanly."""
    import dataclasses
    import json

    original = AnswerVerdict(
        correctness=4, groundedness=3, completeness=5, rationale="x",
        source={"completeness": "deterministic"},
    )
    serialized = json.dumps(dataclasses.asdict(original))
    round_tripped = AnswerVerdict(**json.loads(serialized))
    assert round_tripped == original


def test_answer_verdict_deserializes_legacy_payload_without_source():
    """Existing verdict files (no `source` key) deserialize cleanly with source={}."""
    legacy = {"correctness": 5, "groundedness": 4, "completeness": 5, "rationale": "ok"}
    v = AnswerVerdict(**legacy)
    assert v.source == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --project scripts/test_corpora --extra test pytest scripts/test_corpora/tests/test_answer_judge.py -v -k source`
Expected: FAIL — `TypeError: AnswerVerdict.__init__() got an unexpected keyword argument 'source'`.

- [ ] **Step 3: Add the field** — in `scripts/test_corpora/runner/answer_judge.py`, replace the `AnswerVerdict` dataclass:

```python
@dataclasses.dataclass
class AnswerVerdict:
    correctness: int
    groundedness: int
    completeness: int
    rationale: str
    # Per-dimension scorer source ("deterministic" for runner-overridden find
    # completeness; absent/empty for default judge-scored values). Lets reports
    # show which numbers came from which scorer. Back-compat: legacy verdict
    # JSONs without this key deserialize with source={}.
    source: dict[str, str] = dataclasses.field(default_factory=dict)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --project scripts/test_corpora --extra test pytest scripts/test_corpora/tests/test_answer_judge.py -v`
Expected: PASS (all tests — the 3 new `source` tests + all existing).

- [ ] **Step 5: Lint + commit**

```bash
uv run --project scripts/test_corpora ruff check scripts/test_corpora/runner/answer_judge.py
git add scripts/test_corpora/runner/answer_judge.py scripts/test_corpora/tests/test_answer_judge.py
git commit -m "feat(eval): AnswerVerdict.source — track which dimensions came from which scorer"
```

Append the Co-Authored-By trailer.

---

## Task 3: Broaden `GTItem.answer_key` to allow dict; validate `find` shape

`GTItem.answer_key` is currently typed `str | None`. For `find` items we need to allow a `dict` (the `{count, all, sample}` shape). `load_groundtruth` validates the shape per type.

**Files:**
- Modify: `scripts/test_corpora/runner/answer_eval.py` — broaden type annotation; validate in `load_groundtruth`.
- Modify: `scripts/test_corpora/tests/test_answer_eval.py` — 2 failing validation tests + 1 positive load test + add `import pytest`.

- [ ] **Step 1: Write the failing tests** — first ensure `pytest` is imported at the top of `scripts/test_corpora/tests/test_answer_eval.py`. If it's not, add this near the existing imports:

```python
import pytest
```

Then append these tests at the end of the file:

```python
def test_load_groundtruth_validates_find_answer_key_is_a_dict(tmp_path: Path):
    """find items must carry a dict answer_key, not a string."""
    gt = tmp_path / "enron.yaml"
    gt.write_text(yaml.safe_dump({"corpus": "enron", "items": [
        {"id": "find-bad", "question": "Find x.", "clause_category": "n/a",
         "gold_doc": "(see answer_key.all)",
         "answer_key": "wrong shape — should be a dict", "type": "find"},
    ]}))
    with pytest.raises(ValueError, match="find item .* answer_key must be a dict"):
        load_groundtruth(gt)


def test_load_groundtruth_validates_find_answer_key_required_keys(tmp_path: Path):
    """find item's answer_key must include count, all, and sample."""
    gt = tmp_path / "enron.yaml"
    gt.write_text(yaml.safe_dump({"corpus": "enron", "items": [
        {"id": "find-incomplete", "question": "Find x.", "clause_category": "n/a",
         "gold_doc": "(see answer_key.all)",
         "answer_key": {"count": 5}, "type": "find"},
    ]}))
    with pytest.raises(ValueError, match="find item .* missing required answer_key keys"):
        load_groundtruth(gt)


def test_load_groundtruth_accepts_well_formed_find_item(tmp_path: Path):
    """A valid find item with the {count, all, sample} answer_key loads cleanly."""
    gt = tmp_path / "enron.yaml"
    gt.write_text(yaml.safe_dump({"corpus": "enron", "items": [
        {"id": "find-x", "question": "Find x.", "clause_category": "n/a",
         "gold_doc": "(see answer_key.all)",
         "answer_key": {"count": 2, "all": ["a.eml", "b.eml"], "sample": ["a.eml"]},
         "type": "find"},
    ]}))
    items = load_groundtruth(gt)
    assert items[0].answer_key == {"count": 2, "all": ["a.eml", "b.eml"], "sample": ["a.eml"]}
    assert items[0].type == "find"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --project scripts/test_corpora --extra test pytest scripts/test_corpora/tests/test_answer_eval.py -v -k validates_find`
Expected: 2 FAIL (`load_groundtruth` doesn't raise — current loader has no validation). The third test (`accepts_well_formed_find_item`) may pass already because the existing loader stores `answer_key` as whatever shape it came in as, but it covers the regression baseline for Step 3.

- [ ] **Step 3: Broaden the annotation + add validation** — in `scripts/test_corpora/runner/answer_eval.py`:

First, change the `GTItem.answer_key` annotation:

```python
@dataclasses.dataclass(frozen=True)
class GTItem:
    id: str
    question: str
    clause_category: str
    gold_doc: str
    answer_key: str | dict | None
    type: str
```

Then replace `load_groundtruth` with the validating version:

```python
def load_groundtruth(path: Path) -> list[GTItem]:
    data = yaml.safe_load(path.read_text())
    items: list[GTItem] = []
    for i in data["items"]:
        ak = i.get("answer_key")
        qtype = i["type"]
        if qtype == "find":
            if not isinstance(ak, dict):
                raise ValueError(
                    f"find item {i['id']!r} answer_key must be a dict with count/all/sample, "
                    f"got {type(ak).__name__}"
                )
            missing = {"count", "all", "sample"} - set(ak)
            if missing:
                raise ValueError(
                    f"find item {i['id']!r} missing required answer_key keys: {sorted(missing)}"
                )
        items.append(
            GTItem(
                id=i["id"],
                question=i["question"],
                clause_category=i["clause_category"],
                gold_doc=i["gold_doc"],
                answer_key=ak,
                type=qtype,
            )
        )
    return items
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --project scripts/test_corpora --extra test pytest scripts/test_corpora/tests/test_answer_eval.py -v`
Expected: PASS (all tests — the 3 new load tests + every existing test).

- [ ] **Step 5: Lint + commit**

```bash
uv run --project scripts/test_corpora ruff check scripts/test_corpora/runner/answer_eval.py scripts/test_corpora/tests/test_answer_eval.py
git add scripts/test_corpora/runner/answer_eval.py scripts/test_corpora/tests/test_answer_eval.py
git commit -m "feat(eval): broaden GTItem.answer_key to allow dict; validate find shape"
```

Append the Co-Authored-By trailer.

---

## Task 4: Judge `_PROMPT` branches by `qtype` for `find`

For `find` items, render `count + sample` into a new `_PROMPT_FIND` template and instruct the judge to set `completeness=0` (the runner overrides). For `lookup` / `negative`: prompt unchanged from phase 1.

**Files:**
- Modify: `scripts/test_corpora/runner/answer_judge.py` — add `_PROMPT_FIND`; branch in `judge_answer`.
- Modify: `scripts/test_corpora/tests/test_answer_judge.py` — 2 prompt-rendering tests.

- [ ] **Step 1: Write the failing tests** — append to `scripts/test_corpora/tests/test_answer_judge.py`:

```python
def test_judge_find_type_renders_count_and_sample_in_prompt():
    """For find items the prompt includes count and the rendered sample."""
    c = MagicMock()
    c.messages.create.return_value = MagicMock(content=[MagicMock(
        text='{"correctness": 4, "groundedness": 3, "completeness": 0, "rationale": "covered most"}'
    )])
    j = AnswerJudge(client=c)
    v = j.judge_answer(
        question="Find emails about Raptor.",
        model_answer="Four emails mention Raptor.",
        cited="cited docs: skilling-j_inbox_1109_.eml, lay-k_inbox_268_.eml",
        answer_key={"count": 4, "all": ["a.eml", "b.eml", "c.eml", "d.eml"], "sample": ["a.eml", "b.eml"]},
        qtype="find",
    )
    call_args = c.messages.create.call_args
    prompt_sent = call_args.kwargs["messages"][0]["content"]
    assert "QUESTION TYPE: find" in prompt_sent
    assert "count: 4" in prompt_sent
    assert "a.eml" in prompt_sent
    assert "b.eml" in prompt_sent
    # Judge response parses through; completeness=0 stays for the runner to override.
    assert (v.correctness, v.groundedness, v.completeness) == (4, 3, 0)


def test_judge_find_type_with_empty_sample_renders_cleanly():
    """find-negative (sample=[]) prompts with '(empty)' rather than crashing."""
    c = MagicMock()
    c.messages.create.return_value = MagicMock(content=[MagicMock(
        text='{"correctness": 5, "groundedness": 5, "completeness": 0, "rationale": "clean decline"}'
    )])
    j = AnswerJudge(client=c)
    v = j.judge_answer(
        question="Find emails about cryptocurrency.",
        model_answer="No emails mention cryptocurrency.",
        cited="",
        answer_key={"count": 0, "all": [], "sample": []},
        qtype="find",
    )
    prompt_sent = c.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "count: 0" in prompt_sent
    assert "(empty)" in prompt_sent
    assert v.correctness == 5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --project scripts/test_corpora --extra test pytest scripts/test_corpora/tests/test_answer_judge.py -v -k find_type`
Expected: FAIL — the current prompt does not include `"QUESTION TYPE: find"` text shaped this way, and a dict `answer_key` would render as a Python repr (`{'count': 4, ...}`) rather than the structured format the test asserts.

- [ ] **Step 3: Add `_PROMPT_FIND` and branch in `judge_answer`** — in `scripts/test_corpora/runner/answer_judge.py`, add immediately after the existing `_PROMPT` constant:

```python
_PROMPT_FIND = """You are scoring an answer produced by a document-search assistant.

QUESTION:
{question}

QUESTION TYPE: find
The ground-truth answer key is the exhaustive list of relevant documents
(count: {count}). A representative sample is shown below. The eval runner
computes coverage (recall) separately and overrides completeness, so SET
completeness=0 and let the runner override it.

GROUND-TRUTH SAMPLE ({sample_size} of {count} relevant docs):
{rendered_sample}

THE ASSISTANT'S ANSWER:
{model_answer}

THE PASSAGES THE ASSISTANT CITED:
{cited}

Score two dimensions plus completeness=0, each an integer 0-5:
- correctness: does the assistant's narrative reflect the right documents?
  For a negative (count=0), full marks only if the assistant correctly says no
  relevant emails were found.
- groundedness: do the cited documents support the answer? Cited titles that
  are absent from (and not consistent with) the ground-truth set are
  fabrications and score low.
- completeness: SET TO 0. The runner overrides this with a deterministic
  coverage score; do not guess it.

Reply with ONLY a JSON object:
{{"correctness": <0-5>, "groundedness": <0-5>, "completeness": 0, "rationale": "<one sentence>"}}
"""
```

Then replace the `judge_answer` method on `AnswerJudge`:

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
            correctness=int(data["correctness"]),
            groundedness=int(data["groundedness"]),
            completeness=int(data["completeness"]),
            rationale=str(data.get("rationale", "")),
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --project scripts/test_corpora --extra test pytest scripts/test_corpora/tests/test_answer_judge.py -v`
Expected: PASS (all tests — the 2 new find tests + all existing).

- [ ] **Step 5: Lint + commit**

```bash
uv run --project scripts/test_corpora ruff check scripts/test_corpora/runner/answer_judge.py
git add scripts/test_corpora/runner/answer_judge.py scripts/test_corpora/tests/test_answer_judge.py
git commit -m "feat(eval): judge _PROMPT branches by qtype — find renders count + sample"
```

Append the Co-Authored-By trailer.

---

## Task 5: `generate_enron.py` — generator with per-question recipes

The biggest task. Helpers (`_grep`, `_parse_email_date`, `_find_item`) + per-question recipe functions + the `generate()` orchestrator + CLI. Each recipe produces one item; the orchestrator collects them, verifies negatives are absent, writes the YAML.

**Files:**
- Create: `scripts/test_corpora/groundtruth/generate_enron.py`
- Create: `scripts/test_corpora/tests/test_generate_enron.py`

- [ ] **Step 1: Write the failing tests** — create `scripts/test_corpora/tests/test_generate_enron.py`:

```python
# scripts/test_corpora/tests/test_generate_enron.py
from pathlib import Path

import pytest
import yaml

from scripts.test_corpora.groundtruth.generate_enron import (
    _find_item,
    _grep,
    _parse_email_date,
    generate,
)


def _write_eml(
    path: Path,
    *,
    from_: str = "x@y.com",
    to: str = "a@b.com",
    subject: str = "test",
    date: str = "Wed, 14 Aug 2001 09:00:00 -0500",
    body: str = "body text",
) -> None:
    path.write_text(
        f"From: {from_}\nTo: {to}\nSubject: {subject}\nDate: {date}\n\n{body}",
    )


def test_grep_returns_sorted_filenames_case_insensitive(tmp_path: Path):
    ingest = tmp_path / "ingest"
    ingest.mkdir()
    _write_eml(ingest / "a.eml", body="mentions Raptor in body")
    _write_eml(ingest / "b.eml", body="no match here")
    _write_eml(ingest / "c.eml", body="RAPTOR is in caps")
    matches = _grep(ingest, "raptor")
    assert matches == ["a.eml", "c.eml"]


def test_grep_returns_empty_list_for_absent_term(tmp_path: Path):
    ingest = tmp_path / "ingest"
    ingest.mkdir()
    _write_eml(ingest / "a.eml", body="nothing relevant")
    assert _grep(ingest, "cryptocurrency") == []


def test_parse_email_date_filters_pre_1995_sentinel(tmp_path: Path):
    p = tmp_path / "bad.eml"
    _write_eml(p, date="Tue, 1 Jan 1980 00:00:00 +0000")
    assert _parse_email_date(p) is None


def test_parse_email_date_returns_real_dates(tmp_path: Path):
    p = tmp_path / "ok.eml"
    _write_eml(p, date="Wed, 14 Aug 2001 09:00:00 -0500")
    dt = _parse_email_date(p)
    assert dt is not None
    assert dt.year == 2001 and dt.month == 8 and dt.day == 14


def test_parse_email_date_handles_missing_or_malformed(tmp_path: Path):
    p = tmp_path / "nodate.eml"
    p.write_text("From: x@y\nSubject: no date\n\nbody")
    assert _parse_email_date(p) is None


def test_find_item_builds_count_all_sample_shape():
    item = _find_item("enron-find-x", "Find x.", all_matches=["b.eml", "a.eml", "c.eml"])
    assert item["type"] == "find"
    assert item["id"] == "enron-find-x"
    assert item["question"] == "Find x."
    assert item["answer_key"]["count"] == 3
    # `all` is preserved in the order passed in (the caller sorts); sample takes first K
    assert item["answer_key"]["all"] == ["b.eml", "a.eml", "c.eml"]
    assert item["answer_key"]["sample"] == ["b.eml", "a.eml", "c.eml"]


def test_generate_end_to_end_with_minimal_fixture(tmp_path: Path):
    """Build a small fixture that satisfies every recipe; assert all 11 items."""
    ingest = tmp_path / "ingest"
    ingest.mkdir()
    # Cover each recipe with at least one matching email:
    _write_eml(
        ingest / "skilling-j_doc_1.eml",
        from_="jeff.skilling@enron.com",
        subject="Pre-resignation note about CA",
        date="Wed, 1 Aug 2001 09:00:00 -0500",
        body="The California state of the union is concerning. Also: raptor.",
    )
    _write_eml(
        ingest / "lay-k_inbox_1.eml",
        from_="kenneth.lay@enron.com",
        subject="Fwd: business update",
        date="Mon, 6 Aug 2001 12:00:00 -0500",
        body="LJM and off-balance-sheet structures with FERC oversight; Arthur Andersen too.",
    )
    _write_eml(
        ingest / "neutral_doc_1.eml",
        subject="weather",
        date="Fri, 1 Jun 2001 09:00:00 -0500",
        body="just neutral content",
    )
    out = tmp_path / "enron.yaml"
    n = generate(ingest_dir=ingest, out_path=out)
    assert n == 11

    data = yaml.safe_load(out.read_text())
    assert data["corpus"] == "enron"
    by_id = {i["id"]: i for i in data["items"]}

    # All expected ids are present
    expected = {
        "enron-find-raptor", "enron-find-ljm", "enron-find-offbalancesheet",
        "enron-find-arthurandersen", "enron-find-ferc", "enron-find-layforwarded2001",
        "enron-lookup-earliest-california", "enron-lookup-skilling-last-pre-resign",
        "enron-find-neg-cryptocurrency", "enron-find-neg-bitcoin", "enron-find-neg-spacex",
    }
    assert set(by_id) == expected

    # find items have dict answer_keys with count/all/sample
    for fid in [k for k in by_id if k.startswith("enron-find-") and "neg" not in k]:
        ak = by_id[fid]["answer_key"]
        assert set(ak) == {"count", "all", "sample"}

    # Raptor item picked up the right file
    assert by_id["enron-find-raptor"]["answer_key"]["count"] == 1
    assert by_id["enron-find-raptor"]["answer_key"]["all"] == ["skilling-j_doc_1.eml"]

    # Lookup items
    assert by_id["enron-lookup-earliest-california"]["answer_key"] == "2001-08-01"
    assert by_id["enron-lookup-skilling-last-pre-resign"]["answer_key"] == "Pre-resignation note about CA"

    # Negatives: count=0, all=[], sample=[]
    for nid in ["enron-find-neg-cryptocurrency", "enron-find-neg-bitcoin", "enron-find-neg-spacex"]:
        assert by_id[nid]["answer_key"] == {"count": 0, "all": [], "sample": []}
        assert by_id[nid]["type"] == "find"


def test_generate_raises_when_negative_term_has_hits(tmp_path: Path):
    """The generator refuses to emit a negative whose term unexpectedly matches."""
    ingest = tmp_path / "ingest"
    ingest.mkdir()
    _write_eml(ingest / "a.eml", body="The cryptocurrency market is heating up.")
    out = tmp_path / "enron.yaml"
    with pytest.raises(RuntimeError, match="negative term .* unexpectedly has"):
        generate(ingest_dir=ingest, out_path=out)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --project scripts/test_corpora --extra test pytest scripts/test_corpora/tests/test_generate_enron.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.test_corpora.groundtruth.generate_enron'`.

- [ ] **Step 3: Create the generator** — create `scripts/test_corpora/groundtruth/generate_enron.py` with exactly this content:

```python
# scripts/test_corpora/groundtruth/generate_enron.py
"""Generate the Enron answer-eval ground-truth set by grepping `.eml` files.

Per the answer-eval phase-2a design (Enron), there is no expert-labeled set
analogous to CUAD's master_clauses.csv. The clean alternative — and what makes
coverage (recall) measurable — is to derive truth by directly querying the
raw filesystem: `grep`-style per-term searches plus a small amount of email
header parsing.

Each recipe function emits one item. The orchestrator collects all items, then
verifies that the chosen negative search terms have zero hits before emitting
their negative items (refuses to write the YAML otherwise — that would mask a
corpus shift). Run explicitly; the output is curated once by a human and
committed. Never regenerated as a side effect of an eval run.
"""

from __future__ import annotations

import argparse
import email
import email.utils
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

SENTINEL_DATE_CUTOFF = datetime(1995, 1, 1, tzinfo=timezone.utc)
SAMPLE_K = 10
SKILLING_RESIGN_DATE = datetime(2001, 8, 14, tzinfo=timezone.utc)
SKILLING_ADDRS = ("jeff.skilling", "jskilling", "skilling@enron")
NEGATIVE_TERMS: list[tuple[str, str]] = [
    ("cryptocurrency", "cryptocurrency"),
    ("bitcoin", "Bitcoin"),
    ("spacex", "SpaceX"),
]


# ── helpers ─────────────────────────────────────────────────────────────────

def _grep(ingest_dir: Path, pattern: str) -> list[str]:
    """Return sorted list of .eml filenames matching `pattern` (case-insensitive,
    extended-regex). Uses GNU/BSD `grep -lirE` — list filenames, recursive."""
    result = subprocess.run(
        ["grep", "-l", "-i", "-r", "-E", pattern, str(ingest_dir)],
        capture_output=True,
        text=True,
        check=False,  # grep exits 1 on no matches, which is fine
    )
    files = [Path(p).name for p in result.stdout.strip().splitlines() if p]
    return sorted(files)


def _parse_email_date(path: Path) -> datetime | None:
    """Parse an .eml file's Date header; return None if missing, malformed, or
    a pre-1995 sentinel (PST extractions stub Dates as 1980-01-01 etc.)."""
    try:
        msg = email.message_from_string(path.read_text(errors="replace"))
    except Exception:
        return None
    raw = msg.get("Date")
    if not raw:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    # Compare in UTC to avoid tzinfo mismatches.
    dt_utc = dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    if dt_utc < SENTINEL_DATE_CUTOFF:
        return None
    return dt


def _find_item(id_: str, question: str, *, all_matches: list[str]) -> dict:
    """Build a `find`-typed item with the {count, all, sample} answer_key shape."""
    return {
        "id": id_,
        "question": question,
        "clause_category": "n/a",
        "gold_doc": "(see answer_key.all)",
        "answer_key": {
            "count": len(all_matches),
            "all": all_matches,
            "sample": all_matches[:SAMPLE_K],
        },
        "type": "find",
    }


# ── recipes (one per ground-truth item) ─────────────────────────────────────

def _recipe_raptor(ingest: Path) -> dict:
    return _find_item("enron-find-raptor", "Find emails about Raptor.", all_matches=_grep(ingest, "raptor"))


def _recipe_ljm(ingest: Path) -> dict:
    return _find_item("enron-find-ljm", "Find emails about LJM.", all_matches=_grep(ingest, "ljm"))


def _recipe_offbalance(ingest: Path) -> dict:
    return _find_item(
        "enron-find-offbalancesheet",
        "Find emails containing 'off-balance-sheet'.",
        all_matches=_grep(ingest, "off.balance.sheet"),
    )


def _recipe_andersen(ingest: Path) -> dict:
    return _find_item(
        "enron-find-arthurandersen",
        "Find emails mentioning Arthur Andersen.",
        all_matches=_grep(ingest, "arthur.andersen"),
    )


def _recipe_ferc(ingest: Path) -> dict:
    return _find_item(
        "enron-find-ferc", "Find emails about FERC.", all_matches=_grep(ingest, "\\bferc\\b")
    )


def _recipe_lay_forwarded_2001(ingest: Path) -> dict:
    """List emails forwarded by Lay during 2001 — heuristic over the lay-k
    folder: 2001 in Date header + Fw/Fwd in Subject."""
    matches: list[str] = []
    for p in sorted(ingest.glob("lay-k*.eml")):
        try:
            msg = email.message_from_string(p.read_text(errors="replace"))
        except Exception:
            continue
        subj = (msg.get("Subject") or "").lower()
        date = msg.get("Date") or ""
        if "2001" not in date:
            continue
        if not any(tag in subj for tag in ("fw:", "fwd:", "fw ", "fwd ")):
            continue
        matches.append(p.name)
    matches.sort()
    return _find_item(
        "enron-find-layforwarded2001",
        "List emails forwarded by Lay during 2001.",
        all_matches=matches,
    )


def _recipe_earliest_california(ingest: Path) -> dict:
    """The date of the earliest California-mentioning email (sentinel-filtered)."""
    dated: list[tuple[datetime, str]] = []
    for name in _grep(ingest, "california"):
        dt = _parse_email_date(ingest / name)
        if dt is not None:
            dated.append((dt, name))
    if not dated:
        raise RuntimeError("no California-mentioning emails found after the sentinel filter")
    dated.sort()
    earliest_dt, earliest_name = dated[0]
    return {
        "id": "enron-lookup-earliest-california",
        "question": "What was the date of the earliest email about California in the corpus?",
        "clause_category": "n/a",
        "gold_doc": earliest_name,
        "answer_key": earliest_dt.date().isoformat(),
        "type": "lookup",
    }


def _recipe_skilling_last(ingest: Path) -> dict:
    """The subject of the last email Skilling sent before his 2001-08-14 resignation."""
    candidates: list[tuple[datetime, str, str]] = []
    for p in sorted(ingest.glob("*.eml")):
        try:
            msg = email.message_from_string(p.read_text(errors="replace"))
        except Exception:
            continue
        sender = (msg.get("From") or "").lower()
        if not any(a in sender for a in SKILLING_ADDRS):
            continue
        dt = _parse_email_date(p)
        if dt is None:
            continue
        dt_utc = dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        if dt_utc >= SKILLING_RESIGN_DATE:
            continue
        candidates.append((dt_utc, p.name, msg.get("Subject") or ""))
    if not candidates:
        raise RuntimeError("no pre-resignation Skilling emails found")
    candidates.sort()
    _, latest_name, subject = candidates[-1]
    return {
        "id": "enron-lookup-skilling-last-pre-resign",
        "question": "What was the subject of the last email Skilling sent before his resignation?",
        "clause_category": "n/a",
        "gold_doc": latest_name,
        "answer_key": subject.strip(),
        "type": "lookup",
    }


RECIPES = [
    _recipe_raptor,
    _recipe_ljm,
    _recipe_offbalance,
    _recipe_andersen,
    _recipe_ferc,
    _recipe_lay_forwarded_2001,
    _recipe_earliest_california,
    _recipe_skilling_last,
]


def _recipe_negative(slug: str, term: str) -> dict:
    return {
        "id": f"enron-find-neg-{slug}",
        "question": f"Find emails about {term}.",
        "clause_category": "n/a",
        "gold_doc": "(none expected)",
        "answer_key": {"count": 0, "all": [], "sample": []},
        "type": "find",
    }


# ── orchestrator ────────────────────────────────────────────────────────────

def generate(ingest_dir: Path, out_path: Path) -> int:
    """Emit the Enron ground-truth YAML. Returns the number of items written.

    Refuses to write (raises RuntimeError) if a negative term unexpectedly
    matches in the corpus — that signals a corpus shift and the negative needs
    to be re-chosen.
    """
    items: list[dict] = [recipe(ingest_dir) for recipe in RECIPES]
    for slug, term in NEGATIVE_TERMS:
        hits = _grep(ingest_dir, term)
        if hits:
            raise RuntimeError(
                f"negative term {term!r} unexpectedly has {len(hits)} hit(s) "
                f"(first: {hits[0]}); pick a different absent term"
            )
        items.append(_recipe_negative(slug, term))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(yaml.safe_dump({"corpus": "enron", "items": items}, sort_keys=False))
    return len(items)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate the Enron answer-eval ground-truth set.")
    ap.add_argument("--ingest-dir", type=Path, required=True, help="Enron ingest dir (*.eml)")
    ap.add_argument("--out", type=Path, required=True, help="output enron.yaml")
    a = ap.parse_args(argv)
    n = generate(ingest_dir=a.ingest_dir, out_path=a.out)
    print(f"wrote {n} ground-truth items -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --project scripts/test_corpora --extra test pytest scripts/test_corpora/tests/test_generate_enron.py -v`
Expected: PASS (8 tests).

- [ ] **Step 5: Lint + commit**

```bash
uv run --project scripts/test_corpora ruff check scripts/test_corpora/groundtruth/generate_enron.py scripts/test_corpora/tests/test_generate_enron.py
uv run --project scripts/test_corpora ruff format scripts/test_corpora/groundtruth/generate_enron.py scripts/test_corpora/tests/test_generate_enron.py
git add scripts/test_corpora/groundtruth/generate_enron.py scripts/test_corpora/tests/test_generate_enron.py
git commit -m "feat(eval): Enron ground-truth generator (grep + email parse over .eml files)"
```

Append the Co-Authored-By trailer.

---

## Task 6: Generate & curate the frozen `enron.yaml`

Operational — run the generator against the live Enron corpus, eyeball the output, commit the frozen artifact. Mirrors phase 1's CUAD curation discipline.

- [ ] **Step 1: Run the generator against the live corpus**

```bash
WD="$HOME/Library/Application Support/Harbor Clerk/test-corpora"
uv run --project scripts/test_corpora python -m scripts.test_corpora.groundtruth.generate_enron \
  --ingest-dir "$WD/enron/ingest" \
  --out scripts/test_corpora/groundtruth/enron.yaml
```

Expected: `wrote 11 ground-truth items -> scripts/test_corpora/groundtruth/enron.yaml`.

If a negative term raises `RuntimeError: negative term 'X' unexpectedly has N hit(s)`, pick a replacement (still plausibly business-relevant but absent from pre-2003 Enron — e.g., `quantum computing`, `nft`, `tiktok`) and update `NEGATIVE_TERMS` in `generate_enron.py`, then rerun.

- [ ] **Step 2: Human curation pass**

Open `scripts/test_corpora/groundtruth/enron.yaml`. For each item confirm:

- `count` looks roughly right (Raptor ~4, LJM ~21, off-balance-sheet ~17, Arthur Andersen ~21, FERC ~50, Lay-forwarded-2001 ~100+).
- `gold_doc` for `lookup` items points at a real `.eml` filename.
- `enron-lookup-earliest-california` `answer_key` is a sensible ISO date (post-1999 expected; `1980-01-01` means the sentinel filter failed).
- `enron-lookup-skilling-last-pre-resign` `answer_key` is a real-looking email subject (not blank).
- Question text reads naturally.

If anything looks garbled beyond repair, edit the YAML by hand. (The committed YAML is the source of truth; the generator is a one-shot helper.)

- [ ] **Step 3: Commit the frozen set**

```bash
git add scripts/test_corpora/groundtruth/enron.yaml
git commit -m "feat(eval): frozen Enron ground-truth set (~11 items: find/lookup/negative)"
```

Append the Co-Authored-By trailer.

---

## Task 7: Runner `completeness` override for `find` items

Add a small block in `run()` that, immediately after the judge returns a verdict for a `find` item, overrides `completeness` with `compute_coverage(cited_doc_titles, answer_key["all"])` and records `source["completeness"] = "deterministic"`.

**Files:**
- Modify: `scripts/test_corpora/runner/answer_eval.py` — add the override block in `run()`.
- Modify: `scripts/test_corpora/tests/test_answer_eval.py` — 1 test that a `find` item's `completeness` comes from `compute_coverage`, not from the (fake) judge.

- [ ] **Step 1: Write the failing test** — append to `scripts/test_corpora/tests/test_answer_eval.py`:

```python
def test_run_overrides_completeness_for_find_items_with_compute_coverage(tmp_path: Path):
    """For find items, run() ignores the judge's completeness and uses the
    deterministic set-overlap score instead. source['completeness'] is set."""
    gt = tmp_path / "enron.yaml"
    gt.write_text(yaml.safe_dump({"corpus": "enron", "items": [
        {"id": "find-x", "question": "Find x.", "clause_category": "n/a",
         "gold_doc": "(see answer_key.all)",
         "answer_key": {"count": 4, "all": ["a.eml", "b.eml", "c.eml", "d.eml"],
                        "sample": ["a.eml", "b.eml"]},
         "type": "find"},
    ]}))

    def capture_two_of_four(item):
        # Model cited 2 of 4 truth docs -> coverage = round(2/4 * 5) = round(2.5) = 2
        return {"answer": "Two relevant", "cited_doc_titles": ["a.eml", "c.eml"],
                "tool_transcript": []}

    class FakeJudge:
        def judge_answer(self, **kw):
            # Judge would say 5 — but for find items the runner must override.
            return AnswerVerdict(correctness=5, groundedness=5, completeness=5,
                                 rationale="judge-said-5")

    rc = run(
        workdir=tmp_path, corpus="enron", model="m1", label="ov",
        api_base="http://x", refresh=False, rejudge=False, insecure=True,
        groundtruth_path=gt, capture_fn=capture_two_of_four, judge=FakeJudge(),
    )
    assert rc == 0

    persisted = json.loads(
        (tmp_path / "answer-eval" / "verdicts" / "enron" / "m1" / "find-x.json").read_text()
    )
    assert persisted["correctness"] == 5         # judge's value carried through
    assert persisted["groundedness"] == 5        # ditto
    assert persisted["completeness"] == 2        # overridden by compute_coverage
    assert persisted["source"]["completeness"] == "deterministic"

    summary = json.loads(
        (tmp_path / "answer-eval" / "reports" / "ov" / "summary.json").read_text()
    )
    # by_type now has a "find" bucket
    assert "find" in summary["by_type"]
    assert summary["by_type"]["find"]["completeness"] == 2.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project scripts/test_corpora --extra test pytest scripts/test_corpora/tests/test_answer_eval.py::test_run_overrides_completeness_for_find_items_with_compute_coverage -v`
Expected: FAIL — `persisted["completeness"] == 5` (the judge's value) instead of 2; `source` missing the `completeness` key.

- [ ] **Step 3: Add the override block in `run()`** — in `scripts/test_corpora/runner/answer_eval.py`, find the block inside the `for item in items:` loop that constructs the verdict via `judge.judge_answer(...)`. Replace it with the override-aware version:

```python
        if verdict is None:
            verdict = judge.judge_answer(
                question=item.question,
                model_answer=capture.get("answer", ""),
                cited=_cited_text(capture),
                answer_key=item.answer_key,
                qtype=item.type,
            )
            if item.type == "find":
                truth_all = (
                    item.answer_key.get("all", []) if isinstance(item.answer_key, dict) else []
                )
                coverage = compute_coverage(capture.get("cited_doc_titles", []) or [], truth_all)
                verdict = dataclasses.replace(
                    verdict,
                    completeness=coverage,
                    source={**verdict.source, "completeness": "deterministic"},
                )
            ver_path.write_text(json.dumps(dataclasses.asdict(verdict), indent=2))
```

(The existing `if ver_path.exists() and not rejudge and not refresh:` reuse branch above this is unchanged — when reused from disk, the verdict has already been overridden on the prior write, so `completeness` carries the deterministic value.)

- [ ] **Step 4: Run the full answer-eval test file to verify the new test passes and nothing regressed**

Run: `uv run --project scripts/test_corpora --extra test pytest scripts/test_corpora/tests/test_answer_eval.py -v`
Expected: PASS (all tests).

- [ ] **Step 5: Run the full harness suite as a regression guard**

Run: `uv run --project scripts/test_corpora --extra test pytest scripts/test_corpora/tests/ -q`
Expected: PASS for all this branch's tests. (One pre-existing unrelated failure may appear: `test_entity_overlap_english` — missing `en_core_web_sm` spaCy model in the harness venv. Ignore it.)

- [ ] **Step 6: Lint + commit**

```bash
uv run --project scripts/test_corpora ruff check scripts/test_corpora/runner/answer_eval.py
git add scripts/test_corpora/runner/answer_eval.py scripts/test_corpora/tests/test_answer_eval.py
git commit -m "feat(eval): override completeness with compute_coverage for find items"
```

Append the Co-Authored-By trailer.

---

## Task 8: De-risk + first real Enron run

Operational. Spot-check the generator's output against HC, run a single item to validate end-to-end before launching all 11, then the full run.

- [ ] **Step 1: Spot-check 2 items against HC's index** — manual `/api/search` calls for a couple of the gold docs.

```bash
HC_API_KEY="<enron-scoped key>"
API_BASE="http://localhost:8100"

# Raptor: confirm HC's index returns plausible doc titles for the search.
curl -s -H "Authorization: Bearer $HC_API_KEY" \
  -X POST "$API_BASE/api/search" -H "Content-Type: application/json" \
  -d '{"query": "raptor", "k": 5}' | python3 -m json.tool | head -40

# California earliest: confirm the gold doc cited by the generator is in HC.
GOLD=$(python3 -c "
import yaml
d = yaml.safe_load(open('scripts/test_corpora/groundtruth/enron.yaml'))
print(next(i['gold_doc'] for i in d['items'] if i['id']=='enron-lookup-earliest-california'))
")
echo "expected gold doc: $GOLD"
# Then search HC for a distinctive substring of the gold doc's filename.
```

Expected: at least one match per query; the gold doc title (or a recognizable substring) appears among `/api/search` results.

- [ ] **Step 2: Single-item run to validate end-to-end** — kick off the full eval and watch the first item, then `Ctrl+C` after the first verdict if desired. Captures and verdicts are persisted per-item, so a subsequent re-run picks up where this stopped.

```bash
WD="$HOME/Library/Application Support/Harbor Clerk/test-corpora"
HC_API_KEY="<enron-scoped>" ANTHROPIC_API_KEY="<...>" \
  uv run --project scripts/test_corpora python -m scripts.test_corpora.runner.sweep \
  --run-id answer-eval --mode answer-eval \
  --corpora enron --models claude-sonnet-4-6 --label enron-smoke \
  --workdir "$WD" --api-base http://localhost:8100 2>&1 | tail -20
```

Inspect the first persisted verdict:

```bash
python3 - <<'PY'
import json, os
WD = os.path.expanduser("~/Library/Application Support/Harbor Clerk/test-corpora")
ver_dir = f"{WD}/answer-eval/verdicts/enron/claude-sonnet-4-6"
for f in sorted(os.listdir(ver_dir)):
    print(f, json.load(open(f"{ver_dir}/{f}")))
PY
```

Expected: at least one verdict file; for `find` items, `source["completeness"] == "deterministic"`; scores look plausible.

- [ ] **Step 3: Full run for all 11 items**

```bash
HC_API_KEY="<enron-scoped>" ANTHROPIC_API_KEY="<...>" \
  uv run --project scripts/test_corpora python -m scripts.test_corpora.runner.sweep \
  --run-id answer-eval --mode answer-eval \
  --corpora enron --models claude-sonnet-4-6 --label enron-phase2a \
  --workdir "$WD" --api-base http://localhost:8100
```

Expected: completes with an `OVERALL n=11 correctness=… groundedness=… completeness=…` log line; per-label report at `<WD>/answer-eval/reports/enron-phase2a/summary.json`.

- [ ] **Step 4: Sanity-check the report and post results to the PR**

```bash
cat "$WD/answer-eval/reports/enron-phase2a/summary.json"
```

Confirm:
- `overall.n == 11`.
- `by_type` includes `find` and `lookup` buckets. (Phase-2a folds negatives into `find` — negatives are still `type: find` with `count: 0`.)
- At least a couple of `find` items have non-zero `completeness` (the deterministic coverage produced real numbers).
- A spot-check `detail.json` to confirm `find` items carry `"source": {"completeness": "deterministic"}`.

Post a comment to the PR with the headline numbers + a couple of per-item observations, mirroring PR #381's validation comment.

---

## Self-Review

**1. Spec coverage:**
- §1 Goal → covered by all tasks.
- §2 Scope (Enron, find/lookup/negative, Sonnet 4.6) → Tasks 5, 6, 8.
- §3 Why Enron + raw-filesystem ground truth → Task 5.
- §4 Architecture (additive) → Task 5 (generator), Task 4 (judge), Task 7 (runner override) — preserves runner CLI.
- §5 `find` answer_key shape → Task 3 (broadening + validation), Task 5 (`_find_item`).
- §6 Coverage scoring (deterministic + judge hybrid) → Task 1 (`compute_coverage`), Task 7 (override).
- §7 The ground-truth set (~11 items, recipes per question, brittleness notes) → Task 5, Task 6.
- §8 Judge change → Task 4 (`_PROMPT_FIND` + branch).
- §9 Runner change + `AnswerVerdict.source` → Task 2 (field), Task 7 (override block).
- §10 Harness integration (zero CLI changes) → Task 7 makes no CLI changes; Task 8 uses the existing sweep CLI.
- §11 De-risk → Task 8 Step 1 + Step 2.
- §12 Output (by_type now includes `find`) → Task 7 test verifies; Task 8 Step 4 confirms.
- §13 Testing → covered in each TDD task + Task 8 operational.
- §14 Out of scope — respected (no aggregation questions, no synthetic, no OpenAI).
- §15 Open questions for the plan — negative terms (`cryptocurrency`/`bitcoin`/`spacex` baked in with a swap-out instruction in Task 6); coverage→5 mapping (`round`, with banker's-rounding test pin); sample K=10 (constant in Task 5).

**2. Placeholder scan:** `<enron-scoped>` and `<...>` appear in Task 8 commands — they are credential placeholders the operator must substitute, not plan-implementation placeholders. The `<...>` for `ANTHROPIC_API_KEY` mirrors phase 1's plan. No "TBD" / "TODO" / "implement later" anywhere else.

**3. Type consistency:**
- `AnswerVerdict(correctness, groundedness, completeness, rationale, source={})` — used consistently in Tasks 2, 4, 7.
- `AnswerJudge.judge_answer(question=, model_answer=, cited=, answer_key=, qtype=)` — same keyword-only signature, `answer_key` type widened to `str | dict | None` in Task 4. Consistent with Task 3's `GTItem.answer_key` broadening.
- `GTItem(id, question, clause_category, gold_doc, answer_key, type)` — fields unchanged, `answer_key` type widened. Consistent with `load_groundtruth` in Task 3.
- `compute_coverage(cited: list[str], truth_all: list[str]) -> int` — defined in Task 1, called in Task 7 with the right arg names.
- `_find_item(id_, question, *, all_matches)` — Task 5; takes a list, produces the `{count, all, sample}` answer_key shape consistent with Task 3's validation.

No inconsistencies.

# Real Answer-Level Eval — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `--mode answer-eval` — a repeatable eval that runs a frontier model through Harbor Clerk's MCP over a CUAD ground-truth set and scores each answer for correctness, groundedness, and completeness against expert labels.

**Architecture:** A new sweep mode mirroring `--mode retrieval-eval`. A one-shot generator turns CUAD's `master_clauses.csv` expert annotations into a frozen ground-truth YAML. The runner reuses `BaselineGenerator` to drive the model+MCP, persists model-keyed captures + judge verdicts (reused by default), and writes a labeled report. A new `AnswerJudge` scores answers against the ground-truth key.

**Tech Stack:** Python 3.12, `pyyaml`, `anthropic`, `pytest`/`pytest-asyncio`. All work is in `scripts/test_corpora/`. Run tests with `uv run --project scripts/test_corpora --extra test pytest <path>` from the repo root.

**Spec:** `docs/superpowers/specs/2026-05-22-real-eval-phase1-design.md`.

**File map:**
- Create `scripts/test_corpora/groundtruth/__init__.py` — package marker.
- Create `scripts/test_corpora/groundtruth/generate_cuad.py` — CUAD CSV to ground-truth YAML generator.
- Create `scripts/test_corpora/groundtruth/cuad.yaml` — the frozen ground-truth set (generated, then committed).
- Create `scripts/test_corpora/runner/answer_judge.py` — `AnswerJudge` + `AnswerVerdict`.
- Create `scripts/test_corpora/runner/answer_eval.py` — `run()`, `main_from_args()`, `add_cli_args()`.
- Modify `scripts/test_corpora/runner/claude_baseline.py` — add `tool_transcript` capture.
- Modify `scripts/test_corpora/runner/sweep.py` — register `--mode answer-eval` + dispatch.
- Create tests: `test_generate_cuad.py`, `test_answer_judge.py`, `test_answer_eval.py`.

---

## Task 1: Verify `scope_folder_ids` is a pre-ranking filter

The multi-corpus eval bed (Task 8) depends on a folder-scoped API key restricting search **before** ranking — otherwise a scoped search over a 3-corpus index returns fewer than K results and is not equivalent to a single-corpus index.

**Files:** Read-only investigation — `src/harbor_clerk/search.py`, `src/harbor_clerk/mcp_server.py`, `src/harbor_clerk/api/routes/search.py`.

- [ ] **Step 1: Trace the scope.** In `mcp_server.py` find where `scope_folder_ids=api_key.scope_folder_ids` (around line 104) is passed, and follow it into the search call path. Identify the function in `search.py` that receives it.

- [ ] **Step 2: Classify the filter.** Read that function. Determine whether `scope_folder_ids` becomes a SQL predicate on the candidate set (e.g. `WHERE documents.folder_id = ANY(:ids)` inside the FTS/vector CTEs, applied before `ORDER BY score LIMIT k`) — **pre-ranking** — or whether results are fetched then filtered in Python — **post-ranking**.

- [ ] **Step 3: Record the finding** in the module docstring of `scripts/test_corpora/runner/answer_eval.py` when it is created (Task 6, Step 5), and decide:
  - **Pre-ranking** -> Task 8 proceeds with the 3-corpus bed.
  - **Post-ranking** -> Task 8 falls back to single-corpus loads; note it in the report and file a follow-up to fix HC's filter.

- [ ] **Step 4: Commit the finding.**

```bash
git commit --allow-empty -m "chore(eval): record scope_folder_ids filter classification (pre/post-ranking)"
```

---

## Task 2: CUAD ground-truth generator

Turns CUAD's `master_clauses.csv` into a ground-truth YAML. The `-Answer` columns hold Python-list-repr strings (`"['Delaware']"`, or `"[]"` when the clause is absent). The generator extracts roughly; the output is finalized by a human curation pass (Task 3).

**Files:**
- Create: `scripts/test_corpora/groundtruth/__init__.py` (empty)
- Create: `scripts/test_corpora/groundtruth/generate_cuad.py`
- Test: `scripts/test_corpora/tests/test_generate_cuad.py`

- [ ] **Step 1: Write the failing test**

```python
# scripts/test_corpora/tests/test_generate_cuad.py
import csv
from pathlib import Path

import yaml

from scripts.test_corpora.groundtruth.generate_cuad import generate


def _write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def test_generate_emits_lookup_and_negative_items(tmp_path: Path):
    master = tmp_path / "master_clauses.csv"
    _write_csv(master, [
        {"Filename": "AcmeCo_Distributor Agreement.pdf",
         "Governing Law-Answer": "['Delaware']",
         "Most Favored Nation-Answer": "[]"},
        {"Filename": "BetaCo_License Agreement.pdf",
         "Governing Law-Answer": "['New York']",
         "Most Favored Nation-Answer": "['Section 4.2 grants MFN pricing']"},
    ])
    ingest = tmp_path / "ingest"
    ingest.mkdir()
    (ingest / "AcmeCo_Distributor Agreement.pdf").write_text("x")
    (ingest / "BetaCo_License Agreement.pdf").write_text("x")
    out = tmp_path / "cuad.yaml"

    n = generate(master_csv=master, ingest_dir=ingest, out_path=out, per_category=2)

    assert n >= 3
    data = yaml.safe_load(out.read_text())
    assert data["corpus"] == "cuad"
    by_type = {}
    for item in data["items"]:
        by_type.setdefault(item["type"], []).append(item)
        assert item["gold_doc"]  # filename stem, no .pdf
        assert not item["gold_doc"].endswith(".pdf")
    assert "lookup" in by_type and "negative" in by_type
    neg = by_type["negative"][0]
    assert neg["answer_key"] is None  # CUAD labeled the clause absent
    law = next(i for i in by_type["lookup"] if i["clause_category"] == "Governing Law")
    assert law["answer_key"] == "Delaware"
    assert law["gold_doc"] in {"AcmeCo_Distributor Agreement", "BetaCo_License Agreement"}


def test_generate_skips_contracts_not_in_ingest(tmp_path: Path):
    master = tmp_path / "master_clauses.csv"
    _write_csv(master, [
        {"Filename": "NotSampled_Agreement.pdf", "Governing Law-Answer": "['Texas']"},
    ])
    ingest = tmp_path / "ingest"
    ingest.mkdir()  # empty -- NotSampled is not present
    out = tmp_path / "cuad.yaml"

    n = generate(master_csv=master, ingest_dir=ingest, out_path=out, per_category=2)

    assert n == 0
    assert yaml.safe_load(out.read_text())["items"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project scripts/test_corpora --extra test pytest scripts/test_corpora/tests/test_generate_cuad.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.test_corpora.groundtruth'`.

- [ ] **Step 3: Create the package marker**

Create `scripts/test_corpora/groundtruth/__init__.py` as an empty file.

- [ ] **Step 4: Write the generator**

```python
# scripts/test_corpora/groundtruth/generate_cuad.py
"""Generate the CUAD answer-eval ground-truth set from master_clauses.csv.

CUAD ships expert clause labels (Atticus Project). master_clauses.csv has, per
contract, a `<Category>` column and a `<Category>-Answer` column; the -Answer
cell is a Python-list-repr string of the extracted clause text(s), or "[]" when
the clause is absent. We turn a fixed set of (contract, category) pairs into
ground-truth Q&A: lookups (clause present) and negatives (clause absent).

Run explicitly; the output is curated once by a human and committed. It is
never regenerated as a side effect of an eval run.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import yaml

# Clause category -> question template. {name} is filled with the contract's
# filename stem. Categories chosen for crisp, checkable answers.
CATEGORIES: dict[str, str] = {
    "Governing Law": "What is the governing law of the {name} agreement?",
    "Parties": "Who are the parties to the {name} agreement?",
    "Agreement Date": "What is the agreement date of the {name} agreement?",
    "Expiration Date": "What is the expiration date of the {name} agreement?",
    "Cap On Liability": "What is the cap on liability in the {name} agreement?",
    "Exclusivity": "What exclusivity clause does the {name} agreement contain?",
    "Most Favored Nation": "Does the {name} agreement contain a most-favored-nation clause?",
    "Non-Compete": "What non-compete clause does the {name} agreement contain?",
}


def _parse_answer(cell: str | None) -> str | None:
    """Rough extraction from a master_clauses.csv -Answer cell — a
    Python-list-repr string like "['Delaware']", or "[]" when the clause is
    absent. Returns the answer text, or None when absent. Deliberately crude
    (no code evaluation): the generated set is finalized by a human curation
    pass before being committed, so first-pass roughness is acceptable.
    """
    cell = (cell or "").strip()
    if cell in ("", "[]"):
        return None
    inner = cell.strip("[]").strip()
    # First quoted element, surrounding quote stripped.
    first = inner.split("',")[0].split('",')[0].strip()
    return first.strip("'\"").strip() or None


def generate(master_csv: Path, ingest_dir: Path, out_path: Path, per_category: int = 2) -> int:
    """Emit the ground-truth YAML. Returns the number of items written.

    Selects up to `per_category` lookup items per category, plus one negative
    per category where one exists (a contract where CUAD labeled the clause
    absent). Contracts not present in `ingest_dir` (i.e. not in the sampled set
    HC actually holds) are skipped. Deterministic by sorted filename.
    """
    sampled = {p.stem for p in sorted(ingest_dir.glob("*.pdf"))}
    rows = sorted(csv.DictReader(master_csv.open()), key=lambda r: r.get("Filename", ""))

    items: list[dict] = []
    for category, template in CATEGORIES.items():
        answer_col = f"{category}-Answer"
        lookups: list[dict] = []
        negatives: list[dict] = []
        for row in rows:
            filename = (row.get("Filename") or "").strip()
            stem = filename[:-4] if filename.lower().endswith(".pdf") else filename
            if stem not in sampled:
                continue
            answer = _parse_answer(row.get(answer_col))
            if answer is not None and len(lookups) < per_category:
                lookups.append({
                    "id": f"cuad-gt-{category.lower().replace(' ', '-')}-{len(lookups) + 1}",
                    "question": template.format(name=stem),
                    "clause_category": category,
                    "gold_doc": stem,
                    "answer_key": answer,
                    "type": "lookup",
                })
            elif answer is None and len(negatives) < 1:
                negatives.append({
                    "id": f"cuad-gt-{category.lower().replace(' ', '-')}-neg",
                    "question": template.format(name=stem),
                    "clause_category": category,
                    "gold_doc": stem,
                    "answer_key": None,
                    "type": "negative",
                })
        items.extend(lookups + negatives)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(yaml.safe_dump({"corpus": "cuad", "items": items}, sort_keys=False))
    return len(items)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Generate the CUAD answer-eval ground-truth set.")
    ap.add_argument("--master-csv", type=Path, required=True, help="CUAD master_clauses.csv")
    ap.add_argument("--ingest-dir", type=Path, required=True, help="CUAD ingest dir (sampled PDFs)")
    ap.add_argument("--out", type=Path, required=True, help="output cuad.yaml")
    ap.add_argument("--per-category", type=int, default=2)
    a = ap.parse_args(argv)
    n = generate(master_csv=a.master_csv, ingest_dir=a.ingest_dir, out_path=a.out, per_category=a.per_category)
    print(f"wrote {n} ground-truth items -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run --project scripts/test_corpora --extra test pytest scripts/test_corpora/tests/test_generate_cuad.py -v`
Expected: PASS (2 tests).

- [ ] **Step 6: Lint + commit**

```bash
uv run --project scripts/test_corpora ruff check scripts/test_corpora/groundtruth/
uv run --project scripts/test_corpora ruff format scripts/test_corpora/groundtruth/ scripts/test_corpora/tests/test_generate_cuad.py
git add scripts/test_corpora/groundtruth/ scripts/test_corpora/tests/test_generate_cuad.py
git commit -m "feat(eval): CUAD ground-truth generator from master_clauses.csv"
```

---

## Task 3: Generate & curate the ground-truth set

Operational — produce the frozen `cuad.yaml` artifact. Requires the CUAD release extracted on disk (`master_clauses.csv` is under `<workdir>/cuad/extracted/CUAD_v1/`) and the 80 sampled PDFs in `<workdir>/cuad/ingest/`.

- [ ] **Step 1: Run the generator**

```bash
WD="$HOME/Library/Application Support/Harbor Clerk/test-corpora"
uv run --project scripts/test_corpora python -m scripts.test_corpora.groundtruth.generate_cuad \
  --master-csv "$WD/cuad/extracted/CUAD_v1/master_clauses.csv" \
  --ingest-dir "$WD/cuad/ingest" \
  --out scripts/test_corpora/groundtruth/cuad.yaml \
  --per-category 2
```
Expected: `wrote 12-15 ground-truth items`. If `master_clauses.csv` is not at that path, locate it under the extracted CUAD tree and adjust `--master-csv`.

- [ ] **Step 2: Human curation pass**

Open `scripts/test_corpora/groundtruth/cuad.yaml`. For each item confirm: the `question` reads naturally, `answer_key` is a sensible clause value (the crude parser may leave a fragment — trim it to the answer-bearing phrase), `gold_doc` matches a real PDF stem, and there are ~2-3 `negative` items. Delete any item whose `answer_key` is garbled beyond repair. Aim for 12-15 items total.

- [ ] **Step 3: Commit the frozen set**

```bash
git add scripts/test_corpora/groundtruth/cuad.yaml
git commit -m "feat(eval): frozen CUAD ground-truth set (12-15 items)"
```

---

## Task 4: Tool-transcript persistence in `claude_baseline.py`

The answer-eval needs the per-question tool-call transcript (for groundedness, and later tool-use auditing). `BaselineResult` currently saves only `tool_call_count`.

**Files:**
- Modify: `scripts/test_corpora/runner/claude_baseline.py`
- Test: `scripts/test_corpora/tests/test_baseline.py` (add one test)

- [ ] **Step 1: Write the failing test** — append to `tests/test_baseline.py`:

```python
def test_run_question_records_tool_transcript():
    """Each tool call is recorded in BaselineResult.tool_transcript."""
    from unittest.mock import MagicMock

    from scripts.test_corpora.runner.claude_baseline import BaselineGenerator

    fake = MagicMock()
    tool_block = MagicMock(type="tool_use", name="kb_search", id="t1", input={"query": "x"})
    fake.messages.create.side_effect = [
        MagicMock(content=[tool_block], stop_reason="tool_use"),
        MagicMock(content=[MagicMock(text="final answer", type="text")], stop_reason="end_turn"),
    ]
    mcp = MagicMock()
    mcp.list_tools.return_value = []
    mcp.call_tool.return_value = MagicMock(content=[MagicMock(text='{"doc_id": "d1"}')])

    gen = BaselineGenerator(client=fake, mcp_session=mcp)
    res = gen.run_question(question="q", question_id="q1", corpus="cuad")

    assert len(res.tool_transcript) == 1
    call = res.tool_transcript[0]
    assert call["tool"] == "kb_search"
    assert call["args"] == {"query": "x"}
    assert "doc_id" in call["result_summary"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project scripts/test_corpora --extra test pytest scripts/test_corpora/tests/test_baseline.py::test_run_question_records_tool_transcript -v`
Expected: FAIL — `BaselineResult` has no attribute `tool_transcript`.

- [ ] **Step 3: Add the field to `BaselineResult`** — in `claude_baseline.py`, add to the `@dataclasses.dataclass class BaselineResult` (after `tool_call_count`):

```python
    # Per-call record [{tool, args, result_summary}] in call order — for
    # groundedness scoring and tool-use auditing.
    tool_transcript: list[dict]
```

- [ ] **Step 4: Capture it in `run_question`** — in `run_question`: initialize `tool_transcript: list[dict] = []` next to `tool_call_count = 0`; inside the `for block in resp.content` loop, in the `tool_use` branch, immediately after `out = self._exec_tool(block.name, block.input)` add:

```python
                    tool_transcript.append({
                        "tool": block.name,
                        "args": dict(block.input),
                        "result_summary": out[:600],
                    })
```

Then pass `tool_transcript=tool_transcript` into the `BaselineResult(...)` constructor.

- [ ] **Step 5: Run the full baseline test file**

Run: `uv run --project scripts/test_corpora --extra test pytest scripts/test_corpora/tests/test_baseline.py -v`
Expected: PASS (the new test plus all existing).

- [ ] **Step 6: Lint + commit**

```bash
uv run --project scripts/test_corpora ruff check scripts/test_corpora/runner/claude_baseline.py
git add scripts/test_corpora/runner/claude_baseline.py scripts/test_corpora/tests/test_baseline.py
git commit -m "feat(eval): persist per-question tool-call transcript in baselines"
```

---

## Task 5: The answer-eval judge

`AnswerJudge` scores a model answer against the CUAD ground-truth key on three 0-5 dimensions. Mirrors `judge.py`'s structure (Anthropic call, JSON-from-text parsing) with the answer-eval rubric.

**Files:**
- Create: `scripts/test_corpora/runner/answer_judge.py`
- Test: `scripts/test_corpora/tests/test_answer_judge.py`

- [ ] **Step 1: Write the failing test**

```python
# scripts/test_corpora/tests/test_answer_judge.py
import json
from unittest.mock import MagicMock

from scripts.test_corpora.runner.answer_judge import AnswerJudge, AnswerVerdict


def _fake_client(payload: dict) -> MagicMock:
    c = MagicMock()
    c.messages.create.return_value = MagicMock(content=[MagicMock(text=json.dumps(payload))])
    return c


def test_judge_parses_scores():
    payload = {"correctness": 5, "groundedness": 4, "completeness": 5,
               "rationale": "states Delaware, cites the contract"}
    j = AnswerJudge(client=_fake_client(payload))
    v = j.judge_answer(question="What is the governing law?", model_answer="Delaware.",
                       cited="contract X, p2: governed by Delaware law",
                       answer_key="Delaware", qtype="lookup")
    assert isinstance(v, AnswerVerdict)
    assert (v.correctness, v.groundedness, v.completeness) == (5, 4, 5)
    assert v.rationale


def test_judge_handles_negative_item():
    payload = {"correctness": 5, "groundedness": 5, "completeness": 5, "rationale": "correctly declined"}
    j = AnswerJudge(client=_fake_client(payload))
    v = j.judge_answer(question="Does it contain an MFN clause?",
                       model_answer="No most-favored-nation clause is present.",
                       cited="contract Y full text", answer_key=None, qtype="negative")
    assert v.correctness == 5


def test_judge_tolerates_fenced_json():
    c = MagicMock()
    c.messages.create.return_value = MagicMock(content=[MagicMock(
        text='```json\n{"correctness": 0, "groundedness": 0, "completeness": 0, "rationale": "wrong"}\n```')])
    v = AnswerJudge(client=c).judge_answer(question="q", model_answer="a", cited="", answer_key="k", qtype="lookup")
    assert v.correctness == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project scripts/test_corpora --extra test pytest scripts/test_corpora/tests/test_answer_judge.py -v`
Expected: FAIL — `No module named 'scripts.test_corpora.runner.answer_judge'`.

- [ ] **Step 3: Write `answer_judge.py`**

```python
# scripts/test_corpora/runner/answer_judge.py
"""LLM-as-judge for the answer-eval. Scores a model answer against an external
ground-truth key (CUAD expert label) on correctness, groundedness, and
completeness. Independence is not a concern: the judge adjudicates against the
label, it is not itself the source of truth.
"""

from __future__ import annotations

import dataclasses
import json
import re

import anthropic

JUDGE_MODEL = "claude-sonnet-4-6"

_PROMPT = """You are scoring an answer produced by a document-search assistant.

QUESTION:
{question}

GROUND-TRUTH ANSWER KEY (expert-labeled; the source of truth):
{answer_key}

QUESTION TYPE: {qtype}
(For type "negative", the ground-truth answer key is "NONE" — the clause does
not exist in the document, and a correct answer says so.)

THE ASSISTANT'S ANSWER:
{model_answer}

THE PASSAGES THE ASSISTANT CITED:
{cited}

Score three dimensions, each an integer 0-5:
- correctness: does the answer agree with the ground-truth key? (negative type:
  full marks only if it correctly says the clause is absent.)
- groundedness: is every claim supported by a cited passage? 5 = fully
  grounded, 0 = key claims uncited or contradicted by the citation.
- completeness: does the answer cover what the key contains, without burying it
  in irrelevant text?

Reply with ONLY a JSON object:
{{"correctness": <0-5>, "groundedness": <0-5>, "completeness": <0-5>, "rationale": "<one sentence>"}}
"""


@dataclasses.dataclass
class AnswerVerdict:
    correctness: int
    groundedness: int
    completeness: int
    rationale: str


def _extract_json(text: str) -> dict:
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    raw = fence.group(1) if fence else text[text.find("{") : text.rfind("}") + 1]
    return json.loads(raw)


class AnswerJudge:
    def __init__(self, client: anthropic.Anthropic | None = None, model: str = JUDGE_MODEL):
        self._client = client or anthropic.Anthropic()
        self._model = model

    def judge_answer(
        self, *, question: str, model_answer: str, cited: str, answer_key: str | None, qtype: str
    ) -> AnswerVerdict:
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
Expected: PASS (3 tests).

- [ ] **Step 5: Lint + commit**

```bash
uv run --project scripts/test_corpora ruff check scripts/test_corpora/runner/answer_judge.py
uv run --project scripts/test_corpora ruff format scripts/test_corpora/runner/answer_judge.py scripts/test_corpora/tests/test_answer_judge.py
git add scripts/test_corpora/runner/answer_judge.py scripts/test_corpora/tests/test_answer_judge.py
git commit -m "feat(eval): AnswerJudge — score answers against the ground-truth key"
```

---

## Task 6: The answer-eval runner

`answer_eval.py` loads the ground-truth set, runs the model via MCP (reusing `BaselineGenerator`), persists model-keyed captures + verdicts (reused by default), and writes a labeled report. Mirrors `retrieval_eval.py` (`run` / `main_from_args` / `add_cli_args`).

**Files:**
- Create: `scripts/test_corpora/runner/answer_eval.py`
- Test: `scripts/test_corpora/tests/test_answer_eval.py`

- [ ] **Step 1: Write the failing test** — exercises the pure helpers with no network:

```python
# scripts/test_corpora/tests/test_answer_eval.py
from pathlib import Path

import yaml

from scripts.test_corpora.runner.answer_eval import aggregate, load_groundtruth
from scripts.test_corpora.runner.answer_judge import AnswerVerdict


def test_load_groundtruth(tmp_path: Path):
    gt = tmp_path / "cuad.yaml"
    gt.write_text(yaml.safe_dump({"corpus": "cuad", "items": [
        {"id": "g1", "question": "q1", "clause_category": "Governing Law",
         "gold_doc": "AcmeCo", "answer_key": "Delaware", "type": "lookup"},
        {"id": "g2", "question": "q2", "clause_category": "Most Favored Nation",
         "gold_doc": "BetaCo", "answer_key": None, "type": "negative"},
    ]}))
    items = load_groundtruth(gt)
    assert [i.id for i in items] == ["g1", "g2"]
    assert items[0].answer_key == "Delaware"
    assert items[1].answer_key is None and items[1].type == "negative"


def test_aggregate_means_overall_and_by_type():
    rows = [
        ("g1", "lookup", AnswerVerdict(5, 4, 5, "")),
        ("g2", "lookup", AnswerVerdict(3, 2, 3, "")),
        ("g3", "negative", AnswerVerdict(5, 5, 5, "")),
    ]
    summary = aggregate(rows)
    assert summary["overall"]["n"] == 3
    assert summary["overall"]["correctness"] == 13 / 3
    assert summary["by_type"]["lookup"]["n"] == 2
    assert summary["by_type"]["negative"]["correctness"] == 5.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project scripts/test_corpora --extra test pytest scripts/test_corpora/tests/test_answer_eval.py -v`
Expected: FAIL — `No module named 'scripts.test_corpora.runner.answer_eval'`.

- [ ] **Step 3: Write `answer_eval.py`**

```python
# scripts/test_corpora/runner/answer_eval.py
"""--mode answer-eval — run a frontier model through HC's MCP over a
ground-truth set and score answers for correctness / groundedness /
completeness against expert labels.

Mirrors retrieval_eval.py. Artifacts are model-keyed and reused by default:
  <workdir>/answer-eval/captures/<corpus>/<model>/<id>.json   (model+MCP run)
  <workdir>/answer-eval/verdicts/<corpus>/<model>/<id>.json   (judge verdict)
  <workdir>/answer-eval/reports/<label>/summary.json          (per-run report)
Regeneration is explicit: --refresh re-runs captures, --rejudge re-runs the
judge. The ground-truth set itself is a frozen, version-controlled file.

scope_folder_ids classification (Task 1): <FILL IN: pre-ranking | post-ranking>.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
from collections.abc import Callable
from pathlib import Path

import yaml

from scripts.test_corpora.runner.answer_judge import AnswerJudge, AnswerVerdict

log = logging.getLogger("answer_eval")

GROUNDTRUTH_DIR = Path(__file__).resolve().parents[1] / "groundtruth"


@dataclasses.dataclass(frozen=True)
class GTItem:
    id: str
    question: str
    clause_category: str
    gold_doc: str
    answer_key: str | None
    type: str


def load_groundtruth(path: Path) -> list[GTItem]:
    data = yaml.safe_load(path.read_text())
    return [
        GTItem(
            id=i["id"], question=i["question"], clause_category=i["clause_category"],
            gold_doc=i["gold_doc"], answer_key=i.get("answer_key"), type=i["type"],
        )
        for i in data["items"]
    ]


def aggregate(rows: list[tuple[str, str, AnswerVerdict]]) -> dict:
    """rows = [(item_id, type, verdict)]. Means overall and by type."""
    def _means(group: list[AnswerVerdict]) -> dict:
        n = len(group)
        if n == 0:
            return {"n": 0}
        return {
            "n": n,
            "correctness": sum(v.correctness for v in group) / n,
            "groundedness": sum(v.groundedness for v in group) / n,
            "completeness": sum(v.completeness for v in group) / n,
        }

    by_type: dict[str, list[AnswerVerdict]] = {}
    for _id, qtype, v in rows:
        by_type.setdefault(qtype, []).append(v)
    return {
        "overall": _means([v for _, _, v in rows]),
        "by_type": {t: _means(g) for t, g in by_type.items()},
    }


def _cited_text(capture: dict) -> str:
    """Compact rendering of the cited evidence for the judge."""
    titles = capture.get("cited_doc_titles") or []
    transcript = capture.get("tool_transcript") or []
    lines = [f"cited docs: {', '.join(t for t in titles if t)}"]
    for call in transcript:
        lines.append(f"[{call.get('tool')}] {str(call.get('result_summary'))[:400]}")
    return "\n".join(lines)


def run(
    *,
    workdir: Path,
    corpus: str,
    model: str,
    label: str,
    api_base: str,
    refresh: bool,
    rejudge: bool,
    insecure: bool,
    groundtruth_path: Path | None = None,
    capture_fn: Callable[[GTItem], dict] | None = None,
    judge: AnswerJudge | None = None,
) -> int:
    """Returns an exit code (0 = success). `capture_fn` and `judge` are
    injectable for tests; in production they default to a live model+MCP run
    and a live AnswerJudge."""
    gt_path = groundtruth_path or (GROUNDTRUTH_DIR / f"{corpus}.yaml")
    if not gt_path.exists():
        log.error("ground-truth set not found: %s", gt_path)
        return 2
    items = load_groundtruth(gt_path)
    log.info("loaded %d ground-truth items from %s", len(items), gt_path)

    cap_dir = workdir / "answer-eval" / "captures" / corpus / model
    ver_dir = workdir / "answer-eval" / "verdicts" / corpus / model
    cap_dir.mkdir(parents=True, exist_ok=True)
    ver_dir.mkdir(parents=True, exist_ok=True)

    if capture_fn is None:
        capture_fn = _live_capture_fn(api_base=api_base, model=model, insecure=insecure)
    if judge is None:
        judge = AnswerJudge()

    rows: list[tuple[str, str, AnswerVerdict]] = []
    for item in items:
        cap_path = cap_dir / f"{item.id}.json"
        if cap_path.exists() and not refresh:
            capture = json.loads(cap_path.read_text())
        else:
            log.info("capturing %s via model+MCP", item.id)
            capture = capture_fn(item)
            cap_path.write_text(json.dumps(capture, indent=2))

        ver_path = ver_dir / f"{item.id}.json"
        if ver_path.exists() and not rejudge and not refresh:
            verdict = AnswerVerdict(**json.loads(ver_path.read_text()))
        else:
            verdict = judge.judge_answer(
                question=item.question, model_answer=capture.get("answer", ""),
                cited=_cited_text(capture), answer_key=item.answer_key, qtype=item.type,
            )
            ver_path.write_text(json.dumps(dataclasses.asdict(verdict), indent=2))
        log.info("  %s correctness=%d grounded=%d complete=%d",
                 item.id, verdict.correctness, verdict.groundedness, verdict.completeness)
        rows.append((item.id, item.type, verdict))

    summary = aggregate(rows)
    report_dir = workdir / "answer-eval" / "reports" / label
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    (report_dir / "detail.json").write_text(json.dumps(
        [{"id": i, "type": t, **dataclasses.asdict(v)} for i, t, v in rows], indent=2))
    o = summary["overall"]
    log.info("OVERALL n=%d correctness=%.2f groundedness=%.2f completeness=%.2f",
             o["n"], o["correctness"], o["groundedness"], o["completeness"])
    return 0


def _live_capture_fn(*, api_base: str, model: str, insecure: bool) -> Callable[[GTItem], dict]:
    """Build the production capture function: a model+MCP run per item.

    Reuses the phase-1 machinery — HarborClerkClient for auth, SyncMcpSession
    for the MCP tool calls, BaselineGenerator for the agentic loop. The
    HarborClerkClient MUST be authenticated with a corpus-scoped API key so the
    MCP search is restricted to `corpus`.
    """
    import anthropic

    from scripts.test_corpora.runner.claude_baseline import BaselineGenerator
    from scripts.test_corpora.runner.client import HarborClerkClient, SyncMcpSession

    hc = HarborClerkClient(api_base, verify=not insecure)
    hc.login_from_env()  # uses HC_API_KEY or HC_USERNAME/HC_PASSWORD
    mcp = SyncMcpSession(url=f"{api_base}/mcp/mcp", bearer=hc.get_bearer_token())
    anthro = anthropic.Anthropic()

    def capture(item: GTItem) -> dict:
        gen = BaselineGenerator(client=anthro, mcp_session=mcp, model=model)
        res = gen.run_question(question=item.question, question_id=item.id, corpus="cuad")
        return dataclasses.asdict(res)

    return capture


def add_cli_args(p: argparse.ArgumentParser) -> None:
    """Register answer-eval-only flags. --label / --run-id / --workdir /
    --api-base and --corpora / --models already exist on the sweep parser."""
    g = p.add_argument_group("answer-eval (only with --mode answer-eval)")
    g.add_argument("--refresh", action="store_true", help="re-run model+MCP captures even if present")
    g.add_argument("--rejudge", action="store_true", help="re-run the judge even if verdicts are present")


def main_from_args(args: argparse.Namespace) -> int:
    corpus = (args.corpora.split(",")[0].strip() if args.corpora else "cuad")
    model = (args.models.split(",")[0].strip() if args.models else "claude-sonnet-4-6")
    if not args.label:
        log.error("--label is required for --mode answer-eval")
        return 2
    return run(
        workdir=Path(args.workdir), corpus=corpus, model=model, label=args.label,
        api_base=args.api_base, refresh=args.refresh, rejudge=args.rejudge,
        insecure=args.insecure,
    )
```

> **Implementation note for Step 3:** `HarborClerkClient.login_from_env()` and `SyncMcpSession(url=, bearer=)` are how the runner authenticates and opens the MCP session. Open `runner/client.py` and the `_phase1_baseline` call site in `sweep.py` and match the **actual** method names/signatures — if `login_from_env` does not exist, use the same login the sweep uses (`hc.login(email, password)` from `HC_USERNAME`/`HC_PASSWORD`, plus reading `HC_API_KEY` for the scoped key). The behavior must hold: authenticate with the corpus-scoped key, open the MCP session with the bearer token.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --project scripts/test_corpora --extra test pytest scripts/test_corpora/tests/test_answer_eval.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Fill in the Task 1 finding** — replace `<FILL IN: pre-ranking | post-ranking>` in the module docstring with the Task 1 result.

- [ ] **Step 6: Lint + commit**

```bash
uv run --project scripts/test_corpora ruff check scripts/test_corpora/runner/answer_eval.py
uv run --project scripts/test_corpora ruff format scripts/test_corpora/runner/answer_eval.py scripts/test_corpora/tests/test_answer_eval.py
git add scripts/test_corpora/runner/answer_eval.py scripts/test_corpora/tests/test_answer_eval.py
git commit -m "feat(eval): answer-eval runner — capture, judge, report (model-keyed, reuse-by-default)"
```

---

## Task 7: Wire `--mode answer-eval` into the sweep

**Files:** Modify `scripts/test_corpora/runner/sweep.py`. Test: add to `scripts/test_corpora/tests/test_sweep_ingest.py`.

- [ ] **Step 1: Write the failing test** — append to `tests/test_sweep_ingest.py`:

```python
def test_mode_answer_eval_is_accepted_and_dispatches(monkeypatch):
    """--mode answer-eval parses and dispatches to answer_eval.main_from_args."""
    from scripts.test_corpora.runner import answer_eval, sweep

    called = {}
    monkeypatch.setattr(answer_eval, "main_from_args", lambda args: called.setdefault("ok", args) or 0)
    parser = sweep.make_parser()
    args = parser.parse_args([
        "--run-id", "t", "--mode", "answer-eval", "--label", "lbl",
        "--corpora", "cuad", "--models", "claude-sonnet-4-6",
    ])
    rc = sweep.main(args)
    assert rc == 0
    assert called["ok"].label == "lbl"
```

If `sweep.main` does not take a pre-parsed `args` (check its real signature), adjust the call to match — e.g. `sweep.main(["--run-id", "t", "--mode", "answer-eval", "--label", "lbl", "--corpora", "cuad"])`.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project scripts/test_corpora --extra test pytest scripts/test_corpora/tests/test_sweep_ingest.py::test_mode_answer_eval_is_accepted_and_dispatches -v`
Expected: FAIL — argparse rejects `answer-eval` (`--mode` choices are `["retrieval-eval"]`).

- [ ] **Step 3: Register the mode** — in `sweep.py` `make_parser()`, change the `--mode` argument's `choices` from `["retrieval-eval"]` to `["retrieval-eval", "answer-eval"]`, and immediately after the existing `retrieval_eval.add_cli_args(p)` line add:

```python
    from scripts.test_corpora.runner import answer_eval
    answer_eval.add_cli_args(p)
```

- [ ] **Step 4: Add the dispatch branch** — in `sweep.py` `main()`, immediately after the existing `if args.mode == "retrieval-eval":` block, add:

```python
    if args.mode == "answer-eval":
        from scripts.test_corpora.runner import answer_eval
        return answer_eval.main_from_args(args)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run --project scripts/test_corpora --extra test pytest scripts/test_corpora/tests/test_sweep_ingest.py -v`
Expected: PASS.

- [ ] **Step 6: Run the full harness test suite** (regression guard)

Run: `uv run --project scripts/test_corpora --extra test pytest scripts/test_corpora/tests/ -q`
Expected: PASS (all tests).

- [ ] **Step 7: Lint + commit**

```bash
uv run --project scripts/test_corpora ruff check scripts/test_corpora/runner/sweep.py
git add scripts/test_corpora/runner/sweep.py scripts/test_corpora/tests/test_sweep_ingest.py
git commit -m "feat(eval): wire --mode answer-eval into the sweep"
```

---

## Task 8: Eval bed, de-risk probe, and first real run

Operational. Stand up the eval bed, run the de-risk probe, run the first CUAD answer-eval.

- [ ] **Step 1: Ingest the three corpora additively.** In HC's Folders UI (`/folders`), confirm Enron is a watched folder, then add CUAD and synthetic as two more watched folders pointing at `<workdir>/cuad/ingest` and `<workdir>/synthetic/ingest`. Do NOT use the sweep's destructive `_ingest_corpus`. Wait for the pipeline to drain. Record the three folder IDs.

- [ ] **Step 2: Mint three folder-scoped, read-only API keys** — one per corpus — via the API Keys admin UI, each with `scope_folder_ids` set to that corpus's folder ID. Record the CUAD-scoped key.

- [ ] **Step 3: Empirically confirm scoping** (closes Task 1). With all three corpora resident, run two `kb_search` calls through the CUAD-scoped key — one for a CUAD-specific term, one for an Enron-specific term — and confirm: only CUAD docs are returned, and the CUAD-term search returns a full result set (not truncated). If results are truncated or leak other corpora, the filter is post-ranking -> fall back: load CUAD alone for this run and file a follow-up to make HC's folder filter pre-ranking.

- [ ] **Step 4: Run the de-risk probe** — point `AnswerJudge` at the existing 16 Enron baselines:

```bash
uv run --project scripts/test_corpora python - <<'PY'
import glob, json, os
from scripts.test_corpora.runner.answer_judge import AnswerJudge
j = AnswerJudge()
base = os.path.expanduser(
    "~/Library/Application Support/Harbor Clerk/test-corpora/results/sanity-2026-05-22/baselines/enron")
for f in sorted(glob.glob(base + "/*.json")):
    d = json.load(open(f))
    v = j.judge_answer(question=d["question"], model_answer=d["answer"],
                       cited="cited docs: " + ", ".join(d.get("cited_doc_titles") or []),
                       answer_key="(no key - groundedness only)", qtype="lookup")
    print(d["question_id"], "groundedness=", v.groundedness)
PY
```
Expected: the judge runs cleanly over all 16 and prints groundedness scores. This validates `AnswerJudge` end-to-end (the score is partial — citation presence only — since these baselines carry no passages).

- [ ] **Step 5: Run the first CUAD answer-eval**

```bash
WD="$HOME/Library/Application Support/Harbor Clerk/test-corpora"
HC_API_KEY="<cuad-scoped key>" \
uv run --project scripts/test_corpora python -m scripts.test_corpora.runner.sweep \
  --run-id answer-eval --mode answer-eval --corpora cuad --models claude-sonnet-4-6 \
  --label cuad-phase1 --workdir "$WD" --api-base http://localhost:8100
```
Expected: a capture + verdict per ground-truth item; an `OVERALL n=… correctness=… groundedness=… completeness=…` line; `answer-eval/reports/cuad-phase1/summary.json` written.

- [ ] **Step 6: Sanity-check the report.** Open `summary.json` + `detail.json`. Confirm correctness/groundedness/completeness are populated, negatives scored, judge rationales sensible. This is the phase-1 deliverable — the first ground-truth-based answer eval.

---

## Self-Review

**Spec coverage:** §1 goal -> all tasks. §2 scope (CUAD/Sonnet/clause-extraction) -> Tasks 2,3,8. §3 CUAD -> Task 2. §4 ground-truth-independence -> Task 2 (CSV, no MCP). §5 components -> Tasks 2,4,5,6 + claude_baseline (Task 4). §6 eval bed + scope verification -> Tasks 1,8. §7 persistence/regeneration discipline -> Task 6 (model-keyed paths, reuse-by-default, `--refresh`/`--rejudge`; frozen `cuad.yaml` Task 3). §8 ground-truth set -> Tasks 2,3. §9 eval run + transcript persistence -> Tasks 4,6. §10 scoring/judge -> Task 5. §11 harness integration -> Task 7. §12 de-risk probe -> Task 8 Step 4. §13 output -> Task 6 (`summary.json`/`detail.json` under `reports/<label>/`). §14 testing -> tests in Tasks 2,4,5,6,7. §15 out-of-scope — respected. §16 open questions — `per_category` and judge wording settled in code; `--corpora`/`--models` reused rather than new singular flags (documented in `add_cli_args`).

**Placeholder scan:** one deliberate fill-in — `<FILL IN: pre-ranking | post-ranking>` in `answer_eval.py`'s docstring, resolved by Task 6 Step 5 from Task 1's finding. The Step-3 implementation note on `client.py` method names is an explicit "verify against the real file" instruction, not a vague placeholder.

**Type consistency:** `AnswerVerdict` (correctness/groundedness/completeness:int, rationale:str) and `AnswerJudge.judge_answer(question=, model_answer=, cited=, answer_key=, qtype=)` are used identically in Tasks 5, 6, 8. `GTItem` fields match `cuad.yaml`'s keys (Task 2) and `load_groundtruth` (Task 6). The `tool_transcript` entry shape `{tool, args, result_summary}` is consistent between Task 4 (writer) and Task 6 `_cited_text` (reader).

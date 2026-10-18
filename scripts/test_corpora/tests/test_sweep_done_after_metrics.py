"""A unit is finished when its metrics row exists, and not before (#683).

`bench-20260921-0041`: state.json said `cuad/gemma4-26b-a4b/cuad-ask-1` was done, error None, and metrics.csv had
no row for it. The status was saved first and the row computed after, and the spaCy model the entity metric loads
was not installed. `--resume` counted 63 pending units of 64 and moved on; nothing in the run said a number was
missing, and the sweep itself ended with a traceback and the interpreter's exit code 1.
"""

from __future__ import annotations

import contextlib
import csv
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from scripts.test_corpora.runner import sweep

FOLDER_ID = "3f0e7c0a-0000-4000-8000-000000000001"
ANSWERED = {"result": {"status": "completed", "answer": "The agreement is governed by Nevada law.", "citations": []}}


def _questions_dir(tmp_path: Path, text: str = "What is the governing law of the Cybergy affiliate agreement?") -> Path:
    qdir = tmp_path / "questions"
    qdir.mkdir()
    (qdir / "cuad.yaml").write_text(yaml.safe_dump({"research": [], "ask": [{"id": "cuad-ask-1", "text": text}]}))
    return qdir


def _instance() -> MagicMock:
    """An instance that lists no models (so nothing is skipped), with the model already loaded."""
    hc = MagicMock()
    hc.list_models.return_value = []
    hc.summarize_backlog.return_value = 0
    hc.folder_id_for_corpus.return_value = FOLDER_ID
    return hc


def _sweep(tmp_path: Path, qdir: Path, *extra: str) -> int:
    base = ["--run-id", "r1", "--workdir", str(tmp_path), "--phases", "4", "--corpora", "cuad", "--models", "qwen3-8b"]
    flags = ["--questions-dir", str(qdir), "--no-ingest", "--skip-canary", "--no-judge", "--no-hc-logs"]
    return sweep.main([*base, *flags, *extra])


def _rows(run_dir: Path) -> list[dict]:
    with (run_dir / "metrics.csv").open(newline="") as f:
        return list(csv.DictReader(f))


def _unit(run_dir: Path) -> dict:
    (unit,) = json.loads((run_dir / "state.json").read_text())["units"]
    return unit


@contextlib.contextmanager
def _patched(*, answer: dict, entity_overlap):
    """No instance, no judge, no cloud client; the local model's answer and the entity metric are given."""
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(sweep, "HarborClerkClient", return_value=_instance()))
        stack.enter_context(patch.object(sweep, "JudgeClient"))
        stack.enter_context(patch.object(sweep.spend, "anthropic_client", lambda kind: MagicMock()))
        stack.enter_context(patch.object(sweep, "_run_local", return_value=answer))
        stack.enter_context(patch.object(sweep, "entity_overlap", entity_overlap))
        yield


def test_a_crash_between_the_answer_and_its_row_leaves_the_unit_unfinished_and_exits_with_the_documented_code(
    tmp_path, caplog
):
    qdir = _questions_dir(tmp_path)
    run_dir = tmp_path / "results" / "r1"
    (run_dir / "baselines" / "cuad").mkdir(parents=True)
    (run_dir / "baselines" / "cuad" / "cuad-ask-1.json").write_text(
        json.dumps({"answer": "Nevada.", "cited_doc_ids": []})
    )

    absent = OSError(
        "[E050] Can't find model 'en_core_web_sm'. It doesn't seem to be a Python package or a valid path."
    )
    with caplog.at_level("ERROR"), _patched(answer=ANSWERED, entity_overlap=MagicMock(side_effect=absent)):
        assert _sweep(tmp_path, qdir) == sweep.EXIT_UNCAUGHT
    assert "uncaught exception" in caplog.text and "E050" in caplog.text, "the traceback is in the log"
    assert _unit(run_dir)["status"] != "done", "no row was written, so the unit is not finished"
    assert _rows(run_dir) == []

    # The next start runs it again; the row is written, and only then is the unit done.
    with _patched(answer=ANSWERED, entity_overlap=lambda *a, **k: 0.5):
        assert _sweep(tmp_path, qdir, "--resume") == 0
    assert _unit(run_dir)["status"] == "done" and _unit(run_dir)["error"] is None
    assert [(r["question_id"], r["status"], r["entity_overlap"]) for r in _rows(run_dir)] == [
        ("cuad-ask-1", "done", "0.500")
    ]


def test_a_unit_that_gets_no_row_is_still_saved(tmp_path):
    """The one path that finishes a unit without a row: a question still carrying a template placeholder is
    never asked. It is the last unit here, so no later unit's save can cover for it."""
    qdir = _questions_dir(tmp_path, text="What is the governing law of {{contract_a}}?")
    run_dir = tmp_path / "results" / "r1"
    with _patched(answer={}, entity_overlap=lambda *a, **k: 0.0):
        assert _sweep(tmp_path, qdir) == 0
    unit = _unit(run_dir)
    assert unit["status"] == "error" and unit["error"] == "unfilled placeholder: {{contract_a}}"

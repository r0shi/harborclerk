"""Unit tests for scripts/test_corpora/runner/rerun_pr_j.py.

The provider call is mocked — we don't make real LLM calls in CI.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock


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
    """Stub enron.yaml with the two qids. Uses the real-YAML schema
    (items: + type:) — same convention sample_pr_j tests for real coverage."""
    import yaml

    gt = {
        "corpus": "enron",
        "items": [
            {"id": "n-1", "type": "negative", "question": "Q1?", "answer_key": None},
            {
                "id": "f-1",
                "type": "find",
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


def test_rerun_skips_qids_missing_from_groundtruth(tmp_path):
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
    (tmp_path / "groundtruth" / "enron.yaml").write_text("corpus: enron\nitems: []\n")

    def _fake_factory(model, *, mcp_session):
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

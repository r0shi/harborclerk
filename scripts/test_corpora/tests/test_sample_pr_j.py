"""Unit tests for scripts/test_corpora/runner/sample_pr_j.py."""

from __future__ import annotations

import json
from pathlib import Path


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
    from scripts.test_corpora.runner.sample_pr_j import write_populations

    populations = {"negatives_hedged": [], "finds_short": []}
    out_path = tmp_path / "pr_j_populations.json"
    write_populations(populations, out_path, model="claude-sonnet-4-6", corpora=("enron",))

    loaded = json.loads(out_path.read_text())
    assert "model" in loaded
    assert "corpora" in loaded
    assert "generated_at" in loaded
    assert loaded["negatives_hedged"] == []
    assert loaded["finds_short"] == []

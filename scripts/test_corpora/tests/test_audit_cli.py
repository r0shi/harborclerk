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
    render_markdown,
)


def _write_capture(captures_dir: Path, qid: str, tools: list[str]) -> None:
    captures_dir.mkdir(parents=True, exist_ok=True)
    (captures_dir / f"{qid}.json").write_text(
        json.dumps(
            {
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
        )
    )


def _write_verdict(verdicts_dir: Path, qid: str, correctness: int = 5) -> None:
    verdicts_dir.mkdir(parents=True, exist_ok=True)
    (verdicts_dir / f"{qid}.json").write_text(
        json.dumps(
            {
                "correctness": correctness,
                "groundedness": 5,
                "completeness": 5,
                "rationale": "ok",
                "source": {},
            }
        )
    )


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
        {
            "id": "q1",
            "type": "lookup",
            "correctness": 3,
            "groundedness": 4,
            "completeness": 5,
            "rationale": "x",
            "source": {},
        },
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

    rc = main(
        [
            "--workdir",
            str(tmp_path),
            "--label",
            "smoke",
        ]
    )
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
            "low_correctness_low_tool_use": [
                {"qid": "q-fail", "tools_used": ["kb_search"], "correctness": 1, "title_fragment": "what does X say?"}
            ],
            "earliest_latest_questions_no_by_date_tool": [],
            "ambiguous_id_questions_no_verify_tool": [],
        },
        "citation_hygiene": {
            "grounded_count": 4,
            "total": 5,
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
        "label": "smoke",
        "corpus": "synthetic",
        "baseline_model": "m",
        "generated_at": "...",
        "tool_use": {
            "total_captures": 0,
            "tool_call_distribution": {},
            "tool_call_counts_per_tool": {},
            "captures_by_tool_count": {},
        },
        "failure_correlation": {
            "low_correctness_low_tool_use": [],
            "earliest_latest_questions_no_by_date_tool": [],
            "ambiguous_id_questions_no_verify_tool": [],
        },
        "citation_hygiene": {"grounded_count": 0, "total": 0, "no_citations": [], "fabricated_citations": []},
        "cross_judge": None,
    }
    md = render_markdown(audit)
    assert "Cross-judge" not in md


def test_render_markdown_cross_judge_section_shows_deltas_and_disagreements():
    audit = {
        "label": "smoke",
        "corpus": "synthetic",
        "baseline_model": "claude-sonnet-4-6",
        "generated_at": "...",
        "tool_use": {
            "total_captures": 0,
            "tool_call_distribution": {},
            "tool_call_counts_per_tool": {},
            "captures_by_tool_count": {},
        },
        "failure_correlation": {
            "low_correctness_low_tool_use": [],
            "earliest_latest_questions_no_by_date_tool": [],
            "ambiguous_id_questions_no_verify_tool": [],
        },
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
                {
                    "qid": "q-disagree",
                    "judge_a": {"correctness": 4, "groundedness": 5, "completeness": 3, "rationale": "rat-a"},
                    "judge_b": {"correctness": 2, "groundedness": 5, "completeness": 2, "rationale": "rat-b"},
                    "max_delta": 2,
                    "delta_dim": "correctness",
                },
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

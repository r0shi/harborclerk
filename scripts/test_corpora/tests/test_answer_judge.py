# scripts/test_corpora/tests/test_answer_judge.py
import json
from unittest.mock import MagicMock

import pytest

from scripts.test_corpora.runner.answer_judge import AnswerJudge, AnswerVerdict, _extract_json


def _fake_client(payload: dict) -> MagicMock:
    c = MagicMock()
    c.messages.create.return_value = MagicMock(content=[MagicMock(text=json.dumps(payload))])
    return c


def test_judge_parses_scores():
    payload = {
        "correctness": 5,
        "groundedness": 4,
        "completeness": 5,
        "rationale": "states Delaware, cites the contract",
    }
    j = AnswerJudge(client=_fake_client(payload))
    v = j.judge_answer(
        question="What is the governing law?",
        model_answer="Delaware.",
        cited="contract X, p2: governed by Delaware law",
        answer_key="Delaware",
        qtype="lookup",
    )
    assert isinstance(v, AnswerVerdict)
    assert (v.correctness, v.groundedness, v.completeness) == (5, 4, 5)
    assert v.rationale


def test_judge_handles_negative_item():
    payload = {"correctness": 5, "groundedness": 5, "completeness": 5, "rationale": "correctly declined"}
    j = AnswerJudge(client=_fake_client(payload))
    v = j.judge_answer(
        question="Does it contain an MFN clause?",
        model_answer="No most-favored-nation clause is present.",
        cited="contract Y full text",
        answer_key=None,
        qtype="negative",
    )
    assert v.correctness == 5


def test_judge_tolerates_fenced_json():
    c = MagicMock()
    c.messages.create.return_value = MagicMock(
        content=[
            MagicMock(
                text='```json\n{"correctness": 0, "groundedness": 0, "completeness": 0, "rationale": "wrong"}\n```'
            )
        ]
    )
    v = AnswerJudge(client=c).judge_answer(question="q", model_answer="a", cited="", answer_key="k", qtype="lookup")
    assert v.correctness == 0


def test_extract_json_rejects_response_without_json():
    """A judge reply with no JSON object raises a clear error, not a JSONDecodeError."""
    with pytest.raises(ValueError, match="no JSON object"):
        _extract_json("the model refused and returned only prose")

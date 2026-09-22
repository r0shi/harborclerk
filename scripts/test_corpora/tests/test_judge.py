import json
from unittest.mock import MagicMock

import pytest

from scripts.test_corpora.runner.judge import JudgeClient, JudgeVerdict


def test_judge_parses_structured_response():
    fake_anthropic = MagicMock()
    fake_anthropic.messages.create.return_value.content = [
        MagicMock(
            text=json.dumps(
                {
                    "claim_recall": 4,
                    "claim_precision": 5,
                    "entity_recall": 4,
                    "completeness": 4,
                    "answers_question": 5,
                    "missing_facts": ["one fact"],
                    "extra_facts": [],
                    "contradictions": [],
                    "verdict": "pass",
                }
            )
        )
    ]

    j = JudgeClient(client=fake_anthropic)
    v = j.judge(question="Q?", baseline="B", model_answer="M")
    assert isinstance(v, JudgeVerdict)
    assert v.verdict == "pass"
    assert v.claim_recall == 4
    assert v.missing_facts == ["one fact"]


def test_judge_handles_extra_text_around_json():
    fake_anthropic = MagicMock()
    fake_anthropic.messages.create.return_value.content = [
        MagicMock(
            text="Here is the verdict:\n```json\n"
            + json.dumps(
                {
                    "claim_recall": 3,
                    "claim_precision": 3,
                    "entity_recall": 3,
                    "completeness": 3,
                    "answers_question": 3,
                    "missing_facts": [],
                    "extra_facts": [],
                    "contradictions": [],
                    "verdict": "marginal",
                }
            )
            + "\n```"
        )
    ]

    j = JudgeClient(client=fake_anthropic)
    v = j.judge(question="Q?", baseline="B", model_answer="M")
    assert v.verdict == "marginal"


def test_the_verdict_follows_answers_question_and_the_prompt_says_so():
    """The rubric graded coverage of the baseline and named no rule for the verdict; a correct short answer came out
    "marginal" (docs/reports/2026-09-22-rubric-test.md). The rule is one line, and it is what fixed it."""
    from scripts.test_corpora.runner.judge import JUDGE_PROMPT

    assert "The verdict follows answers_question alone, not completeness." in JUDGE_PROMPT
    assert '"answers_question": int,' in JUDGE_PROMPT and "Do not penalise" in JUDGE_PROMPT
    assert JUDGE_PROMPT.index("- answers_question:") > JUDGE_PROMPT.index("- completeness:")


def test_answers_question_is_read_from_the_judge_not_defaulted():
    fake = MagicMock()
    fake.messages.create.return_value.content = [
        MagicMock(
            text=json.dumps(
                {
                    "claim_recall": 1,
                    "claim_precision": 1,
                    "entity_recall": 1,
                    "completeness": 1,
                    "answers_question": 5,
                    "verdict": "pass",
                }
            )
        )
    ]
    v = JudgeClient(client=fake).judge(question="Q?", baseline="B", model_answer="M")
    assert (v.answers_question, v.completeness) == (5, 1), (
        "a short right answer: low coverage, full marks for the answer"
    )
    fake.messages.create.return_value.content = [
        MagicMock(
            text='{"claim_recall": 1, "claim_precision": 1, "entity_recall": 1, "completeness": 1, "verdict": "fail"}'
        )
    ]
    with pytest.raises(KeyError):
        JudgeClient(client=fake).judge(question="Q?", baseline="B", model_answer="M")

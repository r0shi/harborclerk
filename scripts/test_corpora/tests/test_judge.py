import json
from unittest.mock import MagicMock

from scripts.test_corpora.runner.judge import JudgeClient, JudgeVerdict


def test_judge_parses_structured_response():
    fake_anthropic = MagicMock()
    fake_anthropic.messages.create.return_value.content = [
        MagicMock(text=json.dumps({
            "claim_recall": 4,
            "claim_precision": 5,
            "entity_recall": 4,
            "completeness": 4,
            "missing_facts": ["one fact"],
            "extra_facts": [],
            "contradictions": [],
            "verdict": "pass",
        }))
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
        MagicMock(text="Here is the verdict:\n```json\n" + json.dumps({
            "claim_recall": 3, "claim_precision": 3, "entity_recall": 3, "completeness": 3,
            "missing_facts": [], "extra_facts": [], "contradictions": [], "verdict": "marginal",
        }) + "\n```")
    ]

    j = JudgeClient(client=fake_anthropic)
    v = j.judge(question="Q?", baseline="B", model_answer="M")
    assert v.verdict == "marginal"

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

    j = JudgeClient(client=fake_anthropic, model="claude-sonnet-4-6")
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

    j = JudgeClient(client=fake_anthropic, model="claude-sonnet-4-6")
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
    v = JudgeClient(client=fake, model="claude-sonnet-4-6").judge(question="Q?", baseline="B", model_answer="M")
    assert (v.answers_question, v.completeness) == (5, 1), (
        "a short right answer: low coverage, full marks for the answer"
    )
    fake.messages.create.return_value.content = [
        MagicMock(
            text='{"claim_recall": 1, "claim_precision": 1, "entity_recall": 1, "completeness": 1, "verdict": "fail"}'
        )
    ]
    with pytest.raises(KeyError):
        JudgeClient(client=fake, model="claude-sonnet-4-6").judge(question="Q?", baseline="B", model_answer="M")


def test_the_default_judge_is_an_openai_model_called_through_the_openai_client():
    from scripts.test_corpora import conftest as cfg
    from scripts.test_corpora.runner import spend

    assert cfg.JUDGE_MODEL == "gpt-5.6-luna"
    assert cfg.JUDGE_MODEL in spend.load_config().prices, "the default judge must be priced, or every run is refused"
    fake = MagicMock()
    fake.chat.completions.create.return_value.choices = [
        MagicMock(
            message=MagicMock(
                content='{"claim_recall": 5, "claim_precision": 5, "entity_recall": 5, "completeness": 5, "answers_question": 5, "verdict": "pass"}'
            )
        )
    ]
    v = JudgeClient(client=fake).judge(question="Q?", baseline="B", model_answer="M")
    assert v.verdict == "pass" and fake.messages.create.call_count == 0
    kw = fake.chat.completions.create.call_args.kwargs
    assert (
        kw["model"] == "gpt-5.6-luna" and kw["max_completion_tokens"] >= 1500 and "Q?" in kw["messages"][0]["content"]
    )


def test_a_claude_judge_that_thinks_by_default_is_run_with_thinking_off():
    fake = MagicMock()
    fake.messages.create.return_value.content = [
        MagicMock(
            type="text",
            text='{"claim_recall": 5, "claim_precision": 5, "entity_recall": 5, "completeness": 5, "answers_question": 5, "verdict": "pass"}',
        )
    ]
    JudgeClient(client=fake, model="claude-sonnet-5").judge(question="Q?", baseline="B", model_answer="M")
    assert fake.messages.create.call_args.kwargs["thinking"] == {"type": "disabled"}
    JudgeClient(client=fake, model="claude-haiku-4-5").judge(question="Q?", baseline="B", model_answer="M")
    assert "thinking" not in fake.messages.create.call_args.kwargs


def test_a_judge_from_neither_vendor_is_refused_when_built(monkeypatch):
    with pytest.raises(ValueError, match="neither a Claude nor an OpenAI model"):
        JudgeClient(client=MagicMock(), model="llama-3")


def test_without_a_client_the_judge_builds_the_metered_client_of_its_vendor(monkeypatch):
    from scripts.test_corpora.runner import judge as judge_module
    from scripts.test_corpora.runner import spend

    built = []
    monkeypatch.setattr(spend, "anthropic_client", lambda kind: built.append(("anthropic", kind)) or MagicMock())
    monkeypatch.setattr(spend, "openai_client", lambda kind: built.append(("openai", kind)) or MagicMock())
    judge_module.JudgeClient()
    judge_module.JudgeClient(model="claude-haiku-4-5")
    assert built == [("openai", "judge"), ("anthropic", "judge")], "both vendors' verdicts are booked as `judge`"

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


def test_extract_json_ignores_trailing_prose():
    """raw_decode stops at the JSON object's closing brace, ignoring trailing
    commentary even when that commentary itself contains braces."""
    txt = '{"correctness": 5, "groundedness": 5, "completeness": 5, "rationale": "ok"}\n\nNote: see {4.2}.'
    assert _extract_json(txt)["correctness"] == 5


def test_judge_defaults_missing_score_keys_to_zero():
    """If the judge omits a score key (Sonnet sometimes drops 'completeness'
    for find items when told 'SET TO 0; do not guess'), default to 0 rather
    than crash with a KeyError mid-eval."""
    c = MagicMock()
    c.messages.create.return_value = MagicMock(
        content=[MagicMock(text='{"correctness": 5, "groundedness": 4, "rationale": "no completeness key"}')]
    )
    v = AnswerJudge(client=c).judge_answer(
        question="q",
        model_answer="a",
        cited="",
        answer_key="k",
        qtype="lookup",
    )
    assert v.correctness == 5
    assert v.groundedness == 4
    assert v.completeness == 0  # defaulted


def test_judge_defaults_non_int_score_to_zero():
    """If the judge returns a non-integer score (e.g., 'N/A'), default to 0."""
    c = MagicMock()
    c.messages.create.return_value = MagicMock(
        content=[MagicMock(text='{"correctness": "N/A", "groundedness": 4, "completeness": 5, "rationale": "x"}')]
    )
    v = AnswerJudge(client=c).judge_answer(
        question="q",
        model_answer="a",
        cited="",
        answer_key="k",
        qtype="lookup",
    )
    assert v.correctness == 0  # defaulted
    assert v.groundedness == 4
    assert v.completeness == 5


def test_answer_verdict_source_defaults_to_empty_dict():
    """AnswerVerdict has an optional `source` field defaulting to {}."""
    v = AnswerVerdict(correctness=5, groundedness=5, completeness=5, rationale="ok")
    assert v.source == {}


def test_answer_verdict_source_round_trips_via_json():
    """AnswerVerdict(...) -> asdict -> json -> dict -> AnswerVerdict(**dict) cleanly."""
    import dataclasses
    import json

    original = AnswerVerdict(
        correctness=4,
        groundedness=3,
        completeness=5,
        rationale="x",
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


def test_judge_find_type_renders_count_and_sample_in_prompt():
    """For find items the prompt includes count and the rendered sample."""
    c = MagicMock()
    c.messages.create.return_value = MagicMock(
        content=[
            MagicMock(text='{"correctness": 4, "groundedness": 3, "completeness": 0, "rationale": "covered most"}')
        ]
    )
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
    c.messages.create.return_value = MagicMock(
        content=[
            MagicMock(text='{"correctness": 5, "groundedness": 5, "completeness": 0, "rationale": "clean decline"}')
        ]
    )
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


# ── PR-J: render_prompt() module-level function ───────────────────────


def test_render_prompt_lookup_includes_answer_key_and_qtype():
    from scripts.test_corpora.runner.answer_judge import render_prompt

    out = render_prompt(
        question="What is the governing law?",
        model_answer="Delaware.",
        cited="- contract.pdf",
        answer_key="Delaware",
        qtype="lookup",
    )
    assert "GROUND-TRUTH ANSWER KEY" in out
    assert "Delaware" in out
    assert "QUESTION TYPE: lookup" in out
    assert "What is the governing law?" in out


def test_render_prompt_find_includes_count_and_sample():
    from scripts.test_corpora.runner.answer_judge import render_prompt

    out = render_prompt(
        question="List every email from Houston",
        model_answer="3 emails found",
        cited="- email1.eml\n- email2.eml",
        answer_key={"count": 87, "all": [], "sample": ["e1.eml", "e2.eml"]},
        qtype="find",
    )
    assert "QUESTION TYPE: find" in out
    assert "count: 87" in out
    assert "e1.eml" in out
    assert "e2.eml" in out
    # find prompt sets completeness=0 deterministically
    assert "SET TO 0" in out


def test_render_prompt_negative_renders_NONE_for_missing_key():
    from scripts.test_corpora.runner.answer_judge import render_prompt

    out = render_prompt(
        question="Does this mention a non-compete?",
        model_answer="No mention.",
        cited="(no passages cited)",
        answer_key=None,
        qtype="negative",
    )
    assert "NONE" in out
    assert "QUESTION TYPE: negative" in out


def test_render_prompt_empty_model_answer_renders_placeholder():
    from scripts.test_corpora.runner.answer_judge import render_prompt

    out = render_prompt(
        question="q",
        model_answer="",
        cited="",
        answer_key="k",
        qtype="lookup",
    )
    # Both placeholders should appear
    assert "(empty)" in out
    assert "(no passages cited)" in out


def test_render_prompt_find_empty_sample_renders_empty_placeholder():
    """find-negative items (sample=[]) shouldn't crash — should render '(empty)'."""
    from scripts.test_corpora.runner.answer_judge import render_prompt

    out = render_prompt(
        question="q",
        model_answer="a",
        cited="c",
        answer_key={"count": 0, "all": [], "sample": []},
        qtype="find",
    )
    assert "count: 0" in out
    assert "(empty)" in out

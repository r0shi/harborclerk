"""judge_bakeoff re-judges what a finished run left on disk. It must spend only on verdicts it does not have, book
every call, and treat a judge it cannot parse as a result about that judge."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.test_corpora import judge_bakeoff
from scripts.test_corpora.runner import spend

GOOD = '{"claim_recall": 4, "claim_precision": 5, "entity_recall": 4, "completeness": 4, "verdict": "pass"}'


@pytest.fixture(autouse=True)
def _fresh_meter():
    spend.reset_for_tests()
    yield
    spend.reset_for_tests()


def _run_dir(tmp_path: Path) -> Path:
    run = tmp_path / "results" / "r1"
    (run / "baselines" / "cuad").mkdir(parents=True)
    for qid, question in (("q1", "Who are the parties?"), ("q2", "What law governs?"), ("q3", "No baseline for this")):
        if qid != "q3":
            (run / "baselines" / "cuad" / f"{qid}.json").write_text(
                json.dumps({"question_id": qid, "question": question, "answer": f"reference for {qid}"})
            )
    for model, qid, result in (
        ("m-one", "q1", {"answer": "A and B"}),
        ("m-one", "q2", {"answer": "   "}),  # completed with an empty answer: nothing to judge
        ("m-two", "q1", {"report": "a research report", "answer": None}),
        ("m-two", "q3", {"answer": "an answer with no baseline"}),
    ):
        folder = run / "responses" / "cuad" / model
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{qid}__standard.json").write_text(
            json.dumps({"corpus": "cuad", "model": model, "question_id": qid, "depth": "standard", "result": result})
        )
    return run


def test_it_judges_every_answer_that_has_a_baseline_and_something_to_judge(tmp_path):
    items = judge_bakeoff.load_items(_run_dir(tmp_path))
    assert [(i["model"], i["question_id"], i["answer"]) for i in items] == [
        ("m-one", "q1", "A and B"),
        ("m-two", "q1", "a research report"),
    ]
    assert items[0]["question"] == "Who are the parties?" and items[0]["baseline"] == "reference for q1"
    assert items[0]["key"] == "cuad__m-one__q1"


def _judges(monkeypatch, replies):
    """Route every judge call to `replies(judge, prompt)`, and count them."""
    calls = []

    def ask(judge, prompt):
        calls.append((judge, prompt))
        return replies(judge, prompt), "end_turn"

    monkeypatch.setattr(judge_bakeoff, "ask", ask)
    return calls


def test_a_verdict_already_on_disk_is_not_bought_again(tmp_path, monkeypatch):
    run, out = _run_dir(tmp_path), tmp_path / "out"
    calls = _judges(monkeypatch, lambda judge, prompt: GOOD)
    argv = ["--run-dir", str(run), "--out-dir", str(out), "--judges", "claude-haiku-4-5", "--reps", "2"]
    assert judge_bakeoff.main(argv) == 0
    assert len(calls) == 4, "two answers, two repetitions"
    assert sorted(p.name for p in (out / "reference" / "claude-haiku-4-5" / "rep1").iterdir()) == [
        "cuad__m-one__q1.json",
        "cuad__m-two__q1.json",
    ]
    assert judge_bakeoff.main(argv) == 0
    assert len(calls) == 4, "resumed: nothing left to buy"
    assert judge_bakeoff.main([*argv[:-1], "3"]) == 0
    assert len(calls) == 6, "a third repetition buys only itself"


def test_each_rubric_sends_its_own_prompt_with_the_question_the_reference_and_the_answer(tmp_path, monkeypatch):
    run, out = _run_dir(tmp_path), tmp_path / "out"
    calls = _judges(monkeypatch, lambda judge, prompt: GOOD)
    base = ["--run-dir", str(run), "--out-dir", str(out), "--judges", "claude-haiku-4-5"]
    judge_bakeoff.main(base)
    judge_bakeoff.main([*base, "--rubric", "split"])
    reference, split = calls[0][1], calls[2][1]
    for prompt in (reference, split):
        assert "Who are the parties?" in prompt and "reference for q1" in prompt and "A and B" in prompt
    assert "claim_recall" in reference and "answers_question" not in reference
    assert "answers_question" in split and "Do not penalise brevity" in split
    assert (out / "split" / "claude-haiku-4-5" / "rep0" / "cuad__m-one__q1.json").exists()


def test_a_judge_that_does_not_return_json_is_a_result_not_a_crash(tmp_path, monkeypatch):
    run, out = _run_dir(tmp_path), tmp_path / "out"
    _judges(
        monkeypatch,
        lambda judge, prompt: (
            "I would rate this answer quite highly." if "m-one" in prompt or "A and B" in prompt else GOOD
        ),
    )
    assert judge_bakeoff.main(["--run-dir", str(run), "--out-dir", str(out), "--judges", "gpt-5.6-luna"]) == 0
    bad = json.loads((out / "reference" / "gpt-5.6-luna" / "rep0" / "cuad__m-one__q1.json").read_text())
    good = json.loads((out / "reference" / "gpt-5.6-luna" / "rep0" / "cuad__m-two__q1.json").read_text())
    assert "verdict" not in bad and bad["parse_error"] and bad["raw"].startswith("I would rate")
    assert good["verdict"]["verdict"] == "pass" and good["stop"] == "end_turn"


def test_only_the_named_answers_are_judged(tmp_path, monkeypatch):
    run, out = _run_dir(tmp_path), tmp_path / "out"
    calls = _judges(monkeypatch, lambda judge, prompt: GOOD)
    keys = tmp_path / "keys.json"
    keys.write_text(json.dumps(["cuad__m-two__q1"]))
    judge_bakeoff.main(
        ["--run-dir", str(run), "--out-dir", str(out), "--judges", "claude-haiku-4-5", "--keys-file", str(keys)]
    )
    assert len(calls) == 1 and "a research report" in calls[0][1]


def test_a_judge_nobody_priced_is_refused_before_anything_is_bought(tmp_path, monkeypatch):
    calls = _judges(monkeypatch, lambda judge, prompt: GOOD)
    with pytest.raises(spend.UnpricedModel):
        judge_bakeoff.main(
            [
                "--run-dir",
                str(_run_dir(tmp_path)),
                "--out-dir",
                str(tmp_path / "out"),
                "--judges",
                "claude-haiku-4-5,gpt-99",
            ]
        )
    assert calls == [], "the priced judge was not called either"


def test_each_vendor_is_called_through_its_own_metered_client_and_booked(monkeypatch):
    booked = []
    anthropic_reply = SimpleNamespace(content=[SimpleNamespace(type="text", text=GOOD)], stop_reason="end_turn")
    openai_reply = SimpleNamespace(
        choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=GOOD))]
    )
    sent = {}

    def anthropic_client(kind):
        return SimpleNamespace(
            messages=SimpleNamespace(create=lambda **kw: sent.setdefault("anthropic", kw) and anthropic_reply)
        )

    def openai_client(kind):
        create = lambda **kw: sent.setdefault("openai", kw) and openai_reply  # noqa: E731
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    monkeypatch.setattr(spend, "anthropic_client", anthropic_client)
    monkeypatch.setattr(spend, "openai_client", openai_client)
    monkeypatch.setattr(spend, "get_meter", lambda: SimpleNamespace(count_unit=booked.append))
    assert judge_bakeoff.ask("claude-haiku-4-5", "p") == (GOOD, "end_turn")
    assert judge_bakeoff.ask("gpt-5.6-luna", "p") == (GOOD, "stop")
    assert booked == ["judge", "cross_judge"]
    assert "thinking" not in sent["anthropic"] and sent["openai"]["max_completion_tokens"] == 6000
    # Sonnet 5 thinks by itself when the field is left out; the models it is compared with do not.
    sent.clear()
    judge_bakeoff.ask("claude-sonnet-5", "p")
    assert sent["anthropic"]["thinking"] == {"type": "disabled"}
    with pytest.raises(ValueError, match="neither"):
        judge_bakeoff.ask("llama-3", "p")


def test_the_spend_cap_stops_it_with_the_documented_exit_code(tmp_path, monkeypatch):
    def ask(judge, prompt):
        raise spend.SpendCapExceeded("over the cap")

    monkeypatch.setattr(judge_bakeoff, "ask", ask)
    rc = judge_bakeoff.main(
        ["--run-dir", str(_run_dir(tmp_path)), "--out-dir", str(tmp_path / "out"), "--judges", "claude-haiku-4-5"]
    )
    assert rc == 3

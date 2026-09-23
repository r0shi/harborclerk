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
    items, left_out = judge_bakeoff.load_items(_run_dir(tmp_path))
    assert [(i["model"], i["question_id"], i["answer"]) for i in items] == [
        ("m-one", "q1", "A and B"),
        ("m-two", "q1", "a research report"),
    ]
    assert items[0]["question"] == "Who are the parties?" and items[0]["baseline"] == "reference for q1"
    assert items[0]["key"] == "cuad__m-one__q1__standard"
    assert left_out == {"empty answer": 1, "no baseline": 1}, "what is left out is counted, by reason"


def _answer(run: Path, model: str, qid: str, depth: str, answer: str) -> None:
    folder = run / "responses" / "cuad" / model
    folder.mkdir(parents=True, exist_ok=True)
    record = {"corpus": "cuad", "model": model, "question_id": qid, "depth": depth, "result": {"answer": answer}}
    (folder / f"{qid}__{depth}.json").write_text(json.dumps(record))


def test_one_question_answered_at_three_depths_is_three_answers(tmp_path, monkeypatch):
    """Review of #694: the key had no depth, so phase 3's three depths shared one verdict file, one was bought, the
    other two were skipped as "already on disk", and the file did not say which depth it had judged."""
    run, out = _run_dir(tmp_path), tmp_path / "out"
    for depth in ("quick", "deep"):
        _answer(run, "m-one", "q1", depth, f"the {depth} answer")
    items, _ = judge_bakeoff.load_items(run)
    keys = [i["key"] for i in items if i["model"] == "m-one"]
    assert keys == ["cuad__m-one__q1__deep", "cuad__m-one__q1__quick", "cuad__m-one__q1__standard"]
    calls = _judges(monkeypatch, lambda judge, prompt: GOOD)
    assert judge_bakeoff.main(["--run-dir", str(run), "--out-dir", str(out), "--judges", "claude-haiku-4-5"]) == 0
    assert len(calls) == 4 and sum("the deep answer" in p for _, p in calls) == 1
    assert len(list((out / "reference" / "claude-haiku-4-5" / "rep0").iterdir())) == 4


def test_it_judges_what_the_sweep_would(tmp_path):
    """Only units the run finished, and only against a baseline the sweep itself would accept."""
    run = _run_dir(tmp_path)
    _answer(run, "m-one", "q2", "standard", "California")
    (run / "baselines" / "cuad" / "q4.json").write_text(
        json.dumps({"question_id": "q4", "question": "?", "answer": "I couldn't find any documents about that."})
    )
    _answer(run, "m-one", "q4", "standard", "an answer to a question whose baseline found nothing")
    before, _ = judge_bakeoff.load_items(run)
    assert "cuad__m-one__q2__standard" in [i["key"] for i in before]
    assert "cuad__m-one__q4__standard" not in [i["key"] for i in before], "an unusable baseline is not paid for"
    units = [
        {"corpus": "cuad", "model": "m-one", "question_id": "q1", "depth": "standard", "status": "done"},
        {"corpus": "cuad", "model": "m-one", "question_id": "q2", "depth": "standard", "status": "degraded"},
        {"corpus": "cuad", "model": "m-two", "question_id": "q1", "depth": "standard", "status": "error"},
    ]
    (run / "state.json").write_text(json.dumps({"units": units}))
    items, left_out = judge_bakeoff.load_items(run)
    assert [i["key"] for i in items] == ["cuad__m-one__q1__standard"]
    assert left_out["the run did not finish this unit (not `done` in state.json)"] >= 2


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
        "cuad__m-one__q1__standard.json",
        "cuad__m-two__q1__standard.json",
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
    assert (out / "split" / "claude-haiku-4-5" / "rep0" / "cuad__m-one__q1__standard.json").exists()


def test_a_judge_that_does_not_return_json_is_a_result_not_a_crash(tmp_path, monkeypatch):
    run, out = _run_dir(tmp_path), tmp_path / "out"
    _judges(
        monkeypatch,
        lambda judge, prompt: (
            "I would rate this answer quite highly." if "m-one" in prompt or "A and B" in prompt else GOOD
        ),
    )
    assert judge_bakeoff.main(["--run-dir", str(run), "--out-dir", str(out), "--judges", "gpt-5.6-luna"]) == 0
    bad = json.loads((out / "reference" / "gpt-5.6-luna" / "rep0" / "cuad__m-one__q1__standard.json").read_text())
    good = json.loads((out / "reference" / "gpt-5.6-luna" / "rep0" / "cuad__m-two__q1__standard.json").read_text())
    assert "verdict" not in bad and bad["parse_error"] and bad["raw"].startswith("I would rate")
    assert good["verdict"]["verdict"] == "pass" and good["stop"] == "end_turn"


def test_only_the_named_answers_are_judged(tmp_path, monkeypatch):
    run, out = _run_dir(tmp_path), tmp_path / "out"
    calls = _judges(monkeypatch, lambda judge, prompt: GOOD)
    keys = tmp_path / "keys.json"
    keys.write_text(json.dumps(["cuad__m-two__q1__standard"]))
    judge_bakeoff.main(
        ["--run-dir", str(run), "--out-dir", str(out), "--judges", "claude-haiku-4-5", "--keys-file", str(keys)]
    )
    assert len(calls) == 1 and "a research report" in calls[0][1]


def test_a_judge_nobody_priced_is_refused_before_anything_is_bought(tmp_path, monkeypatch):
    calls = _judges(monkeypatch, lambda judge, prompt: GOOD)
    base = ["--run-dir", str(_run_dir(tmp_path)), "--out-dir", str(tmp_path / "out"), "--judges"]
    # Exit code 3, as the sweep gives. A traceback is not one of the documented ways to stop (review of #694).
    assert judge_bakeoff.main([*base, "claude-haiku-4-5,gpt-99"]) == 3
    assert calls == [], "the priced judge was not called either"
    assert judge_bakeoff.main([*base, "claude-haiku-4-5", "--spend-cap-usd", "30"]) == 3, "a cap over the limit"
    assert calls == []


def test_a_purchase_estimated_over_the_cap_does_not_start(tmp_path, monkeypatch):
    """ADR 0001, decision 6: a pre-run estimate that refuses to start. The first version had none, and its real run
    finished at 2.93 of a 3.00 cap."""
    run, out = _run_dir(tmp_path), tmp_path / "out"
    calls = _judges(monkeypatch, lambda judge, prompt: GOOD)
    argv = ["--run-dir", str(run), "--out-dir", str(out), "--judges", "claude-sonnet-4-6", "--reps", "40"]
    assert judge_bakeoff.main([*argv, "--spend-cap-usd", "0.50"]) == 3, "80 verdicts at about 1.6 cents"
    assert calls == []
    assert judge_bakeoff.main([*argv, "--spend-cap-usd", "5"]) == 0 and len(calls) == 80
    # What is already on disk is not estimated again: the same purchase now costs nothing, under any cap.
    assert judge_bakeoff.main([*argv, "--spend-cap-usd", "0.01"]) == 0 and len(calls) == 80


def test_it_will_not_write_into_a_run_directory(tmp_path, monkeypatch):
    """A run has its own ledger. Resuming into it would book judge money under the sweep's name."""
    run = _run_dir(tmp_path)
    calls = _judges(monkeypatch, lambda judge, prompt: GOOD)
    (run / "state.json").write_text(json.dumps({"units": []}))
    (run / "spend.json").write_text(json.dumps({"run": {"mode": "sweep"}, "total_usd": 7.25}))
    assert judge_bakeoff.main(["--run-dir", str(run), "--out-dir", str(run), "--judges", "claude-haiku-4-5"]) == 2
    assert calls == [] and json.loads((run / "spend.json").read_text())["run"]["mode"] == "sweep"


def test_a_judge_that_was_cut_off_has_no_verdict_and_can_be_bought_again(tmp_path, monkeypatch):
    """A reasoning model can spend its whole output limit thinking. JSON that happens to parse out of a truncated
    reply is not its verdict."""
    run, out = _run_dir(tmp_path), tmp_path / "out"
    stops = iter(["length", "end_turn", "end_turn", "end_turn"])
    calls = []

    def ask(judge, prompt):
        calls.append(prompt)
        return GOOD, next(stops)

    monkeypatch.setattr(judge_bakeoff, "ask", ask)
    argv = ["--run-dir", str(run), "--out-dir", str(out), "--judges", "gpt-5.6-luna"]
    assert judge_bakeoff.main(argv) == 0
    first = json.loads((out / "reference" / "gpt-5.6-luna" / "rep0" / "cuad__m-one__q1__standard.json").read_text())
    assert "verdict" not in first and "cut off (length)" in first["parse_error"]
    assert judge_bakeoff.main(argv) == 0 and len(calls) == 2, "kept as a result about that judge"
    assert judge_bakeoff.main([*argv, "--retry-failed"]) == 0 and len(calls) == 3, "unless asked again"
    again = json.loads((out / "reference" / "gpt-5.6-luna" / "rep0" / "cuad__m-one__q1__standard.json").read_text())
    assert again["verdict"]["verdict"] == "pass"


def test_a_real_call_is_reserved_settled_and_written_to_the_bake_offs_own_ledger(tmp_path, monkeypatch):
    """Every other test here stubs the client. This one goes through the meter: a fake SDK under MeteredAnthropic."""
    run, out = _run_dir(tmp_path), tmp_path / "out"
    usage = SimpleNamespace(
        input_tokens=2500, output_tokens=400, cache_creation_input_tokens=0, cache_read_input_tokens=0
    )
    reply = SimpleNamespace(content=[SimpleNamespace(type="text", text=GOOD)], stop_reason="end_turn", usage=usage)
    sdk = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: reply))
    monkeypatch.setattr(spend, "anthropic_client", lambda kind: spend.MeteredAnthropic(kind, sdk))
    assert judge_bakeoff.main(["--run-dir", str(run), "--out-dir", str(out), "--judges", "claude-haiku-4-5"]) == 0
    ledger = json.loads((out / "spend.json").read_text())
    row = ledger["by_kind"]["judge"]
    assert (row["calls"], row["units"], row["input_tokens"], row["output_tokens"]) == (2, 2, 5000, 800)
    assert ledger["total_usd"] == pytest.approx(2 * (2500 * 1.00 + 400 * 5.00) / 1e6)
    assert ledger["run"]["mode"] == "judge-bakeoff" and ledger["run"]["source_run"] == "r1"


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
    assert booked == ["judge", "judge"], "one kind: the sweep's judge prompt shape, whichever vendor answers it"
    assert "thinking" not in sent["anthropic"] and sent["openai"]["max_completion_tokens"] == 6000
    # Sonnet 5 and Opus 5 think by themselves when the field is left out; the models they are compared with do not.
    for thinker in ("claude-sonnet-5", "claude-opus-5"):
        sent.clear()
        judge_bakeoff.ask(thinker, "p")
        assert sent["anthropic"]["thinking"] == {"type": "disabled"}, thinker
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

"""The cloud spend cap (ADR 0001, decision 6): prices in configuration, a pre-run estimate that refuses
to start over the cap, a hard stop on the running total, and actual spend on record."""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
from types import SimpleNamespace

import anthropic
import pytest

from scripts.test_corpora import conftest as cfg
from scripts.test_corpora.runner import spend
from scripts.test_corpora.runner.spend import Price, SpendCapExceeded, SpendConfig, SpendMeter, Usage

HARNESS = Path(__file__).resolve().parents[1]


def _harness_sources() -> list[Path]:
    """Every Python source of the harness except its tests. The walk prunes dot-directories: local
    virtualenvs live here under several names and hold tens of thousands of files."""
    found = []
    for root, dirs, files in os.walk(HARNESS):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in {"tests", "__pycache__"}]
        found += [Path(root) / f for f in files if f.endswith(".py")]
    return sorted(found)


def _config(cap: float = 1.00) -> SpendConfig:
    return SpendConfig(
        cap_usd=cap,
        prices={
            "m": Price(input=10.0, output=100.0),
            "cached": Price(10.0, 100.0, cache_write=20.0, cache_read=1.0),
            "claude-sonnet-4-6": Price(input=3.0, output=15.0),
        },
        prices_verified="2026-01-01",
        chars_per_token=2.0,
        estimates={"judge": Usage(1000, 100)},
    )


# ── the shipped configuration ──


def test_the_shipped_policy_is_the_adrs():
    c = spend.load_config()
    assert c.cap_usd == 25.00
    assert c.price("claude-sonnet-4-6") == Price(input=3.0, output=15.0, cache_write=6.0, cache_read=0.30)
    assert c.price("gpt-4o") == Price(input=2.5, output=10.0)


def test_every_model_the_harness_names_by_default_is_priced():
    """An unpriced default would make the first call of a run fail, after the local models had run for hours."""
    from scripts.test_corpora.runner import answer_judge
    from scripts.test_corpora.runner.cross_judge import OpenAIJudgeProvider
    from scripts.test_corpora.runner.providers.anthropic_provider import AnthropicProvider
    from scripts.test_corpora.runner.providers.openai_provider import OpenAIProvider

    c = spend.load_config()
    named = {cfg.JUDGE_MODEL, cfg.BASELINE_MODEL, answer_judge.JUDGE_MODEL}
    named.add(OpenAIJudgeProvider(client=object())._model)
    named.add(AnthropicProvider(mcp_session=None)._model)
    named.add(OpenAIProvider(mcp_session=None)._model)
    for model in named:
        c.price(model)


def test_every_call_kind_the_harness_uses_has_an_estimate():
    kinds = set()
    for path in _harness_sources():
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("anthropic_client", "openai_client")
            ):
                assert node.args and isinstance(node.args[0], ast.Constant), (
                    f"{path.name}:{node.lineno}: the call kind must be a string literal, so it can be checked here"
                )
                kinds.add(node.args[0].value)
    assert kinds == set(spend.load_config().estimates), "a call kind without an estimate cannot be planned for"


def test_an_unpriced_model_is_refused_with_the_way_to_fix_it():
    with pytest.raises(spend.UnpricedModel, match="spend.yaml"):
        _config().price("gpt-99")


@pytest.mark.parametrize("body", ["cap_usd: 0", "cap_usd: -5"])
def test_a_cap_of_nothing_is_a_configuration_error(tmp_path, body):
    good = (HARNESS / "spend.yaml").read_text()
    bad = tmp_path / "spend.yaml"
    bad.write_text(good.replace("cap_usd: 25.00", body))
    with pytest.raises(ValueError):
        spend.load_config(bad)


# ── arithmetic ──


def test_cost_is_tokens_times_price_per_million():
    c = _config()
    assert c.cost("m", Usage(1_000_000, 0)) == 10.0
    assert c.cost("m", Usage(0, 1_000_000)) == 100.0
    assert c.cost("m", Usage(1000, 100)) == pytest.approx(0.01 + 0.01)
    assert c.cost("cached", Usage(0, 0, cache_write_tokens=1_000_000, cache_read_tokens=1_000_000)) == 21.0
    # No cache price configured: a write is charged at twice input, a read as plain input. Never less.
    assert c.cost("m", Usage(0, 0, cache_write_tokens=1_000_000, cache_read_tokens=1_000_000)) == 30.0


# ── the hard stop ──


def test_a_call_that_could_cross_the_cap_is_refused_before_it_is_made():
    m = SpendMeter(_config(cap=1.00))
    m.settle("m", "judge", m.reserve("m", 50_000, 4000), Usage(50_000, 4000))  # 0.50 + 0.40
    assert m.total_usd == pytest.approx(0.90)
    with pytest.raises(SpendCapExceeded, match="0.9000 of 1.00"):
        m.reserve("m", 1000, 1000)  # worst case 0.11
    assert m.total_usd == pytest.approx(0.90), "a refused call costs nothing"
    m.reserve("m", 1000, 900)  # worst case 0.10: exactly the cap is not over it


def test_reservations_in_flight_count_against_the_cap():
    m = SpendMeter(_config(cap=1.00))
    m.reserve("m", 0, 6000)  # 0.60 outstanding, nothing settled
    with pytest.raises(SpendCapExceeded):
        m.reserve("m", 0, 6000)
    assert m.remaining_usd == pytest.approx(0.40)


def test_settling_replaces_the_worst_case_with_what_was_used():
    m = SpendMeter(_config())
    reserved = m.reserve("m", 10_000, 5000)
    assert reserved == pytest.approx(0.60)
    assert m.settle("m", "judge", reserved, Usage(2000, 100)) == pytest.approx(0.03)
    assert m.total_usd == pytest.approx(0.03) and m.remaining_usd == pytest.approx(0.97)


def test_a_response_without_readable_usage_is_charged_its_worst_case():
    m = SpendMeter(_config())
    reserved = m.reserve("m", 10_000, 5000)
    assert m.settle("m", "judge", reserved, None) == pytest.approx(0.60)
    assert m.snapshot()["estimated_calls"] == 1


def test_a_failed_call_gives_its_reservation_back():
    m = SpendMeter(_config())
    m.release(m.reserve("m", 10_000, 5000))
    assert m.total_usd == 0 and m.remaining_usd == pytest.approx(1.00)


def test_a_run_may_lower_the_cap_and_may_not_raise_it(monkeypatch):
    assert SpendMeter(_config(cap=25), cap_usd=5).cap_usd == 5
    with pytest.raises(ValueError, match="not raise"):
        SpendMeter(_config(cap=25), cap_usd=25.01)
    with pytest.raises(ValueError):
        SpendMeter(_config(cap=25), cap_usd=0)
    monkeypatch.setenv(spend.CAP_ENV, "3")
    assert spend.configure().cap_usd == 3
    monkeypatch.setenv(spend.CAP_ENV, "1000")
    with pytest.raises(ValueError, match="not raise"):
        spend.configure()


# ── the ledger ──


def test_the_ledger_is_rewritten_after_every_call_and_a_resumed_run_inherits_it(tmp_path):
    ledger = tmp_path / "run" / "spend.json"
    m = SpendMeter(_config(), ledger_path=ledger)
    m.settle("m", "judge", m.reserve("m", 1000, 100), Usage(1000, 100))
    on_disk = json.loads(ledger.read_text())
    assert on_disk["total_usd"] == pytest.approx(0.02) and on_disk["calls"] == 1
    assert on_disk["by_kind"]["judge"] == {
        "calls": 1,
        "units": 0,
        "input_tokens": 1000,
        "cache_tokens": 0,
        "output_tokens": 100,
        "usd": 0.02,
    }
    assert on_disk["by_model"]["m"]["usd"] == 0.02
    assert on_disk["cap_usd"] == 1.00 and on_disk["prices_verified"] == "2026-01-01"
    assert not list(ledger.parent.glob("*.tmp"))

    resumed = SpendMeter(_config(), ledger_path=ledger)
    assert resumed.total_usd == pytest.approx(0.02)
    resumed.settle("m", "judge", resumed.reserve("m", 1000, 100), Usage(1000, 100))
    assert json.loads(ledger.read_text())["calls"] == 2
    with pytest.raises(SpendCapExceeded):
        resumed.reserve("m", 0, 9700)  # 0.97 on top of 0.04: the resumed run shares the first one's cap


def test_the_ledger_names_the_judge_exists_before_any_call_and_refuses_a_second_judge(tmp_path):
    ledger = tmp_path / "spend.json"
    SpendMeter(_config(), ledger_path=ledger, run_info={"run_id": "r1", "judge_model": "claude-sonnet-4-6"})
    on_disk = json.loads(ledger.read_text())
    assert on_disk["run"] == {"run_id": "r1", "judge_model": "claude-sonnet-4-6"}
    assert on_disk["total_usd"] == 0 and on_disk["calls"] == 0
    SpendMeter(_config(), ledger_path=ledger, run_info={"judge_model": "claude-sonnet-4-6"})
    with pytest.raises(ValueError, match="mix two judges"):
        SpendMeter(_config(), ledger_path=ledger, run_info={"judge_model": "claude-sonnet-5"})
    # An entry point that names no judge (the cross-judge audit) resumes without complaint and keeps the record.
    assert SpendMeter(_config(), ledger_path=ledger).snapshot()["run"]["judge_model"] == "claude-sonnet-4-6"


# ── the pre-run estimate ──


def test_a_run_estimated_over_the_cap_does_not_start():
    m = SpendMeter(_config(cap=1.00))
    assert m.estimate([("judge", "m", 10)]) == pytest.approx(0.20)
    assert m.require_within_cap([("judge", "m", 50)]) == pytest.approx(1.00)
    with pytest.raises(SpendCapExceeded, match="51 x judge on m"):
        m.require_within_cap([("judge", "m", 51)])
    m.settle("m", "judge", m.reserve("m", 0, 1000), Usage(0, 1000))  # 0.10 spent
    with pytest.raises(SpendCapExceeded, match="0.10 already spent"):
        m.require_within_cap([("judge", "m", 50)])
    with pytest.raises(KeyError, match="no estimate"):
        m.estimate([("unknown_kind", "m", 1)])


def test_units_are_counted_apart_from_calls_because_a_question_is_a_tool_loop():
    m = SpendMeter(_config())
    for _ in range(3):
        m.settle("m", "baseline_question", m.reserve("m", 1000, 100), Usage(1000, 100, cache_read_tokens=500))
    m.count_unit("baseline_question")
    row = m.snapshot()["by_kind"]["baseline_question"]
    assert (row["calls"], row["units"], row["input_tokens"], row["cache_tokens"]) == (3, 1, 3000, 1500)


def test_each_call_kind_counts_its_units_where_the_work_finishes():
    """The estimates are per unit. A kind that never counts units cannot be corrected from the ledger."""
    counted = set()
    for path in _harness_sources():
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "count_unit" and node.args:
                counted.add(node.args[0].value)
    assert counted == set(spend.load_config().estimates)


def _units(kind: str) -> int:
    return spend.get_meter().snapshot()["by_kind"].get(kind, {}).get("units", 0)


def test_each_finished_piece_of_work_counts_one_unit_whoever_did_it(tmp_path, monkeypatch):
    """The source check above is satisfied if either provider counts `baseline_question`. Each is driven here:
    removing the count from one alone used to pass."""
    from unittest.mock import MagicMock

    from scripts.test_corpora.corpora import synthetic
    from scripts.test_corpora.runner.answer_judge import AnswerJudge
    from scripts.test_corpora.runner.cross_judge import OpenAIJudgeProvider
    from scripts.test_corpora.runner.judge import JudgeClient
    from scripts.test_corpora.runner.providers.anthropic_provider import AnthropicProvider
    from scripts.test_corpora.runner.providers.openai_provider import OpenAIProvider

    _use()
    claude = MagicMock()
    claude.messages.create.return_value = MagicMock(content=[MagicMock(text="An answer.")], stop_reason="end_turn")
    AnthropicProvider(mcp_session=None, client=claude).run_question(question="q", question_id="q1", corpus="cuad")
    assert _units("baseline_question") == 1

    gpt = MagicMock()
    message = SimpleNamespace(content="An answer.", tool_calls=None)
    gpt.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(finish_reason="stop", message=message)]
    )
    OpenAIProvider(mcp_session=None, model="gpt-4o", client=gpt).run_question(
        question="q", question_id="q1", corpus="cuad"
    )
    assert _units("baseline_question") == 2
    assert OpenAIJudgeProvider(client=gpt).judge("prompt") == "An answer." and _units("cross_judge") == 1

    verdict = (
        '{"claim_recall": 5, "claim_precision": 5, "entity_recall": 5, "completeness": 5, "verdict": "pass", '
        '"correctness": 5, "groundedness": 5, "rationale": "r"}'
    )
    claude.messages.create.return_value = MagicMock(content=[MagicMock(text=verdict)])
    JudgeClient(client=claude).judge(question="q", baseline="b", model_answer="a")
    assert _units("judge") == 1
    AnswerJudge(client=claude).judge_answer(question="q", model_answer="a", cited="", answer_key="k", qtype="lookup")
    assert _units("answer_judge") == 1

    claude.messages.create.return_value = MagicMock(content=[MagicMock(text='{"text": "t", "facts": {}}')])
    monkeypatch.setattr(synthetic, "_make_client", lambda: claude)
    synthetic.acquire(tmp_path, doc_counts={"invoice": 2}, ocr_subset_count=0)
    assert _units("synthetic_doc") == 2


def test_the_report_header_line_names_spend_cap_judge_and_price_date():
    m = SpendMeter(_config(), run_info={"judge_model": "claude-sonnet-4-6"})
    m.settle("m", "judge", m.reserve("m", 1000, 100), Usage(1000, 100))
    m.settle("m", "judge", m.reserve("m", 0, 1000), None)
    assert spend.header_line(m.snapshot()) == (
        "Cloud spend: USD 0.12 of a 1.00 cap over 2 calls (1 charged at their worst case); "
        "judge claude-sonnet-4-6; prices as of 2026-01-01."
    )


def test_the_sweeps_plan_counts_pending_cloud_work(tmp_path):
    from scripts.test_corpora.runner import sweep
    from scripts.test_corpora.runner.state import Status

    def unit(phase, status=Status.PENDING, corpus="cuad"):
        return SimpleNamespace(phase=phase, status=status, corpus=corpus)

    units = [unit(0, corpus="synthetic"), unit(1), unit(1), unit(1, Status.DONE), unit(4), unit(5), unit(6), unit(2)]
    plan = dict((k, n) for k, _, n in sweep._spend_plan(units, workdir=tmp_path, judge_model="j", no_judge=False))
    assert plan == {"synthetic_doc": 280, "baseline_question": 2, "judge": 2}
    assert [m for _, m, _ in sweep._spend_plan(units, workdir=tmp_path, judge_model="j", no_judge=False)][2] == "j"
    quiet = dict((k, n) for k, _, n in sweep._spend_plan(units, workdir=tmp_path, judge_model="j", no_judge=True))
    assert quiet["judge"] == 0
    # An acquired synthetic corpus generates nothing.
    ingest = tmp_path / "synthetic" / "ingest"
    ingest.mkdir(parents=True)
    (ingest / ".acquired").write_text("")
    (ingest / "0001_invoice.txt").write_text("x")
    assert sweep._spend_plan(units, workdir=tmp_path, judge_model="j", no_judge=False)[0][2] == 0


def test_the_sweep_refuses_a_run_estimated_over_the_cap_and_makes_no_client(tmp_path, monkeypatch, caplog):
    from scripts.test_corpora.runner import sweep

    # The refusal comes before the login. With credentials set, a login attempt would show up as a call.
    monkeypatch.setenv("HC_USERNAME", "someone@example.test")
    monkeypatch.setenv("HC_PASSWORD", "not-a-real-password")
    logins = []
    monkeypatch.setattr(sweep.HarborClerkClient, "login", lambda self, *a: logins.append(a))
    made = []
    monkeypatch.setattr(spend, "anthropic_client", lambda kind: made.append(kind))
    args = ["--run-id", "r1", "--workdir", str(tmp_path), "--phases", "1", "--corpora", "cuad"]
    with caplog.at_level("ERROR"):
        code = sweep.main([*args, "--spend-cap-usd", "0.01"])
    assert code == 3 and made == [] and logins == []
    assert "stopped by the spend cap" in caplog.text and "baseline_question" in caplog.text


def test_a_cap_stop_at_the_judge_does_not_cost_the_unit_its_row():
    """The unit is DONE in state.json before it is judged, --resume skips DONE units, and metrics.csv is
    append-only. So the stop is held until the row is written. Checked on the parsed source: driving the
    sweep's unit loop needs a live Harbor Clerk."""
    from scripts.test_corpora.runner import sweep

    tree = ast.parse(Path(sweep.__file__).read_text())
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    judged = [
        t
        for t in ast.walk(main)
        if isinstance(t, ast.Try)
        and any(isinstance(c, ast.Call) and getattr(c.func, "attr", "") == "judge" for s in t.body for c in ast.walk(s))
    ]
    # main's own try contains the call too; the innermost one is the judge's.
    inner = max(judged, key=lambda t: t.lineno)
    handlers = [ast.unparse(h.type) for h in inner.handlers]
    assert handlers == ["spend.SpendError", "Exception"], "the spend stop is caught first, and held, not swallowed"
    held = inner.handlers[0].body
    assert [ast.unparse(s) for s in held] == ["spend_stop = exc"]
    row_written = next(
        c.lineno
        for c in ast.walk(main)
        if isinstance(c, ast.Call) and ast.unparse(c) == "metrics_writer.writerow(metrics_row)"
    )
    raised = [
        r.lineno for r in ast.walk(main) if isinstance(r, ast.Raise) and r.exc and ast.unparse(r.exc) == "spend_stop"
    ]
    assert len(raised) == 1 and raised[0] > row_written > inner.lineno


def test_answer_eval_mode_returns_3_when_the_cap_stops_it(tmp_path, monkeypatch, caplog):
    from scripts.test_corpora.runner import answer_eval, sweep

    def stopped(args):
        raise SpendCapExceeded("over")

    monkeypatch.setattr(answer_eval, "main_from_args", stopped)
    with caplog.at_level("ERROR"):
        code = sweep.main(["--run-id", "r1", "--workdir", str(tmp_path), "--mode", "answer-eval", "--label", "x"])
    assert code == 3 and "stopped by the spend cap" in caplog.text
    assert json.loads((tmp_path / "results" / "r1" / "spend.json").read_text())["calls"] == 0


def test_answer_eval_judges_with_the_judge_the_ledger_names(tmp_path, monkeypatch):
    from scripts.test_corpora.runner import answer_eval, sweep

    seen = {}
    monkeypatch.setattr(answer_eval, "run", lambda **kw: seen.update(kw) or 0)
    args = ["--run-id", "r1", "--workdir", str(tmp_path), "--mode", "answer-eval", "--label", "x"]
    assert sweep.main([*args, "--judge-model", "claude-sonnet-5"]) == 0
    assert seen["judge_model"] == "claude-sonnet-5"
    assert (
        json.loads((tmp_path / "results" / "r1" / "spend.json").read_text())["run"]["judge_model"] == "claude-sonnet-5"
    )

    built = {}
    monkeypatch.undo()
    monkeypatch.setattr(answer_eval, "AnswerJudge", lambda model: built.setdefault("model", model))
    monkeypatch.setattr(answer_eval, "_live_capture_fn", lambda **kw: None)
    monkeypatch.setattr(answer_eval, "load_groundtruth", lambda path: [])
    answer_eval.run(
        workdir=tmp_path,
        corpus="cuad",
        model="qwen3-8b",
        label="x",
        api_base="http://localhost:1",
        refresh=False,
        rejudge=False,
        insecure=False,
        judge_model="claude-sonnet-5",
    )
    assert built["model"] == "claude-sonnet-5"
    assert (
        json.loads((tmp_path / "answer-eval" / "reports" / "x" / "summary.json").read_text())["judge_model"]
        == "claude-sonnet-5"
    )


def test_the_rerun_tool_returns_3_when_its_estimate_is_over_the_cap(tmp_path, monkeypatch):
    from scripts.test_corpora.runner import rerun_pr_j

    populations = tmp_path / "pop.json"
    populations.write_text(json.dumps({"negatives_hedged": [{"id": str(i)} for i in range(40)], "finds_short": []}))
    monkeypatch.setenv(spend.CAP_ENV, "0.50")
    monkeypatch.setattr(rerun_pr_j, "_make_mcp_session", lambda **kw: pytest.fail("refused before any session"))
    code = rerun_pr_j.main(["--workdir", str(tmp_path), "--label", "t", "--populations", str(populations)])
    assert code == 3


# ── the metered clients ──


class _FakeAnthropic:
    def __init__(self, usage=None, fail=None):
        self.calls = []
        self.messages = SimpleNamespace(create=self._create, stream=lambda **k: "unmetered")
        # Real SDK clients have these. A passthrough would reach them, so the fake has them too.
        self.beta = self.completions = self.batches = "unmetered"
        self._usage, self._fail = usage, fail

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self._fail:
            raise self._fail
        return SimpleNamespace(content=[SimpleNamespace(text="{}")], usage=self._usage)


def _use(cap: float = 1.00) -> SpendMeter:
    spend._meter = SpendMeter(_config(cap))
    return spend._meter


def test_a_metered_call_is_charged_from_the_responses_usage_block():
    meter = _use()
    inner = _FakeAnthropic(usage=SimpleNamespace(input_tokens=2000, output_tokens=100))
    client = spend.MeteredAnthropic("judge", inner)
    client.messages.create(model="m", max_tokens=500, messages=[{"role": "user", "content": "x" * 100}])
    assert inner.calls[0]["max_tokens"] == 500, "the request reaches the API unchanged"
    assert meter.total_usd == pytest.approx(0.03) and meter.snapshot()["by_kind"]["judge"]["calls"] == 1


def test_the_reservation_uses_the_requests_size_and_its_output_limit():
    meter = _use(cap=0.05)
    inner = _FakeAnthropic(usage=SimpleNamespace(input_tokens=1, output_tokens=1))
    client = spend.MeteredAnthropic("judge", inner)
    with pytest.raises(SpendCapExceeded):
        client.messages.create(model="m", max_tokens=1000, messages=[])  # 0.10 of output alone
    with pytest.raises(SpendCapExceeded):
        client.messages.create(model="m", max_tokens=1, messages=[{"role": "user", "content": "x" * 12_000}])
    assert inner.calls == [] and meter.total_usd == 0
    client.messages.create(model="m", max_tokens=100, messages=[])
    assert len(inner.calls) == 1


class _Refused(anthropic.APIStatusError):
    """The API answered with an error status. Built without a real HTTP response."""

    def __init__(self):
        Exception.__init__(self, "529 overloaded")


def test_a_call_the_api_refused_costs_nothing_and_an_unpriced_model_is_never_called():
    meter = _use()
    inner = _FakeAnthropic(fail=_Refused())
    client = spend.MeteredAnthropic("judge", inner)
    with pytest.raises(anthropic.APIStatusError):
        client.messages.create(model="m", max_tokens=5000, messages=[])
    assert meter.total_usd == 0 and meter.remaining_usd == pytest.approx(1.00)
    with pytest.raises(spend.UnpricedModel):
        client.messages.create(model="gpt-99", max_tokens=10, messages=[])
    assert len(inner.calls) == 1


@pytest.mark.parametrize("failure", [TimeoutError("read timed out"), ConnectionError("reset"), KeyboardInterrupt()])
def test_a_call_that_may_have_been_served_is_charged_its_worst_case(failure):
    """A timeout, a dropped connection or Ctrl-C mid-generation can still be billed, with nobody left to
    read the usage. Recording it as free would let the real total pass the cap unseen."""
    meter = _use()
    client = spend.MeteredAnthropic("judge", _FakeAnthropic(fail=failure))
    with pytest.raises(type(failure)):
        client.messages.create(model="m", max_tokens=5000, messages=[])
    assert meter.total_usd == pytest.approx(0.50, abs=0.001) and meter.remaining_usd == pytest.approx(0.50, abs=0.001)
    assert meter.snapshot()["estimated_calls"] == 1


def test_openai_refusals_are_told_from_openai_failures():
    import openai

    class Refused(openai.APIStatusError):
        def __init__(self):
            Exception.__init__(self, "429")

    meter = _use()

    def create(**kwargs):
        raise Refused()

    inner = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    with pytest.raises(openai.APIStatusError):
        spend.MeteredOpenAI("cross_judge", inner).chat.completions.create(model="m", max_completion_tokens=600)
    assert meter.total_usd == 0
    # An Anthropic status error is not an OpenAI one: through this client it is an unknown failure.
    inner.chat.completions.create = lambda **k: (_ for _ in ()).throw(_Refused())
    with pytest.raises(anthropic.APIStatusError):
        spend.MeteredOpenAI("cross_judge", inner).chat.completions.create(model="m", max_completion_tokens=600)
    assert meter.total_usd > 0


def test_a_mocked_response_is_charged_its_worst_case_not_nothing():
    from unittest.mock import MagicMock

    meter = _use()
    spend.MeteredAnthropic("judge", MagicMock()).messages.create(model="m", max_tokens=1000, messages=[])
    assert meter.total_usd == pytest.approx(0.10, abs=0.001) and meter.snapshot()["estimated_calls"] == 1


def test_the_metered_client_can_do_nothing_but_create():
    _use()
    client = spend.MeteredAnthropic("judge", _FakeAnthropic())
    with pytest.raises(spend.UnmeterableCall):
        client.messages.create(model="m", max_tokens=10, messages=[], stream=True)
    for reach in (lambda: client.beta, lambda: client.messages.stream, lambda: client.completions):
        with pytest.raises(spend.UnmeteredAttribute, match="metered"):
            reach()
    inner = SimpleNamespace(responses="unmetered", beta="unmetered", chat=SimpleNamespace(completions="unmetered"))
    oai = spend.MeteredOpenAI("cross_judge", inner)
    for reach in (lambda: oai.responses, lambda: oai.chat.completions.stream, lambda: oai.beta):
        with pytest.raises(spend.UnmeteredAttribute):
            reach()


def test_attribute_access_on_a_metered_client_behaves_like_attribute_access():
    """hasattr, getattr-with-default and copy all rely on AttributeError. A BaseException here was a trap."""
    import copy

    client = spend.MeteredAnthropic("judge", _FakeAnthropic())
    assert hasattr(client, "messages") and not hasattr(client, "beta") and not hasattr(client.messages, "stream")
    assert getattr(client, "close", None) is None
    assert copy.copy(client).kind == "judge"
    assert issubclass(spend.UnmeteredAttribute, AttributeError)


def test_openai_calls_are_metered_by_their_own_usage_names_and_output_limit():
    meter = _use()
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=3000, completion_tokens=200))

    inner = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    client = spend.MeteredOpenAI("cross_judge", inner)
    client.chat.completions.create(model="m", max_completion_tokens=600, messages=[])
    assert meter.total_usd == pytest.approx(0.03 + 0.02)
    _use(cap=0.05)
    with pytest.raises(SpendCapExceeded):
        client.chat.completions.create(model="m", max_completion_tokens=600, messages=[])
    assert len(calls) == 1


def test_the_openai_client_is_not_built_until_it_is_used(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    spend.openai_client("cross_judge")  # openai.OpenAI() would raise here without a key


# ── out of budget is not a bad unit ──


def test_no_spend_error_can_be_caught_by_the_handlers_that_keep_a_run_going():
    for error in (SpendCapExceeded, spend.UnpricedModel, spend.UnmeterableCall):
        assert issubclass(error, spend.SpendError) and not issubclass(error, Exception)


def test_the_synthetic_corpus_stops_at_the_cap_instead_of_filling_with_placeholders(tmp_path, monkeypatch):
    from scripts.test_corpora.corpora import synthetic

    _use(cap=0.0001)
    monkeypatch.setattr(synthetic, "_make_client", lambda: spend.MeteredAnthropic("synthetic_doc", _FakeAnthropic()))
    with pytest.raises(SpendCapExceeded):
        synthetic.acquire(tmp_path, doc_counts={"invoice": 3}, ocr_subset_count=0)
    assert not list((tmp_path / "ingest").glob("*.txt")), "no '[generation failed]' documents"
    assert not (tmp_path / "ingest" / ".acquired").exists()


def test_an_unpriced_model_stops_the_synthetic_corpus_too(tmp_path, monkeypatch):
    """Found by this suite: as an ordinary exception it was swallowed per document, and the corpus was
    written as placeholders and marked acquired."""
    from scripts.test_corpora.corpora import synthetic

    spend._meter = SpendMeter(SpendConfig(1.0, {"m": Price(1.0, 1.0)}, "2026-01-01", 2.0, {}))
    monkeypatch.setattr(synthetic, "_make_client", lambda: spend.MeteredAnthropic("synthetic_doc", _FakeAnthropic()))
    with pytest.raises(spend.UnpricedModel):
        synthetic.acquire(tmp_path, doc_counts={"invoice": 3}, ocr_subset_count=0)
    assert not (tmp_path / "ingest" / ".acquired").exists()


def test_the_judge_and_the_providers_get_metered_clients_when_given_none():
    from scripts.test_corpora.runner.answer_judge import AnswerJudge
    from scripts.test_corpora.runner.cross_judge import OpenAIJudgeProvider
    from scripts.test_corpora.runner.judge import JudgeClient
    from scripts.test_corpora.runner.providers.anthropic_provider import AnthropicProvider
    from scripts.test_corpora.runner.providers.openai_provider import OpenAIProvider

    assert JudgeClient()._client.kind == "judge"
    assert AnswerJudge()._client.kind == "answer_judge"
    assert OpenAIJudgeProvider()._client.kind == "cross_judge"
    assert AnthropicProvider(mcp_session=None)._client.kind == "baseline_question"
    assert OpenAIProvider(mcp_session=None)._client.kind == "baseline_question"


# ── nothing else may make a cloud client ──

_SDKS = {"anthropic", "openai"}


def _is_client_class(name: str) -> bool:
    # Anthropic, AsyncAnthropic, AnthropicBedrock, AnthropicFoundry, OpenAI, AsyncOpenAI, AzureOpenAI, ...
    # The SDKs' error and type names (RateLimitError, APIStatusError, NotGiven) contain neither word.
    return "Anthropic" in name or "OpenAI" in name


class _ClientReferences(ast.NodeVisitor):
    """Every reference to a cloud SDK client class outside a type annotation: constructed, aliased
    (`factory = anthropic.Anthropic`), or imported by name. Parsed, not searched: the docstrings and
    comments in this harness mention `anthropic.Anthropic()` a dozen times.

    What this cannot see: a class reached through getattr or importlib, and a raw client handed to
    `JudgeClient(client=...)` by a caller. The first is deliberate evasion; the second is how the tests
    inject fakes, and the sweep passes a metered one."""

    def __init__(self):
        self.hits: list[int] = []

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.value, ast.Name) and node.value.id in _SDKS and _is_client_class(node.attr):
            self.hits.append(node.lineno)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if (node.module or "").split(".")[0] in _SDKS:
            self.hits += [node.lineno for a in node.names if _is_client_class(a.name)]

    def visit_arg(self, node: ast.arg) -> None:
        pass  # a parameter's annotation is not a use

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self.visit(node.value)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        for child in (*node.args.args, *node.args.kwonlyargs, *node.body, *node.decorator_list):
            self.visit(child)
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)

    visit_AsyncFunctionDef = visit_FunctionDef


def _client_references(source: str) -> list[int]:
    visitor = _ClientReferences()
    visitor.visit(ast.parse(source))
    return sorted(visitor.hits)


def test_the_guard_sees_a_use_and_not_a_mention_or_an_annotation():
    assert _client_references("import anthropic\nc = anthropic.Anthropic()\n") == [2]
    assert _client_references("import anthropic\nfactory = anthropic.Anthropic\nc = factory()\n") == [2]
    assert _client_references("import anthropic\nc = anthropic.AnthropicFoundry()\n") == [2]
    assert _client_references("from openai import OpenAI\nc = OpenAI(api_key='k')\n") == [1]
    assert _client_references("from anthropic import AsyncAnthropicBedrock as B\n") == [1]
    assert _client_references("import openai\ndef f(c=openai.OpenAI()): ...\n") == [2]
    quiet = (
        '"""uses anthropic.Anthropic()"""\n# openai.OpenAI()\nimport anthropic, openai\n'
        "def f(client: anthropic.Anthropic | None = None) -> openai.OpenAI: ...\n"
        "x: anthropic.Anthropic\n"
        "try: ...\nexcept (anthropic.RateLimitError, openai.APIStatusError): ...\n"
    )
    assert _client_references(quiet) == []


def test_only_the_spend_module_refers_to_a_cloud_client_class():
    offenders = {}
    sources = _harness_sources()
    assert len(sources) > 25, "the walk found too little to mean anything"
    for path in sources:
        rel = path.relative_to(HARNESS)
        if rel == Path("runner/spend.py"):
            continue
        if hits := _client_references(path.read_text()):
            offenders[str(rel)] = hits
    assert offenders == {}, "an unmetered client is an uncapped one: use spend.anthropic_client / spend.openai_client"

"""The cloud spend cap (ADR 0001, decision 6): prices in configuration, a pre-run estimate that refuses
to start over the cap, a hard stop on the running total, and actual spend on record."""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path
from types import SimpleNamespace

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
    assert on_disk["by_kind"]["judge"] == {"calls": 1, "input_tokens": 1000, "output_tokens": 100, "usd": 0.02}
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

    made = []
    monkeypatch.setattr(spend, "anthropic_client", lambda kind: made.append(kind))
    args = ["--run-id", "r1", "--workdir", str(tmp_path), "--phases", "1", "--corpora", "cuad"]
    with caplog.at_level("ERROR"):
        code = sweep.main([*args, "--spend-cap-usd", "0.01"])
    assert code == 3 and made == []
    assert "stopped by the spend cap" in caplog.text and "baseline_question" in caplog.text


# ── the metered clients ──


class _FakeAnthropic:
    def __init__(self, usage=None, fail=None):
        self.calls = []
        self.messages = SimpleNamespace(create=self._create, stream=lambda **k: "unmetered")
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


def test_an_api_error_costs_nothing_and_an_unpriced_model_is_never_called():
    meter = _use()
    inner = _FakeAnthropic(fail=RuntimeError("overloaded"))
    client = spend.MeteredAnthropic("judge", inner)
    with pytest.raises(RuntimeError, match="overloaded"):
        client.messages.create(model="m", max_tokens=5000, messages=[])
    assert meter.total_usd == 0 and meter.remaining_usd == pytest.approx(1.00)
    with pytest.raises(spend.UnpricedModel):
        client.messages.create(model="gpt-99", max_tokens=10, messages=[])
    assert len(inner.calls) == 1


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
        with pytest.raises((spend.UnmeterableCall, AttributeError)):
            reach()
    oai = spend.MeteredOpenAI("cross_judge", object())
    for reach in (lambda: oai.responses, lambda: oai.chat.completions.stream, lambda: oai.beta):
        with pytest.raises((spend.UnmeterableCall, AttributeError)):
            reach()


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

_CLIENT_CLASSES = {
    "Anthropic",
    "AsyncAnthropic",
    "AnthropicBedrock",
    "AnthropicVertex",
    "OpenAI",
    "AsyncOpenAI",
    "AzureOpenAI",
    "AsyncAzureOpenAI",
}


def _client_constructions(source: str) -> list[int]:
    """Line numbers where a cloud SDK client is constructed. Parsed, not searched: the docstrings and
    comments in this harness mention `anthropic.Anthropic()` a dozen times."""
    hits = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", None)
            if name in _CLIENT_CLASSES:
                hits.append(node.lineno)
    return hits


def test_the_guard_sees_a_construction_and_not_a_mention():
    assert _client_constructions("import anthropic\nc = anthropic.Anthropic()\n") == [2]
    assert _client_constructions("from openai import OpenAI\nc = OpenAI(api_key='k')\n") == [2]
    assert (
        _client_constructions('"""uses anthropic.Anthropic()"""\n# openai.OpenAI()\nx: "anthropic.Anthropic"\n') == []
    )


def test_only_the_spend_module_constructs_a_cloud_client():
    offenders = {}
    sources = _harness_sources()
    assert len(sources) > 25, "the walk found too little to mean anything"
    for path in sources:
        rel = path.relative_to(HARNESS)
        if rel == Path("runner/spend.py"):
            continue
        if hits := _client_constructions(path.read_text()):
            offenders[str(rel)] = hits
    assert offenders == {}, "an unmetered client is an uncapped one: use spend.anthropic_client / spend.openai_client"

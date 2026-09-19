"""Cloud spend metering for the eval harness (ADR 0001, decision 6).

Every cloud client the harness uses comes from `anthropic_client()` or
`openai_client()` here. The client it gets back can do one thing, create a
completion, and that one thing is metered:

- before the call, the worst case (request size at the input price plus
  `max_tokens` at the output price) is reserved against the cap, and the call
  is refused if the running total plus that reservation would cross it;
- after the call, the reservation is replaced by what the response's usage
  block says was used, and the ledger (`spend.json`) is rewritten.

So the running total can overshoot the cap by at most the amount one call's
real input exceeds its estimate, and the estimate is deliberately high.

The errors here are not `Exception`s. The harness is full of
`except Exception` handlers whose job is to keep a twenty-hour run going past
one bad unit (the judge call, each synthetic document, the retry helpers).
Running out of budget, or calling a model that has no price, is not a bad
unit: caught there, the synthetic corpus fills with "[generation failed]"
documents, is marked acquired, and the run carries on unjudged. Like
`KeyboardInterrupt`, these have to reach the top.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import math
import os
import threading
import time
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parent.parent / "spend.yaml"
CAP_ENV = "HC_EVAL_SPEND_CAP_USD"
LEDGER_NAME = "spend.json"
# Attempts after the first, for what the SDKs would have retried themselves. Each one is metered.
MAX_RETRIES = 2
_sleep = time.sleep
# Reserved for the reply when a request names no output limit.
DEFAULT_MAX_OUTPUT_TOKENS = 16384


class SpendError(BaseException):
    """A reason the run has to stop, not a reason one unit failed. See the module docstring for why none of
    these is an Exception: each would otherwise be swallowed once per remaining call."""


class SpendCapExceeded(SpendError):
    """The next call could take the run past its cap."""


class UnpricedModel(SpendError):
    """The model has no price in spend.yaml, so a call to it cannot be capped. Every call would fail the same way."""


class UnmeterableCall(SpendError):
    """A request whose cost the meter cannot see (a streamed create)."""


class SpendConfigError(ValueError):
    """The run asked for something the spend policy does not allow (a higher cap, a second judge on a resumed
    run). Entry points turn it into exit code 3, like a cap refusal."""


class UnmeteredAttribute(AttributeError):
    """Anything on a metered client other than create. An AttributeError, because that is what attribute
    access raises: `hasattr`, `getattr(x, name, default)`, `copy` and mock autospec all depend on it. Nothing
    is spent on this path, so nothing is lost if a keep-going handler catches it."""


@dataclasses.dataclass(frozen=True)
class Price:
    """USD per million tokens."""

    input: float
    output: float
    cache_write: float | None = None
    cache_read: float | None = None


@dataclasses.dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0


@dataclasses.dataclass(frozen=True)
class SpendConfig:
    cap_usd: float
    prices: dict[str, Price]
    prices_verified: str
    chars_per_token: float
    estimates: dict[str, Usage]

    def price(self, model: str) -> Price:
        try:
            return self.prices[model]
        except KeyError:
            raise UnpricedModel(
                f"{model!r} has no price in {CONFIG_PATH.name}, so a call to it cannot be capped. "
                f"Add it under prices.models with the list price and the date you read it. Priced: {sorted(self.prices)}"
            ) from None

    def cost(self, model: str, usage: Usage) -> float:
        p = self.price(model)
        # A cache write costs more than plain input and a cache read less. Where the config gives no cache
        # price, the write is charged as the dearest published multiplier (2x) and the read as plain input.
        write = p.cache_write if p.cache_write is not None else 2 * p.input
        read = p.cache_read if p.cache_read is not None else p.input
        return (
            usage.input_tokens * p.input
            + usage.output_tokens * p.output
            + usage.cache_write_tokens * write
            + usage.cache_read_tokens * read
        ) / 1_000_000


def load_config(path: Path | None = None) -> SpendConfig:
    raw = yaml.safe_load((path or CONFIG_PATH).read_text())
    cap = float(raw["cap_usd"])
    if not cap > 0:
        raise ValueError("cap_usd must be positive")
    prices = {m: Price(**{k: float(v) for k, v in p.items()}) for m, p in raw["prices"]["models"].items()}
    for m, p in prices.items():
        if not (p.input > 0 and p.output > 0):
            raise ValueError(f"{m}: a price of zero would make the cap meaningless")
    chars = float(raw["chars_per_token"])
    if not chars > 0:
        raise ValueError("chars_per_token must be positive")
    estimates = {k: Usage(int(v["input_tokens"]), int(v["output_tokens"])) for k, v in raw["estimates"].items()}
    return SpendConfig(cap, prices, str(raw["prices"]["verified"]), chars, estimates)


_EMPTY_ROW = {"calls": 0, "units": 0, "input_tokens": 0, "cache_tokens": 0, "output_tokens": 0, "usd": 0.0}


def header_line(snapshot: dict[str, Any]) -> str:
    """The line a dated report leads with (ADR 0001, decision 6: spend in the report header)."""
    run = snapshot.get("run", {})
    estimated = snapshot.get("estimated_calls", 0)
    return (
        f"Cloud spend: USD {snapshot['total_usd']:.2f} of a {snapshot['cap_usd']:.2f} cap over {snapshot['calls']} calls"
        + (f" ({estimated} charged at their worst case)" if estimated else "")
        + f"; judge {run.get('judge_model') or 'none'}; prices as of {snapshot['prices_verified']}."
    )


class SpendMeter:
    """The running total for one benchmark run. Thread-safe; persisted after every call when given a ledger."""

    def __init__(
        self,
        config: SpendConfig,
        *,
        cap_usd: float | None = None,
        ledger_path: Path | None = None,
        run_info: dict[str, str] | None = None,
    ):
        if cap_usd is not None and cap_usd > config.cap_usd:
            raise SpendConfigError(
                f"a run may lower the cap, not raise it: asked for {cap_usd:.2f}, {CONFIG_PATH.name} says "
                f"{config.cap_usd:.2f}. Raising it is an edit to that file, in a PR."
            )
        if cap_usd is not None and not cap_usd > 0:
            raise SpendConfigError("the cap must be positive")
        self.config = config
        self.cap_usd = config.cap_usd if cap_usd is None else float(cap_usd)
        self._ledger_path = ledger_path
        # What a report has to name beside the spend: the judge and the baseline model of this run.
        self._run_info = dict(run_info or {})
        self._lock = threading.Lock()
        self._outstanding = 0.0
        self._total = 0.0
        self._calls = 0
        self._estimated_calls = 0
        self._by_model: dict[str, dict[str, float]] = {}
        self._by_kind: dict[str, dict[str, float]] = {}
        if ledger_path is not None and ledger_path.exists():
            # A resumed run is the same run: it inherits what it has already spent.
            prior = json.loads(ledger_path.read_text())
            self._total = float(prior["total_usd"])
            self._calls = int(prior.get("calls", 0))
            self._estimated_calls = int(prior.get("estimated_calls", 0))
            self._by_model = prior.get("by_model", {})
            self._by_kind = prior.get("by_kind", {})
            was, now = prior.get("run", {}).get("judge_model"), self._run_info.get("judge_model")
            if was and now and was != now:
                raise SpendConfigError(
                    f"this run was judged by {was}; resuming it with {now} would mix two judges' scores in one "
                    "set of results. Start a new --run-id for a different judge."
                )
            self._run_info = {**prior.get("run", {}), **self._run_info}
            if was and not now:
                # A --no-judge resume judges nothing. It does not un-judge what the run already judged: the
                # record stays, and so does the guard against a second judge.
                self._run_info["judge_model"] = was
        with self._lock:
            # Written at the start, so a run that spends nothing still has its manifest.
            self._persist_locked()

    @property
    def total_usd(self) -> float:
        with self._lock:
            return self._total

    @property
    def remaining_usd(self) -> float:
        with self._lock:
            return self.cap_usd - self._total - self._outstanding

    def reserve(self, model: str, input_tokens: int, max_output_tokens: int) -> float:
        """Set aside one call's worst case, or refuse the call."""
        worst = self.config.cost(model, Usage(input_tokens, max_output_tokens))
        with self._lock:
            if self._total + self._outstanding + worst > self.cap_usd:
                raise SpendCapExceeded(
                    f"spent USD {self._total:.4f} of {self.cap_usd:.2f}; the next {model} call could cost up to "
                    f"{worst:.4f} ({input_tokens} tokens in, {max_output_tokens} out). Stopping before it."
                )
            self._outstanding += worst
        return worst

    def release(self, reserved: float) -> None:
        """The call failed before it produced a response, so nothing was billed."""
        with self._lock:
            self._outstanding = max(0.0, self._outstanding - reserved)

    def settle(self, model: str, kind: str, reserved: float, usage: Usage | None) -> float:
        """Replace a reservation with the actual cost. With no readable usage, the reservation is the cost."""
        actual = reserved if usage is None else self.config.cost(model, usage)
        with self._lock:
            self._outstanding = max(0.0, self._outstanding - reserved)
            self._total += actual
            self._calls += 1
            if usage is None:
                self._estimated_calls += 1
            for table, key in ((self._by_model, model), (self._by_kind, kind)):
                row = table.setdefault(key, dict(_EMPTY_ROW))
                row["calls"] += 1
                if usage is not None:
                    row["input_tokens"] += usage.input_tokens
                    row["cache_tokens"] = (
                        row.get("cache_tokens", 0) + usage.cache_write_tokens + usage.cache_read_tokens
                    )
                    row["output_tokens"] += usage.output_tokens
                row["usd"] = round(row["usd"] + actual, 6)
            self._persist_locked()
        return actual

    def count_unit(self, kind: str) -> None:
        """One unit of work of this kind is finished. The estimates in spend.yaml are per unit, and a unit is
        not always a call: a baseline question is a tool loop of several. Tokens per unit, not per call, is
        what corrects an estimate."""
        with self._lock:
            row = self._by_kind.setdefault(kind, dict(_EMPTY_ROW))
            row["units"] = row.get("units", 0) + 1
            self._persist_locked()

    def estimate(self, plan: list[tuple[str, str, int]]) -> float:
        """USD for a plan of (kind, model, calls), from the per-kind token estimates in spend.yaml."""
        total = 0.0
        for kind, model, count in plan:
            if count <= 0:
                continue
            if kind not in self.config.estimates:
                raise KeyError(f"no estimate for call kind {kind!r} in {CONFIG_PATH.name}")
            total += count * self.config.cost(model, self.config.estimates[kind])
        return total

    def require_within_cap(self, plan: list[tuple[str, str, int]]) -> float:
        """Refuse to start a run whose estimate, on top of what is already spent, is over the cap."""
        estimate = self.estimate(plan)
        with self._lock:
            spent = self._total
        if spent + estimate > self.cap_usd:
            lines = ", ".join(f"{count} x {kind} on {model}" for kind, model, count in plan if count > 0)
            raise SpendCapExceeded(
                f"this run is estimated at USD {estimate:.2f} ({lines}) on top of {spent:.2f} already spent, over "
                f"the cap of {self.cap_usd:.2f}. Narrow it (--phases, or --no-judge) and run it again, with --resume if "
                "this run id already has units."
            )
        return estimate

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> dict[str, Any]:
        return {
            "run": self._run_info,
            "cap_usd": self.cap_usd,
            "total_usd": round(self._total, 6),
            "calls": self._calls,
            # Calls whose response carried no readable usage and were charged their worst case instead.
            "estimated_calls": self._estimated_calls,
            "by_model": self._by_model,
            "by_kind": self._by_kind,
            "prices_verified": self.config.prices_verified,
            "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

    def _persist_locked(self) -> None:
        if self._ledger_path is None:
            return
        self._ledger_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._ledger_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._snapshot_locked(), indent=2) + "\n")
        tmp.replace(self._ledger_path)


# ── the process-wide meter ──

_meter: SpendMeter | None = None
_meter_lock = threading.Lock()


def _cap_from_env() -> float | None:
    raw = os.environ.get(CAP_ENV)
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        raise SpendConfigError(f"{CAP_ENV}={raw!r} is not a number") from None


def configure(
    *,
    ledger_path: Path | None = None,
    cap_usd: float | None = None,
    config_path: Path | None = None,
    run_info: dict[str, str] | None = None,
) -> SpendMeter:
    """Set the meter for this run. Entry points call this once, with the run's directory, before any cloud call."""
    global _meter
    config = load_config(config_path)
    requested = cap_usd if cap_usd is not None else _cap_from_env()
    with _meter_lock:
        _meter = SpendMeter(config, cap_usd=requested, ledger_path=ledger_path, run_info=run_info)
        return _meter


def get_meter() -> SpendMeter:
    """The configured meter. An entry point that forgot to configure one still gets a cap, just no ledger."""
    global _meter
    with _meter_lock:
        if _meter is None:
            log.warning("spend meter used before configure(): the cap applies, but no spend.json will be written")
            _meter = SpendMeter(load_config(), cap_usd=_cap_from_env())
        return _meter


def reset_for_tests() -> None:
    global _meter
    with _meter_lock:
        _meter = None


# ── metered clients ──


def _estimate_tokens(payload: Any, chars_per_token: float) -> int:
    return math.ceil(len(json.dumps(payload, default=str)) / chars_per_token)


def _as_count(value: Any) -> int | None:
    # bool is an int; a MagicMock is not. Anything that is not a plain non-negative count is unreadable.
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _anthropic_usage(resp: Any) -> Usage | None:
    u = getattr(resp, "usage", None)
    i, o = _as_count(getattr(u, "input_tokens", None)), _as_count(getattr(u, "output_tokens", None))
    if i is None or o is None:
        return None
    return Usage(
        i,
        o,
        _as_count(getattr(u, "cache_creation_input_tokens", None)) or 0,
        _as_count(getattr(u, "cache_read_input_tokens", None)) or 0,
    )


def _openai_usage(resp: Any) -> Usage | None:
    u = getattr(resp, "usage", None)
    # prompt_tokens includes cached tokens; all of them are charged at the full input price.
    i, o = _as_count(getattr(u, "prompt_tokens", None)), _as_count(getattr(u, "completion_tokens", None))
    return None if i is None or o is None else Usage(i, o)


def _response_headers(exc: BaseException) -> dict[str, str]:
    headers = getattr(getattr(exc, "response", None), "headers", None)
    try:
        return {str(k).lower(): str(v) for k, v in dict(headers or {}).items()}
    except (TypeError, ValueError):
        return {}


def _retry_delay(exc: BaseException, attempt: int) -> float:
    """How long to wait before the next attempt: the server's Retry-After when it gives one of a minute or
    less (what the SDKs' own retries do), otherwise a short exponential backoff. Retrying a 429 after half
    a second, with the server asking for ten, fails the call, and a failed judge call is an empty verdict."""
    headers = _response_headers(exc)
    for name, scale in (("retry-after-ms", 0.001), ("retry-after", 1.0)):
        try:
            asked = float(headers[name]) * scale
        except (KeyError, ValueError):
            continue
        if 0 < asked <= 60:
            return asked
    return min(8.0, 0.5 * 2**attempt)


class _MeteredCreate:
    def __init__(self, owner: _MeteredClient, output_limit_keys: tuple[str, ...], read_usage):
        self._owner = owner
        self._output_limit_keys = output_limit_keys
        self._read_usage = read_usage

    def __getattr__(self, name: str) -> Any:
        raise UnmeteredAttribute(f"only create is metered; {name!r} would spend without being seen")

    def create(self, **kwargs: Any) -> Any:
        if kwargs.get("stream"):
            raise UnmeterableCall("a streamed response has no usage block to meter; call without stream=True")
        model = kwargs["model"]
        limit = next((kwargs[k] for k in self._output_limit_keys if kwargs.get(k)), DEFAULT_MAX_OUTPUT_TOKENS)
        meter = get_meter()
        meter.config.price(model)  # an unpriced model is refused before anything is built or reserved
        self._owner._client()  # a client that cannot be built (no key) has spent nothing: fail before reserving
        request = {k: v for k, v in kwargs.items() if k not in ("model", *self._output_limit_keys)}
        estimate = _estimate_tokens(request, meter.config.chars_per_token)
        # The SDK clients are built with max_retries=0 and the retries happen here, so that every attempt
        # is reserved and settled. A retry inside the SDK would be an attempt the ledger never saw.
        for attempt in range(1 + MAX_RETRIES):
            reserved = meter.reserve(model, estimate, int(limit))
            try:
                resp = self._owner._create(**kwargs)
            except BaseException as exc:
                if self._owner._known_unbilled(exc):
                    meter.release(reserved)
                else:
                    # A read timeout, a connection dropped after the request went out, Ctrl-C mid-generation:
                    # it may have been served and billed with nobody left to read the usage.
                    meter.settle(model, self._owner.kind, reserved, None)
                if attempt < MAX_RETRIES and self._owner._retryable(exc):
                    _sleep(_retry_delay(exc, attempt))
                    continue
                raise
            meter.settle(model, self._owner.kind, reserved, self._read_usage(resp))
            return resp
        raise AssertionError("unreachable")  # pragma: no cover


class _MeteredClient:
    """A cloud client that can create a completion and nothing else. There is no attribute passthrough:
    `messages.stream`, `messages.batches`, `beta`, `responses` and the rest would spend without being seen."""

    def __init__(self, kind: str, factory, inner: Any | None):
        self.kind = kind
        self._factory = factory
        self._inner = inner

    def _known_unbilled(self, exc: BaseException) -> bool:
        """True only where the request is known not to have been served: the API answered with an error
        status, the connection was never made, or the SDK rejected the request while building it (no
        credentials, a malformed argument: a plain TypeError or ValueError). Anything else is charged: phantom dollars during an outage
        would fill a ledger that a resumed run inherits, but so would silence about a served call."""
        import httpx

        sdk = self._sdk()
        if isinstance(exc, getattr(sdk, "APIStatusError", ())):
            return True
        if isinstance(exc, getattr(sdk, "APIConnectionError", ())):
            return isinstance(exc.__cause__, (httpx.ConnectError, httpx.ConnectTimeout))
        # Exactly these types: json.JSONDecodeError and pydantic's ValidationError are ValueErrors too, and
        # those are raised after a response has been served.
        return type(exc) in (TypeError, ValueError)

    def _retryable(self, exc: BaseException) -> bool:
        """What the SDKs themselves retry: connection errors, and 408, 409, 429 and 5xx."""
        sdk = self._sdk()
        should = _response_headers(exc).get("x-should-retry")
        if should in ("true", "false") and isinstance(exc, getattr(sdk, "APIStatusError", ())):
            return should == "true"  # the server's own word, which the SDKs obey
        if isinstance(exc, getattr(sdk, "APIConnectionError", ())):
            return True
        status = getattr(exc, "status_code", None)
        return (
            isinstance(exc, getattr(sdk, "APIStatusError", ()))
            and isinstance(status, int)
            and (status in (408, 409, 429) or status >= 500)
        )

    def _client(self) -> Any:
        # Built on first use: openai.OpenAI() demands its key at construction, and the harness builds
        # providers it may never call.
        if self._inner is None:
            self._inner = self._factory()
        return self._inner

    def __getattr__(self, name: str) -> Any:
        raise UnmeteredAttribute(
            f"the metered client does not expose {name!r}: only the create call is metered, and an unmetered "
            "call is an uncapped one. Add it to runner/spend.py with its metering."
        )


class MeteredAnthropic(_MeteredClient):
    def __init__(self, kind: str, inner: Any | None = None):
        import anthropic

        self._sdk = lambda: anthropic
        super().__init__(kind, lambda: anthropic.Anthropic(max_retries=0), inner)
        self.messages = _MeteredCreate(self, ("max_tokens",), _anthropic_usage)

    def _create(self, **kwargs: Any) -> Any:
        return self._client().messages.create(**kwargs)


class _Completions:
    def __init__(self, completions: _MeteredCreate):
        self.completions = completions


class MeteredOpenAI(_MeteredClient):
    def __init__(self, kind: str, inner: Any | None = None):
        import openai

        self._sdk = lambda: openai
        super().__init__(kind, lambda: openai.OpenAI(max_retries=0), inner)
        self.chat = _Completions(_MeteredCreate(self, ("max_completion_tokens", "max_tokens"), _openai_usage))

    def _create(self, **kwargs: Any) -> Any:
        return self._client().chat.completions.create(**kwargs)


def anthropic_client(kind: str) -> MeteredAnthropic:
    """The only way the harness gets an Anthropic client. `kind` names the call for the ledger and the estimate."""
    return MeteredAnthropic(kind)


def openai_client(kind: str) -> MeteredOpenAI:
    """The only way the harness gets an OpenAI client."""
    return MeteredOpenAI(kind)

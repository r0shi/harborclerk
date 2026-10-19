"""Chat orchestration — streaming tool-calling loop against llama-server."""

import asyncio
import json
import logging
import re
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncGenerator
from contextlib import aclosing
from typing import TYPE_CHECKING, Any

import httpx
from sqlalchemy import select

from harbor_clerk.api.scope import build_user_scope
from harbor_clerk.config import get_settings, refresh_llm_settings
from harbor_clerk.db import async_session_factory
from harbor_clerk.llm.citations import dedupe_citations, extract_citations_from_tool_result
from harbor_clerk.llm.health import report_llm_error, report_llm_success, server_unready_message
from harbor_clerk.llm.models import context_budget, get_model
from harbor_clerk.llm.tools import execute_tool, get_chat_tools, summarize_tool_result
from harbor_clerk.models.chat_message import ChatMessage
from harbor_clerk.models.conversation import Conversation

if TYPE_CHECKING:
    from harbor_clerk.llm.models import ModelInfo

logger = logging.getLogger(__name__)

# SSE keepalive interval — prevents idle connection drops during LLM thinking.
_KEEPALIVE_INTERVAL = 30.0

# Rough chars-per-token figure the estimator divides by. It is a prose figure, and the estimate is raw: every bound
# derived from it is multiplied by the measured ratio below (#805).
_CHARS_PER_TOKEN = 3.5

# Reserve this fraction of context window for the model's response.
_RESPONSE_RESERVE = 0.20

# Stop the tool loop when context usage exceeds this fraction, leaving room
# for the model to generate a text response.
_TOOL_LOOP_BUDGET = 0.75

# Hard safety cap on tool rounds (prevents infinite loops even if budget check fails).
_MAX_TOOL_ROUNDS = 25

# When a time budget (settings.chat_time_budget_seconds; 0 turns every bound off) is configured and spent, the
# forced final answer gets this long, from its first content token. Thinking is not content: a model that reasons
# before it writes spent the whole grace on reasoning deltas and the user got nothing (#742), so the forced call asks
# it not to think, and one that thinks regardless gets _FINAL_ANSWER_THINKING_SECONDS from its first token to start
# writing, or is cut: thinking alone cannot hold the answer open. What it thought by then is shown (see the forced
# call), so a model whose answer lives in its reasoning channel (gpt-oss) has this long for the whole of it.
_FINAL_ANSWER_GRACE_SECONDS = 120.0
_FINAL_ANSWER_THINKING_SECONDS = 120.0

# The estimate is calibrated against the tokenizer (#805). The estimator divides characters by _CHARS_PER_TOKEN, a
# prose figure; tool results are JSON thick with identifiers, scores and quoted snippets, which tokenize at under two
# characters a token. On the four forced prompts llama-server refused (qwen36-35b-a3b at 16,384,
# bench-20260928-1050-keyed and the Enron and synthetic runs) its own count ran 1.95 to 2.31 times the estimate: a
# search stopped at an estimated 76% of the context was 18,224 tokens by the tokenizer, and the other three were
# 17,887, 19,240 and 21,228 against estimates of about 9,200 (#774). So the context budget's 75% was about 150% on a
# tool-heavy conversation, and every bound derived from the estimate was off by the same factor.
#
# llama-server counts the prompt of every completed call and reports it as ``usage.prompt_tokens``, in the stream's
# last chunk when the request asks for it (``stream_options: {"include_usage": true}``; at the pin, v0.4.1, the usage
# chunk carries an empty ``choices`` array, as the OpenAI spec has it, and without the option no usage is sent).
# The count is of the whole prompt, cached prefix included. The next prompt is predicted from it
# (``_predict_prompt_tokens``): the server's exact count of the last prompt it counted, plus the messages appended
# since, weighted per message (``_predict_messages_tokens``): a tool result and the call that asked for it at the
# model's tool ratio, prose (the system prompt, the user's and the assistant's words) and the tool schema at the
# model's prose ratio. With no count yet (the first call of a turn, and the history trim before it) the whole prompt
# is weighted the same way; reading all of it at the tool ratio trimmed twenty prose exchanges on a 16,384 slot to
# four, every turn for the life of the process, where the server at 70% takes thirteen (the third review of #806).
# The prediction is what the context-budget stop, the history trim and the forced call's outer deadline compare
# against; the context percentage the UI shows is the last count itself when there is one. A count below
# _ESTIMATE_RATIO_MIN times the estimate is distrusted (a build counting something else): it is not kept, and the
# next prompt is predicted from the weights alone. The one figure read bare is the per-result truncation cap
# (``tool_result_max_chars``), deliberately: dividing it by the ratio halves the evidence a search returns, a
# retrieval change for a benchmark to decide.
#
# Two ratios, per model, for the life of the process, and the prose ratio per conversation too (below). The tool
# ratio is measured as the increment between two counted prompts of a turn's loop: the difference of the counts over
# the estimate of the messages appended between them, which is the tool result and its call and nothing else. It is
# never measured over a whole prompt: a tool-less prompt measures the wrong density (a ratio taken from one read the
# first at-cap result at 1.2x, the second review), and a prompt whose results are the history's measures a diluted
# one (the system prompt and the schema, about 3,800 raw tokens at 1.2x, took a 2.2 density to 2.0 over three turns
# of the same chat, the review of #808), so a turn whose first counted prompt holds old results measures nothing
# until its first increment. The forced call measures nothing either: it drops the tools, so its count against the
# last is no increment; its count is kept for the bookkeeping. The prose ratio is measured over the whole of every
# prompt with no tool row in it (the first call of a conversation: system prompt, schema, question), which is
# exactly the density it is applied to, whether the server accepted the prompt (``usage.prompt_tokens``) or refused
# it as too long (the refusal names its count, ``_refused_prompt_tokens``). Its seed is 1.2, the figure the first
# calls measured on the benchmark models; a constant had no feedback, and at 16,384 a prose history that tokenizes
# at 1.5x (pasted logs, tables) put a trimmed first prompt at 87% of the slot and one at 1.8x (CJK text under the
# Qwen tokenizers) at 105%, refused on every turn for the life of the process (the review of #808). A refused first
# prompt is where that density is met: the loop records it from the refusal and, at a turn's first call, retries
# the turn once with the history trimmed against the count and the slot the refusal states (the loop's overflow
# branch), so the next conversation's first prompt is trimmed to fit and the refused one still gets its answer. A
# refusal once the turn has searched ends the search as the context budget does, and the forced call's calibrated
# retry shrinks the results; a first prompt refused twice is the "context window full" error, as before. The first prose
# measurement replaces the seed, which is a placeholder and not a measurement (the tool ratio's default is never
# replaced because its measurements once came from the wrong density, which cannot happen to the prose ratio);
# later ones blend in at _ESTIMATE_RATIO_SMOOTHING, under the same clamp. A limitation: once a conversation's
# history holds a tool row, every later first prompt of it is tool-bearing and measures neither ratio, so the prose
# density is re-measured only by conversations (or turns) that have not searched.
#
# Prose density is a property of the conversation (its language, pasted logs or tables), not of the model: English
# prose counts at about 1.0x the estimate, French 1.1x, CJK under the Qwen tokenizers 1.5 to 2.7x. Kept per model
# alone, the table followed whichever conversation ran last: alternating twenty-exchange conversations at 1.8x and
# 1.0x on a 16,384 slot moved it 1.8, 1.4, 1.6, 1.3, the third conversation's first prompt went at 89% of the slot and
# the English ones kept 6 and 8 exchanges of the 16 that fit (#810). So every prose measurement is also kept for its
# conversation and model (``_conversation_prose_ratios``), and a turn whose history holds no tool row reads the
# conversation's own (``_conversation_prose_ratio``) before the model's table, which is what a conversation this
# process has not measured starts from. The conversation's entry is replaced by each measurement, not blended: the
# next prompt is the measured one plus an exchange, so the last count is the nearest density there is, and a
# conversation that changes language follows it a turn behind rather than at half a step a turn. So the turn-one
# sample, mostly the system prompt and the schema, is replaced as the history grows, and needs no minimum share of
# history to be trusted: the prompt it is applied to has the same mix, and the trim it governs only bites once the
# history, re-measured every turn, is long. The entry applies only while it is re-measured: once the history holds a
# tool row, every first prompt is tool-bearing and measures nothing, and an entry kept would be the last tool-less
# sample for the life of the process (the turn-one prompt of a conversation that searched at once: a CJK one read
# at 1.05 where the model's table, fed by other CJK conversations, said 1.8; the review of #844). Such a turn, and
# every read after a turn has searched, takes the model's table as before #810, and the entry is dropped
# (``_forget_conversation_prose_ratio``), so a history whose tool rows have left the window starts again from the
# model's table until its first tool-less prompt measures it. The context-budget stop reads the model's table
# outright: it acts once a turn has searched (the forced call's deadline goes by the turn's rule, for a time budget
# spent before any result). The tool ratio stays per model:
# results are the corpus's passages in the tools' JSON, whatever the conversation's language, and it is re-measured
# inside every searching turn. The entries live as long as the process (the oldest past _CONVERSATION_PROSE_MAX go):
# the stored rows cannot rebuild one, since ``tokens_used`` adds every completion, reasoning included, to the count,
# and the rows a count covered depend on the trim of the time. After a restart a conversation starts from the model's
# table, as every conversation did before, and its first prose prompt measures it again (a refused one included,
# which is then retried as above).
#
# The tool ratio's default is the tool-heavy figure, 2.0: too high stops a search a round before the server would
# have (soft); too low admits a prompt the server refuses (hard). It is never replaced: every measurement is blended
# in at half weight from whatever stands, since the next increment is like the last and the half kept guards against
# a single odd count. The tool table is process-wide, so two conversations running at once blend their measurements
# in it; every model runs one slot, so that is rare. A measurement is clamped before it is blended: a count the
# estimate cannot explain moves a ratio at most half way to the clamp, never off the scale. The clamp's top is 3.0:
# it covers the 2.31 observed on tool results with margin.
#
# The forced prompt goes as the calibrated estimate stands, not trimmed beforehand: the server refuses before it reads
# and names its count, so a refused prompt is trimmed against that count, with the response reserve, and sent once
# more (the forced call, below). Trimming on an uncalibrated estimate discarded about half the retrieved evidence on
# every context-budget stop, including the twenty of twenty-four that fit as they were. What the calibration does
# not guarantee is the reserve: the server refuses at the slot's context, not at the reserve, so a prompt it accepts
# at 95% leaves the answer 5%. An answer cut there ends with finish_reason "length": content already streamed stands
# as it is, and a thought alone is shown under the "context ran out" lead.
#
# ``POST /tokenize`` would give the exact count before the forced call, but it takes raw text, not messages: the
# count would need the chat template rendered first (``/apply-template``, whose documented body is ``messages``
# alone, not the tools or the ``chat_template_kwargs`` the forced call sends), two requests and a rendering this
# module cannot check without a live server. The running calibration and the calibrated retry cover the forced call.
_ESTIMATE_RATIO_DEFAULT = 2.0
_ESTIMATE_RATIO_MIN = 0.5
_ESTIMATE_RATIO_MAX = 3.0
# The prose ratio's seed: what a tool-less prompt (the system prompt, the schema, the user's and the assistant's words)
# tokenized at over the estimate on the benchmark models. Replaced by the model's first measurement (module comment).
_PROSE_RATIO_DEFAULT = 1.2
# Measured prose-density ratios by model id ("" when none is active), beside the tool table. Process-wide.
_prose_ratios: dict[str, float] = {}
# Measured prose-density ratios by (conversation id, model id), read before the model's table (#810). Process-wide,
# the most recently measured _CONVERSATION_PROSE_MAX kept.
_conversation_prose_ratios: OrderedDict[tuple[str, str], float] = OrderedDict()
_CONVERSATION_PROSE_MAX = 1024
_ESTIMATE_RATIO_SMOOTHING = 0.5  # the weight of the newest measurement, the first included
# The history trim's target, as a fraction of the context: under the response reserve and under the tool-loop budget
# by a margin, so a trimmed first call is not already at the budget when its first result comes back (the second
# review of #806: trimmed to the reserve alone, the first call landed in the band where tools were omitted).
_TRIM_BUDGET_FRACTION = min(1 - _RESPONSE_RESERVE, _TOOL_LOOP_BUDGET) - 0.05
# Measured tokenizer-count-over-estimate ratios, by model id ("" when none is active). Process-wide.
_estimate_ratios: dict[str, float] = {}
# A tool result is not shrunk below this many characters: what is left names the tool and says it was cut.
_FORCED_CALL_RESULT_FLOOR_CHARS = 400
# Safety on a trim calibrated by the server's count: the count is of the prompt it refused, the retry's is a
# different one, and the template's per-message overhead does not shrink with the content.
_FORCED_CALL_RETRY_SAFETY = 1.1
# An increment smaller than this many raw tokens (an empty search result and its call: about 50, most of it the
# template's wrappers) does not measure the tool ratio: one such round at half weight took a converged 2.2 to 1.6
# and under-read the next at-cap result by a quarter (the fourth review of #808). A result worth measuring is bigger.
_INCREMENT_FLOOR_TOKENS = 200

# The forced call's outer bound (#773). Both clocks above start at the model's first token; before it the prompt is
# re-read in full (no tools this time, so the prompt cache misses), and the only bound on that read was the client's
# read timeout of 120 s per silent stretch, which a server that is prefilling never trips. Qwen3.8-27B prefills at 56
# tokens a second on the 32 GB mini (docs/reports/2026-09-28-benchmark-ix-bench-20260928-1050-keyed.md; the
# mixture-of-experts models at 240 to 360), so a forced call at 18,000 tokens spent five and a half minutes before
# either clock started, and a 300 s budget ended 8 to 19 minutes later. The outer deadline runs from the moment the
# search stopped: the time the slowest registered model needs to read the prompt (its estimated tokens, times the
# factor above, at this rate), then the thinking bound, then the grace. A constant rather than a registry field: two
# tiers have been measured, and an unmeasured figure in the registry would be a claim.
_PREFILL_TOKENS_PER_SECOND = 50.0

# llama-server refuses a prompt longer than the slot's context with HTTP 400 and, at the pin, a message that gives
# its count: {"error":{"code":400,"message":"request (18224 tokens) exceeds the available context size ...",
# "type":"exceed_context_size_error"}}. The older wordings are kept for older builds.
_CONTEXT_OVERFLOW_HINTS = (
    "exceed_context_size",
    "exceeds the available context",
    "context length",
    "too long",
    "context window",
    "max_tokens",
    "n_ctx",
)

# The forced final call's last message. Omitting the tool definitions says nothing to a model in the middle of a
# search: Qwen3.6 wrote the next tool call as text, eight times in ten (#742). Told the search is over, it answers.
# The second form is for a search cut before any result came back (the first generation outlasted the budget):
# there are no "results above" to answer from.
_SEARCH_OVER_MESSAGE = (
    "The search has ended. Answer the question now from the results above; do not call any tool. "
    "If they do not contain the answer, say what was found and what was not."
)
_SEARCH_OVER_UNSEARCHED_MESSAGE = (
    "The search has ended before any result came back; do not call any tool. Say that the documents could not be "
    "searched in the time allowed, and answer only what you can without them."
)

# The clock the budget is kept by; tests replace it here, not in ``time`` (the event loop keeps time there too).
_now = time.monotonic


def _tool_result_max_chars(context_tokens: int) -> int:
    """The per-tool-result truncation cap: a quarter of the context at ~3.5 characters a token, at most
    80,000 characters. ``payload_bounds`` sizes the summary and metadata bounds against it (#831)."""
    return min(int(context_tokens * 0.25 * 3.5), 80_000)


def _stopped_early_note(spent_seconds: float) -> str:
    minutes = max(1, round(spent_seconds / 60))
    unit = "minute" if minutes == 1 else "minutes"
    return f"\n\n_(I stopped after {minutes} {unit} and answered from what I had found.)_"


def _nothing_produced_fallback(stop_reason: str | None, server_failure: str | None = None) -> str:
    """The app's words when no answer came from the model, naming why (#742, #712 item 2, #684 item 3).

    The old sentence ("I used the available context to search but wasn't able to formulate a complete response")
    read as the model's answer, and a run of dead-server and budget failures was reported as answered. This one
    says who failed and in what: the model, which wrote nothing, or the model server, which failed the forced call
    (``server_failure`` says how). The eval harness recognises either opening (``APP_FALLBACK_LEADS`` in
    ``scripts/test_corpora/runner/quality.py``) and does not judge it as an answer: the first words are a contract.
    """
    stopped = {
        "time_budget": "the search stopped at its time budget",
        "context_budget": "the search results filled the model's context",
        "tool_rounds": f"the search stopped at its cap of {_MAX_TOOL_ROUNDS} tool rounds",
    }.get(stop_reason or "")
    if server_failure:
        lead = f"The model server failed while the final answer was being written ({server_failure})"
        lead += f": {stopped}, and the answer then asked for from what it had found did not come." if stopped else "."
        advice = "Try again in a moment; if it fails again, check the model in Settings."
    else:
        lead = {
            "time_budget": (
                "The model produced no answer in the time allowed: the search stopped at its time budget, and when "
                "asked to answer from what it had found, the model wrote nothing in the time it had."
            ),
            "context_budget": (
                "The model produced no answer in the context allowed: the search results filled the model's "
                "context, and when asked to answer from what it had found, the model wrote nothing."
            ),
            "tool_rounds": (
                f"The model produced no answer after {_MAX_TOOL_ROUNDS} tool rounds: the search stopped at its cap, "
                "and when asked to answer from what it had found, the model wrote nothing."
            ),
        }.get(stop_reason or "", "The model produced no answer: it returned nothing for this question.")
        advice = "Try a narrower question, or the Research tab for a question that spans many documents."
    return f"_{lead} {advice}_"


def _cut_thought_answer(thought: str, ran_out: str) -> str:
    """A thought cut short (by the time bounds, or by the server when the context ran out under it), under the app's
    lead: what the model had is shown, and not as an answer it gave. The lead's first words are the harness's too
    (``APP_FALLBACK_LEADS``)."""
    return f"_The model was still reasoning when its {ran_out} ran out; what it had:_\n\n{thought.strip()}"


def _forced_answer_cut(
    now: float,
    thinking_deadline: float | None,
    final_deadline: float | None,
    outer_deadline: float | None = None,
) -> str | None:
    """Why the forced answer is cut at ``now`` ("outer", "grace" or "thinking"), or None to keep reading.

    The outer deadline, measured from the moment the search stopped, holds whatever the inner clocks say: it is
    what bounds the prompt's re-read before the model's first token (#773). Under it, the grace, once running, is
    the only bound. Before it runs, the thinking bound is, once the model's first token has started it.
    """
    if outer_deadline is not None and now >= outer_deadline:
        return "outer"
    if final_deadline is not None:
        return "grace" if now >= final_deadline else None
    if thinking_deadline is not None and now >= thinking_deadline:
        return "thinking"
    return None


def _forced_call_outer_deadline(stopped_at: float, predicted_prompt_tokens: int) -> float:
    """When the forced call is cut whatever its clocks say: the time the slowest registered model needs to read the
    prompt (as predicted from the server's last count, ``_predict_prompt_tokens``, at _PREFILL_TOKENS_PER_SECOND),
    then the thinking bound, then the grace, from the moment the search stopped."""
    prefill_allowance = predicted_prompt_tokens / _PREFILL_TOKENS_PER_SECOND
    return stopped_at + prefill_allowance + _FINAL_ANSWER_THINKING_SECONDS + _FINAL_ANSWER_GRACE_SECONDS


def _line_carries_no_token(line: str) -> bool:
    """Whether a line of the stream can be let through past a deadline because it cuts nothing: "[DONE]", a blank
    separator or a comment, an unparseable body, the usage chunk (no choices at all), or the finish chunk (a delta
    with no content, tool call or reasoning). The chunks that follow a finished answer arrive after its last token,
    and cutting the answer on one of them would store a complete answer as cut (#806 review)."""
    if not line.startswith("data: "):
        return True
    data = line[6:]
    if data.strip() == "[DONE]":
        return True
    try:
        chunk = json.loads(data)
    except json.JSONDecodeError:
        return True
    choices = chunk.get("choices") or []
    if not choices:
        return True
    delta = choices[0].get("delta") or {}
    return not (delta.get("content") or delta.get("tool_calls") or delta.get("reasoning_content"))


def _is_context_overflow(status_code: int, detail: str) -> bool:
    """Whether llama-server refused the request because the prompt is longer than the slot's context."""
    lowered = detail.lower()
    return status_code == 400 and any(hint in lowered for hint in _CONTEXT_OVERFLOW_HINTS)


def _refused_prompt_tokens(detail: str) -> int | None:
    """The server's own count of the prompt it refused, from the 400's message ("request (18224 tokens) exceeds
    ...") or its ``n_prompt_tokens`` field; None when the body gives neither."""
    match = re.search(r"request \((\d+) tokens\)", detail) or re.search(r'"n_prompt_tokens"\s*:\s*(\d+)', detail)
    return int(match.group(1)) if match else None


def _refused_context_tokens(detail: str) -> int | None:
    """The slot's context as the refusal states it ("... exceeds the available context size (16384 tokens)", or its
    ``n_ctx`` field); None when the body gives neither. It is what the server runs at, which the client's
    ``context_budget`` need not be: under Compose nothing clamps the registry's window to the server's -c (#654)."""
    match = re.search(r"context size \((\d+) tokens\)", detail) or re.search(r'"n_ctx"\s*:\s*(\d+)', detail)
    return int(match.group(1)) if match else None


def _estimate_tokens(text: str) -> int:
    """Estimate token count from character length. Raw: the caller multiplies by the model's measured ratio."""
    return int(len(text) / _CHARS_PER_TOKEN)


def _estimate_messages_tokens(messages: list[dict]) -> int:
    """Estimate total tokens for a message list."""
    total = 0
    for msg in messages:
        total += _estimate_tokens(msg.get("content", ""))
        if msg.get("tool_calls"):
            total += _estimate_tokens(json.dumps(msg["tool_calls"]))
    # Per-message overhead (~4 tokens each for role, separators)
    total += len(messages) * 4
    return total


def _get_tool_schema_tokens(model: "ModelInfo | None" = None) -> int:
    """Compute the tool schema token estimate from current settings."""
    return _estimate_tokens(json.dumps(get_chat_tools(model=model)))


def _estimate_payload_tokens(messages: list[dict], tools: list[dict] | None) -> int:
    """The raw estimate of a chat request as sent: its messages and, when sent, its tool definitions. It is what a
    call's ``usage.prompt_tokens`` is measured against."""
    return _estimate_messages_tokens(messages) + (_estimate_tokens(json.dumps(tools)) if tools else 0)


def _estimate_ratio(model_id: str | None) -> float:
    """The tokenizer's count over the raw estimate, as last measured for ``model_id`` in this process, or the
    default when the model has not been measured."""
    return _estimate_ratios.get(model_id or "", _ESTIMATE_RATIO_DEFAULT)


def _clamped_ratio(counted: int, estimated: int) -> float:
    return min(_ESTIMATE_RATIO_MAX, max(_ESTIMATE_RATIO_MIN, counted / estimated))


def _record_estimate_ratio(model_id: str | None, counted: int, estimated: int) -> float | None:
    """Record a tool-result increment: the server's ``counted`` tokens of it (the difference of two prompts' counts)
    against the raw ``estimated`` tokens of the messages it holds, and return the model's tool ratio as it now
    stands. The measurement is clamped to [_ESTIMATE_RATIO_MIN, _ESTIMATE_RATIO_MAX] and blended in at
    _ESTIMATE_RATIO_SMOOTHING from what stands (the default, for a first measurement: it is never replaced outright).
    A count or an estimate that is not positive records nothing (None). The caller measures increments only (see the
    module comment)."""
    if counted <= 0 or estimated <= 0:
        return None
    measured = _clamped_ratio(counted, estimated)
    key = model_id or ""
    previous = _estimate_ratios.get(key, _ESTIMATE_RATIO_DEFAULT)
    ratio = previous + _ESTIMATE_RATIO_SMOOTHING * (measured - previous)
    _estimate_ratios[key] = ratio
    return ratio


def _measure_increment(model_id: str | None, counted: int, previous: int, increment: list[dict]) -> float | None:
    """Record the increment between two counted prompts of a turn, ``counted`` and ``previous``, over the raw
    estimate of ``increment``, the messages appended between them (the tool result and its call, nothing else), and
    return the tool ratio as it now stands; None when the increment is under _INCREMENT_FLOOR_TOKENS, which measures
    the template's wrappers rather than the result."""
    estimated = _estimate_messages_tokens(increment)
    if estimated < _INCREMENT_FLOOR_TOKENS:
        logger.debug(
            "Increment of %d estimated tokens is under the floor; not measured (model=%s)", estimated, model_id
        )
        return None
    ratio_now = _record_estimate_ratio(model_id, counted - previous, estimated)
    logger.debug(
        "Increment counted at %d tokens against %d estimated; ratio now %.2f (model=%s)",
        counted - previous,
        estimated,
        ratio_now if ratio_now is not None else float("nan"),
        model_id,
    )
    return ratio_now


def _prose_ratio(model_id: str | None) -> float:
    """The tokenizer's count over the raw estimate of prose and the schema, as last measured for ``model_id`` in this
    process, or the seed when the model has not been measured."""
    return _prose_ratios.get(model_id or "", _PROSE_RATIO_DEFAULT)


def _record_prose_ratio(model_id: str | None, counted: int, estimated: int) -> float | None:
    """Record a prompt with no tool row in it: the server's ``counted`` tokens against the raw ``estimated`` tokens of
    its messages and schema, and return the model's prose ratio as it now stands. Clamped like the tool ratio; the
    first measurement replaces the seed, later ones blend in at _ESTIMATE_RATIO_SMOOTHING. A count or an estimate
    that is not positive records nothing (None)."""
    if counted <= 0 or estimated <= 0:
        return None
    measured = _clamped_ratio(counted, estimated)
    key = model_id or ""
    previous = _prose_ratios.get(key)
    ratio = measured if previous is None else previous + _ESTIMATE_RATIO_SMOOTHING * (measured - previous)
    _prose_ratios[key] = ratio
    return ratio


def _conversation_prose_ratio(conversation_id: uuid.UUID | str, model_id: str | None) -> float:
    """The prose ratio a turn of ``conversation_id`` reads: the conversation's own last measurement on ``model_id`` in
    this process, or the model's table (``_prose_ratio``) when this process has not measured it (#810)."""
    measured = _conversation_prose_ratios.get((str(conversation_id), model_id or ""))
    return measured if measured is not None else _prose_ratio(model_id)


def _record_conversation_prose_ratio(
    conversation_id: uuid.UUID | str, model_id: str | None, counted: int, estimated: int
) -> float | None:
    """Record a prompt of ``conversation_id`` with no tool row in it: into the model's table, as
    ``_record_prose_ratio`` does (what a conversation not yet measured starts from), and as the conversation's own
    ratio, which the measurement replaces (module comment). Returns the conversation's ratio as it now stands; None
    when the count or the estimate is not positive (nothing recorded). The oldest entry past _CONVERSATION_PROSE_MAX
    goes."""
    if _record_prose_ratio(model_id, counted, estimated) is None:
        return None
    key = (str(conversation_id), model_id or "")
    _conversation_prose_ratios[key] = ratio = _clamped_ratio(counted, estimated)
    _conversation_prose_ratios.move_to_end(key)
    while len(_conversation_prose_ratios) > _CONVERSATION_PROSE_MAX:
        _conversation_prose_ratios.popitem(last=False)
    return ratio


def _forget_conversation_prose_ratio(conversation_id: uuid.UUID | str, model_id: str | None) -> None:
    """Drop the conversation's entry on ``model_id``: its history holds a tool row, so its prose is no longer
    re-measured and the model's table is what it reads (module comment)."""
    _conversation_prose_ratios.pop((str(conversation_id), model_id or ""), None)


def _predict_messages_tokens(messages: list[dict], ratio: float, prose: float) -> float:
    """The tokenizer's count of ``messages`` as predicted with no server count to go on: each message's raw estimate
    (with its per-message overhead) at the measured tool ``ratio`` for a tool result or the assistant call that asked
    for it, at the measured ``prose`` ratio for everything else."""
    total = 0.0
    for msg in messages:
        tokens = _estimate_tokens(msg.get("content") or "") + 4
        if msg.get("tool_calls"):
            tokens += _estimate_tokens(json.dumps(msg["tool_calls"]))
        dense = msg.get("role") == "tool" or bool(msg.get("tool_calls"))
        total += tokens * (ratio if dense else prose)
    return total


def _predict_prompt_tokens(
    messages: list[dict], counted: int | None, counted_upto: int, tools_tokens: int, ratio: float, prose: float
) -> int:
    """The tokenizer's count of the next prompt, as best predicted: the server's exact ``counted`` tokens of the last
    prompt it counted (which held ``messages[:counted_upto]``, and the tools), plus the messages appended since,
    weighted (``_predict_messages_tokens``); or, with no count yet, the whole of ``messages`` weighted and the
    schema's ``tools_tokens`` at the ``prose`` ratio."""
    if counted is None:
        return int(_predict_messages_tokens(messages, ratio, prose) + tools_tokens * prose)
    return int(counted + _predict_messages_tokens(messages[counted_upto:], ratio, prose))


def _answers_every_call(call: dict, results: list[dict]) -> bool:
    """Whether ``results`` (the tool rows directly after the assistant ``call``) hold one result per call it made. Ids
    are compared when every call and result carries one: a model that sent no id is stored with an empty one, and its
    result's row with none, so a missing id is not a mismatch."""
    calls = call.get("tool_calls") or []
    if len(results) != len(calls):
        return False
    call_ids = [tc.get("id") for tc in calls]
    result_ids = [r.get("tool_call_id") for r in results]
    if all(call_ids) and all(result_ids):
        return sorted(call_ids) == sorted(result_ids)
    return True


def _whole_tool_exchanges(history: list[dict]) -> list[dict]:
    """``history`` with every tool exchange whole: each tool result directly after the assistant call that asked for
    it, and each call followed by a result for every call it made (#839). A result whose call is not before it (the
    history window or the trim cut the call off) is dropped, and so is a call whose results were cut, with the
    results that remain. gpt-oss's template raises on a tool message with no call before it, and llama-server
    answers that prompt with a 500 (the pin, v0.4.1); other templates render the orphan and the model reads a
    result to a call it never made. Returns a new list; ``history`` holds no system prompt."""
    kept: list[dict] = []
    i = 0
    while i < len(history):
        msg = history[i]
        if msg.get("role") == "tool":
            i += 1  # a result with no call directly before its run
            continue
        if msg.get("tool_calls"):
            end = i + 1
            while end < len(history) and history[end].get("role") == "tool":
                end += 1
            if _answers_every_call(msg, history[i + 1 : end]):
                kept.extend(history[i:end])
            i = end
            continue
        kept.append(msg)
        i += 1
    return kept


def _trim_to_budget(
    messages: list[dict], context_window: int, model: "ModelInfo | None", ratio: float, prose: float
) -> list[dict]:
    """Trim oldest history messages to fit within context budget.

    Keeps the system prompt (index 0) and the most recent user message
    (last entry), whatever the budget: when nothing else fits, those two
    are what is sent, and the server says whether they fit (the floor; before
    it, a budget below the system prompt alone sent the whole history
    untrimmed). Removes messages from the front of history, a call together
    with its results (``_whole_tool_exchanges``): given whole exchanges, as the
    history window hands it (#839), it returns whole exchanges, and when the
    history fits it returns it as it is. The budget is _TRIM_BUDGET_FRACTION of the
    context, less the schema at the model's measured ``prose`` ratio; the
    messages are weighted as a prediction with no count is
    (``_predict_messages_tokens``): results and their calls at the model's
    measured tool ``ratio``, prose at ``prose``. Reading prose at the tool
    ratio kept four of twenty exchanges where the server took thirteen.

    Returns a new list (does not mutate the input).
    """
    budget = int(context_window * _TRIM_BUDGET_FRACTION - _get_tool_schema_tokens(model) * prose)

    if _predict_messages_tokens(messages, ratio, prose) <= budget or len(messages) < 3:
        return messages

    # System prompt (index 0) and the question (last) are always kept. Trim from index 1 onward.
    system = messages[:1]
    question = messages[-1:]
    history = messages[1:-1]

    # Remove from the front of history until we fit
    while history and _predict_messages_tokens(system + history + question, ratio, prose) > budget:
        # Only the front is cut, so the one exchange a pop can split is the one it starts: an assistant call popped
        # leaves its results leading, and they go with it. (The old "removed a tool result" branch was reached only
        # on a history that already led with an orphan; nothing precedes history[0] to be left without its result.)
        history.pop(0)
        history = _whole_tool_exchanges(history)

    trimmed = system + history + question
    if not history:
        logger.info(
            "Trimmed the whole history: the system prompt and the question alone are over the context budget "
            "(%d tokens); sending them as they are",
            budget,
        )
    if len(trimmed) < len(messages):
        logger.info(
            "Trimmed %d messages from history to fit context budget (%d tokens)",
            len(messages) - len(trimmed),
            budget,
        )
    return trimmed


def _shrink_result(content: str, max_chars: int) -> str:
    """``content`` cut to at most ``max_chars``: the structured cut when it stays within them, a plain one with a
    marker when it does not (``_truncate_for_llm`` adds its marker after the cut, so a result just above its limit
    came back no shorter)."""
    cut = _truncate_for_llm(content, max_chars)
    if len(cut) <= max_chars:
        return cut
    marker = "\n... [cut to fit the context]"
    return content[: max(0, max_chars - len(marker))] + marker


def _trim_for_forced_call(messages: list[dict], budget_tokens: int) -> list[dict]:
    """``messages`` with its tool results shrunk, largest first, until the estimate is within ``budget_tokens``
    (#774).

    The system prompt, the user's messages and the assistant's are kept whole, and so is every message: a tool
    result must follow the call it answers for the template to render. A result is halved at a time (a search's
    results are ranked, so its head is what the model should keep), down to _FORCED_CALL_RESULT_FLOOR_CHARS, and the
    largest goes first because one result at the truncation cap is a quarter of the context. When every result is at
    the floor and the estimate is still over, what is left is sent as it is. Returns a new list; the input is not
    changed, and the stored rows never were.
    """
    trimmed = [dict(m) for m in messages]
    while _estimate_messages_tokens(trimmed) > budget_tokens:
        candidates = [
            i
            for i, m in enumerate(trimmed)
            if m.get("role") == "tool" and len(m.get("content") or "") > _FORCED_CALL_RESULT_FLOOR_CHARS
        ]
        if not candidates:
            break
        largest = max(candidates, key=lambda i: len(trimmed[i]["content"]))
        content = trimmed[largest]["content"]
        trimmed[largest]["content"] = _shrink_result(content, max(_FORCED_CALL_RESULT_FLOOR_CHARS, len(content) // 2))
    return trimmed


_CORE_INSTRUCTIONS = (
    "You are Harbor Clerk, a document assistant for a local knowledge base.\n\n"
    "## How to answer questions\n\n"
    "Ground answers in the corpus whenever possible. When citing documents, "
    "always use this format: [Document Title, page X].\n\n"
    "Never fabricate document content or citations. If you search and find "
    "insufficient evidence, say so clearly rather than guessing.\n\n"
    "You may supplement with general knowledge for context or explanation, "
    "but always make corpus-sourced claims identifiable by their citations. "
    "The absence of a citation signals general knowledge — never cite a "
    "document you did not retrieve.\n\n"
    "## Choosing the right tool\n\n"
    "**Always search first.** For any question about document content — "
    "whether specific or broad — start with search_documents. "
    "It finds relevant passages across the entire corpus, even with hundreds of documents. "
    "The one exception: a question about the earliest, latest, first or last document starts with "
    "documents_by_date (below), because similarity ranking does not find an edge.\n\n"
    "**Follow-up questions need new searches.** When the user asks about a "
    "specific topic, entity, or region not covered in your previous response, "
    "always search for it — do not assume it is absent from the corpus just "
    "because it was not in earlier results. Previous searches may not have "
    "covered all relevant content.\n\n"
    "For factual questions, including ones that name a document:\n"
    "  search_documents → read_passages → expand_context if needed\n"
    "  (verify_identifier only to tell apart similarly named documents in the results)\n\n"
    'For broad or comparative questions ("compare all X", "what does the corpus say about Y"):\n'
    "  Start with corpus_overview to see what documents and topics exist, then use\n"
    "  search_documents with targeted queries based on what you find.\n"
    "  Check has_more and total_candidates in results — if many candidates exist,\n"
    "  use offset to page through or try more specific queries to find what you missed.\n\n"
    'For structural questions ("how many documents?", "what file types?"):\n'
    "  corpus_overview\n\n"
    'For chronological questions ("the earliest email about X", "the last document before Y", "the most recent"):\n'
    "  documents_by_date, with the direction and the subject — search_documents ranks by similarity and\n"
    "  will not find an edge reliably\n\n"
    "For browsing recent changes:\n"
    "  list_documents (shows a paginated subset, not the full corpus)\n\n"
    'For entity questions ("who is mentioned?", "what organizations?"):\n'
    "  entity_search, entity_overview, or entity_cooccurrence\n\n"
    "For document comparison or discovery:\n"
    "  find_related\n\n"
    "For processing status:\n"
    "  ingest_status\n\n"
    "## Context management\n\n"
    "Strongly prefer searching for specific passages over reading entire "
    "documents. The workflow search_documents → read_passages → expand_context "
    "uses far less context than read_document. Only use read_document for "
    "specific page ranges after checking document_outline — never to read "
    "a full document end-to-end.\n\n"
    "## Language\n\n"
    "Respond in the same language as the user's question."
)

SYSTEM_PROMPT = _CORE_INSTRUCTIONS

# Sentinel yielded by _iter_with_keepalive when the LLM is idle.
_KEEPALIVE_SENTINEL = object()


async def _iter_with_keepalive(aiter):
    """Wrap an async iterator, yielding ``_KEEPALIVE_SENTINEL`` every
    ``_KEEPALIVE_INTERVAL`` seconds of inactivity.

    This lets the outer generator yield SSE keepalive comments so
    Starlette can detect client disconnects during long LLM thinks.
    """
    ait = aiter.__aiter__()
    nxt: asyncio.Future | None = None
    try:
        while True:
            try:
                nxt = asyncio.ensure_future(ait.__anext__())
                # Wait for the next item, yielding keepalives while it's pending.
                # Re-use the SAME future across timeouts — never call __anext__ twice.
                while True:
                    done, _ = await asyncio.wait({nxt}, timeout=_KEEPALIVE_INTERVAL)
                    if done:
                        yield nxt.result()
                        break
                    yield _KEEPALIVE_SENTINEL
            except StopAsyncIteration:
                return
    finally:
        # A consumer that stops between items (the time budget, a disconnect) leaves the fetch pending; once
        # the response closes it would fail with nobody awaiting it ("Task exception was never retrieved").
        if nxt is not None and not nxt.done():
            nxt.cancel()


async def chat_stream(
    conversation_id: uuid.UUID,
    user_message: str,
    user_id: uuid.UUID | None = None,
) -> AsyncGenerator[str, None]:
    """Stream chat response as SSE events. Handles tool-calling loop internally.

    Creates its own DB session so it is not tied to the FastAPI DI lifecycle
    (the DI session closes when the endpoint returns, before the SSE generator
    has finished streaming).
    """
    # Pick up model changes that happened outside this process (typically
    # the menubar Preferences window writing config.json directly). Without
    # this, chat would stream against whatever model the API server cached
    # at startup — possibly a model llama-server is no longer hosting.
    refresh_llm_settings()
    settings = get_settings()
    active_model_id = settings.llm_model_id or None

    # Compute per-tool-result truncation limit from model context window.
    # Reserve ~25% of context for tool results (rest: system prompt, tools, history, response).
    # ~3.5 chars per token as a conservative estimate.
    # The context llama-server is running with, not the registry's: the
    # macOS launcher clamps -c to what fits this Mac (#556).
    model = get_model(settings.llm_model_id) if settings.llm_model_id else None
    context_tokens = context_budget(model, settings.llm_yarn_enabled)
    tool_result_max_chars = _tool_result_max_chars(context_tokens)

    async with async_session_factory() as session:
        # Save user message
        user_msg = ChatMessage(
            conversation_id=conversation_id,
            role="user",
            content=user_message,
        )
        session.add(user_msg)
        await session.flush()

        # Auto-title immediately on first message so the sidebar updates before LLM responds
        conv = await session.get(Conversation, conversation_id)
        # Build scope filter from the conversation's stored scope JSONB.
        # user_scope is None when no folder restriction is active.
        user_scope = build_user_scope(conv.scope) if conv else None
        if conv and conv.title == "New conversation":
            conv.title = _generate_title(user_message)
            await session.commit()
            # Emit title event immediately so the frontend can update the sidebar
            yield f"data: {json.dumps({'type': 'title', 'title': conv.title})}\n\n"

        # Load conversation history
        history_result = await session.execute(
            select(ChatMessage)
            .where(ChatMessage.conversation_id == conversation_id)
            .order_by(ChatMessage.created_at, ChatMessage.seq)
        )
        history_rows = history_result.scalars().all()

        # Build messages for the LLM
        from harbor_clerk.topics import get_topic_summary

        topic_hint = await get_topic_summary()
        system_content = SYSTEM_PROMPT
        if topic_hint:
            system_content += f"\n\n## Corpus topics\n{topic_hint}"
        window: list[dict] = []
        for msg in history_rows[-settings.max_history_messages :]:
            content = _truncate_for_llm(msg.content, tool_result_max_chars) if msg.role == "tool" else msg.content
            entry: dict = {"role": msg.role, "content": content}
            if msg.tool_calls:
                entry["tool_calls"] = msg.tool_calls
                if not msg.content:
                    entry["content"] = ""
            if msg.tool_call_id:
                entry["tool_call_id"] = msg.tool_call_id
            window.append(entry)
        # The window is a slice of rows, not of exchanges: it can start on a result whose call is the row before it
        # (#839). The trim repairs that only when it trims; a history that fits is sent as it is. Done here, before
        # the tool-row test below, so a window whose only result was the orphan is read as the tool-less prompt it is.
        whole = _whole_tool_exchanges(window)
        if len(whole) < len(window):
            logger.info(
                "Dropped %d history message(s) of tool exchanges that were not whole: a call or result cut off by "
                "the %d-message window, or results that do not answer their call (conversation=%s)",
                len(window) - len(whole),
                settings.max_history_messages,
                conversation_id,
            )
        messages: list[dict] = [{"role": "system", "content": system_content}, *whole]

        # The conversation's own prose ratio applies while its prose is re-measured. A history holding a tool row is
        # not (its first prompts are tool-bearing), so the entry is dropped and every read takes the model's table, as
        # before #810: kept, it would stand for the life of the process (module comment). A prompt of this turn that
        # the trim leaves tool-less is measured and may be read in the turn (the refusal retry), being its own count.
        # Once the turn has searched, the model's table too, as for the budget stop and the forced call.
        if any(m["role"] == "tool" for m in messages):
            _forget_conversation_prose_ratio(conversation_id, active_model_id)
        searched_this_turn = False  # a tool result came back in this turn (history may hold earlier turns' results)

        def turn_prose_ratio() -> float:
            if searched_this_turn:
                return _prose_ratio(active_model_id)
            return _conversation_prose_ratio(conversation_id, active_model_id)

        # Trim history to fit within context budget, at the last measured ratios of tokenizer count to estimate
        # (#805): prose at the conversation's own (#810, above), or the model's; a model not yet measured at the
        # defaults.
        messages = _trim_to_budget(
            messages, context_tokens, model, _estimate_ratio(active_model_id), turn_prose_ratio()
        )

        # Tool-calling loop — runs until the model produces a text response,
        # context budget is exhausted, or we hit the hard safety cap.
        assistant_content = ""
        # tokens_used on the stored row: the last call's prompt (it holds the whole conversation) plus every call's
        # completion, the forced call's included. A running total of prompts counts the history once per round. A
        # call cut before its usage chunk (the time budget, a deadline) leaves prompt_tokens_last one prompt stale;
        # counted_upto says how many of ``messages`` that count covered, for the prediction of the next prompt.
        prompt_tokens_last: int | None = None
        counted_upto = 0
        completion_tokens_total = 0
        completion_tokens_last = 0
        budget_exhausted = False
        overflow_retried = False  # the turn's prompt was refused as too long once and sent again trimmed (below)
        # The time budget (#719): checked between rounds and between streamed lines, so a single long
        # generation is cut too. 0 means no bound.
        time_budget = float(settings.chat_time_budget_seconds or 0)
        started_at = _now()
        deadline = started_at + time_budget if time_budget > 0 else None
        time_exhausted = False
        streamed_prefix = ""  # text the user watched arrive before a cut dropped the tool call beside it

        # Accumulated citations parsed from each tool result. Deduped+attached
        # to the assistant ChatMessage row (``rag_context`` column) and emitted
        # on the ``done`` SSE event so the UI and the test harness can surface
        # what the model cited without re-parsing the prose.
        citations_accumulated: list[dict] = []

        for _round in range(_MAX_TOOL_ROUNDS):
            # Check context budget before each LLM call, against the next prompt as predicted from the server's
            # count of the last one (#805): the previous round's usage.prompt_tokens, plus what this round appended
            # at the model's measured ratio. The stop needs a search to answer from, so it is keyed on the turn
            # having searched, not on the round: a first prompt refused and retried runs at round one with nothing
            # searched, and a retry trimmed to the floor that the prediction puts over the budget must still be sent
            # (the server, not the prediction, says whether it fits; the fifth review of #808). Prose at the model's
            # table: the stop acts only once the turn has searched, when the conversation's own ratio no longer applies.
            estimate_ratio = _estimate_ratio(active_model_id)
            prose_ratio = _prose_ratio(active_model_id)
            predicted = _predict_prompt_tokens(
                messages, prompt_tokens_last, counted_upto, _get_tool_schema_tokens(model), estimate_ratio, prose_ratio
            )
            usage_frac = predicted / context_tokens if context_tokens > 0 else 0.0
            if usage_frac >= _TOOL_LOOP_BUDGET and searched_this_turn:
                budget_exhausted = True
                logger.info(
                    "Context budget %.0f%% >= %.0f%% after %d tool rounds (%d tokens predicted from the server's "
                    "%s, results at %.2fx, prose at %.2fx), forcing text response (conversation=%s)",
                    usage_frac * 100,
                    _TOOL_LOOP_BUDGET * 100,
                    _round,
                    predicted,
                    prompt_tokens_last if prompt_tokens_last is not None else "estimate",
                    estimate_ratio,
                    prose_ratio,
                    conversation_id,
                )
                break
            if deadline is not None and _round > 0 and _now() >= deadline:
                time_exhausted = True
                logger.info(
                    "Time budget of %.0fs spent after %d tool rounds, forcing text response (conversation=%s)",
                    time_budget,
                    _round,
                    conversation_id,
                )
                break

            tool_calls_accumulated: list[dict] = []
            text_buffer = ""

            # Every call of the loop carries the tool definitions: the budget stop above is what ends the search, and
            # the first call of a conversation must carry them whatever its history (the trim leaves room for them,
            # _TRIM_BUDGET_FRACTION). Omitting them when the first prompt was long told the model to search first
            # with nothing to search with, and it answered from the history.
            send_tools = get_chat_tools(model=model)

            try:
                payload: dict = {
                    "messages": messages,
                    "stream": True,
                    # The server's count of this prompt, in the stream's last chunk, calibrates the estimate (#805).
                    "stream_options": {"include_usage": True},
                    "temperature": 0.3,
                }
                if send_tools:
                    payload["tools"] = send_tools
                sent_estimate = _estimate_payload_tokens(messages, send_tools)
                sent_count = len(messages)
                sent_tool_bearing = any(m.get("role") == "tool" for m in messages)

                llm_url = f"{settings.llama_server_url}/v1/chat/completions"

                # Retry once on 5xx — llama-server can transiently 500 on
                # MoE compute errors or KV cache issues.
                response_obj = None
                client_obj = None
                for _attempt in range(2):
                    client_obj = httpx.AsyncClient(timeout=httpx.Timeout(120.0))
                    response_obj = await client_obj.send(
                        client_obj.build_request("POST", llm_url, json=payload),
                        stream=True,
                    )
                    if response_obj.status_code < 500 or _attempt == 1:
                        break
                    # 5xx on first attempt — close, wait, retry
                    await response_obj.aclose()
                    await client_obj.aclose()
                    report_llm_error(response_obj.status_code)
                    logger.warning("LLM returned %d, retrying in 2s", response_obj.status_code)
                    await asyncio.sleep(2)

                try:
                    response = response_obj

                    if response.status_code >= 400:
                        await response.aread()
                        detail = response.text[:2000]
                        if response.status_code >= 500:
                            report_llm_error(response.status_code)

                        # Detect context overflow from llama-server
                        is_context_overflow = _is_context_overflow(response.status_code, detail)
                        counted = _refused_prompt_tokens(detail) if is_context_overflow else None
                        if counted and counted < _ESTIMATE_RATIO_MIN * sent_estimate:
                            # Below any density the clamp allows, as in the accepted path: not believed, so the
                            # refusal is treated as one that names no count.
                            logger.warning(
                                "Refusal counted the prompt at %d tokens against %d estimated, under %.1fx: count "
                                "distrusted (conversation=%s)",
                                counted,
                                sent_estimate,
                                _ESTIMATE_RATIO_MIN,
                                conversation_id,
                            )
                            counted = None
                        if is_context_overflow and counted and not sent_tool_bearing:
                            # A tool-less prompt's count is the conversation's prose density, exactly (module comment).
                            _record_conversation_prose_ratio(conversation_id, active_model_id, counted, sent_estimate)
                        server_context = _refused_context_tokens(detail) if is_context_overflow else None
                        if server_context and server_context < context_tokens:
                            # The slot the server runs at, which the registry's window need not be (#654): the rest
                            # of the turn, the budget stop and the forced call's retry included, fits that one.
                            context_tokens = server_context
                        if is_context_overflow and searched_this_turn:
                            # The prompt holds this turn's results. The search ends here as it does at the context
                            # budget: the forced call's count-calibrated retry shrinks the results and keeps every
                            # user and assistant message whole (``_trim_for_forced_call``). The history trim below
                            # would not: once the turn has searched the last message is the latest tool row, which
                            # it keeps as the "question" while it pops the real one and the call the row answers
                            # (the third review of #808). Reachable when the server's slot is smaller than the
                            # registry's window (#654) or when light increments have dragged the tool ratio low.
                            # Keyed on the turn having searched, not on the round: a first prompt retried below runs
                            # at round one, and refused again it is the error, not a forced answer to a search that
                            # never happened. The count is an increment like any other, and the forced call's
                            # deadline reads from it.
                            if counted:
                                if prompt_tokens_last is not None:
                                    _measure_increment(
                                        active_model_id, counted, prompt_tokens_last, messages[counted_upto:sent_count]
                                    )
                                prompt_tokens_last, counted_upto = counted, sent_count
                            logger.info(
                                "Prompt refused as too long in tool round %d (%s tokens by the server against %d "
                                "estimated); ending the search and forcing the answer (conversation=%s)",
                                _round + 1,
                                counted if counted else "unknown",
                                sent_estimate,
                                conversation_id,
                            )
                            budget_exhausted = True
                            break
                        if is_context_overflow and not overflow_retried:
                            # A turn's first prompt is refused when the history was trimmed at a density below the
                            # tokenizer's (a 1.8x prose history read at the 1.2 seed, the review of #808). The
                            # refusal is immediate and names its count, recorded above when the prompt had no tool
                            # row, and the turn is sent once more with the history trimmed against the count and the
                            # slot the refusal states, as the forced call's retry is. Telling the user to start a new
                            # conversation sent the next one to the same refusal. The retry takes a round of the cap,
                            # and it is the one retry: a second refusal is the error below. Without a count, the
                            # weights are doubled, which halves the history. Before the turn has searched the last
                            # message is the user's question, which the trim keeps.
                            tool_ratio = _estimate_ratio(active_model_id)
                            prose_ratio = turn_prose_ratio()
                            if counted:
                                at_least = counted / sent_estimate * _FORCED_CALL_RETRY_SAFETY
                                tool_ratio, prose_ratio = max(tool_ratio, at_least), max(prose_ratio, at_least)
                            else:
                                tool_ratio, prose_ratio = 2.0 * tool_ratio, 2.0 * prose_ratio
                            shorter = _trim_to_budget(messages, context_tokens, model, tool_ratio, prose_ratio)
                            if len(shorter) < len(messages):
                                logger.info(
                                    "Prompt refused as too long (%s tokens by the server against %d estimated, "
                                    "context %s); retrying with %d of %d messages (conversation=%s)",
                                    counted if counted else "unknown",
                                    sent_estimate,
                                    server_context if server_context else "unknown",
                                    len(shorter),
                                    len(messages),
                                    conversation_id,
                                )
                                messages = shorter
                                overflow_retried = True
                                # Nothing has been counted yet before the turn has searched; kept explicit so a
                                # retry moved later could not read a count of the untrimmed list.
                                prompt_tokens_last, counted_upto = None, 0
                                continue
                        if is_context_overflow:
                            error_summary = "Context window full — please start a new conversation"
                        else:
                            error_summary = server_unready_message(
                                f"LLM error ({response.status_code})", status_code=response.status_code
                            )
                        error_content = f"Error: {error_summary}"
                        if detail:
                            error_content += f"\n\n{detail}"
                        session.add(
                            ChatMessage(
                                conversation_id=conversation_id,
                                role="assistant",
                                content=error_content,
                                model_id=active_model_id,
                                context_pct=100 if is_context_overflow else None,
                            )
                        )
                        conv = await session.get(Conversation, conversation_id)
                        if conv and conv.title == "New conversation":
                            conv.title = _generate_title(user_message)
                        await session.commit()
                        error_event: dict = {"type": "error", "message": error_summary}
                        if detail:
                            error_event["detail"] = detail
                        yield f"data: {json.dumps(error_event)}\n\n"
                        early_cites = dedupe_citations(citations_accumulated)
                        done_payload: dict = {
                            "type": "done",
                            "context_pct": 100 if is_context_overflow else None,
                            "rag_context": {"citations": early_cites} if early_cites else None,
                        }
                        if conv and conv.title != "New conversation":
                            done_payload["title"] = conv.title
                        if active_model_id:
                            done_payload["model_id"] = active_model_id
                        yield f"data: {json.dumps(done_payload)}\n\n"
                        return

                    report_llm_success()
                    call_usage: dict | None = None  # the last usage the stream carried: read once, after it
                    # Keepalives are keyed to the client's silence, not the model's (#731): a tool call or a
                    # model's thinking streams lines the client hears nothing of, for minutes at a time.
                    last_sent = _now()
                    # aclosing: leaving this loop with `break` must close the iterator now, not when the garbage
                    # collector gets to it, so its pending fetch is cancelled before the response is closed.
                    async with aclosing(_iter_with_keepalive(response.aiter_lines())) as lines:
                        async for line in lines:
                            if deadline is not None:
                                # One generation can outlast the whole budget (a 26B model at 11 tokens a
                                # second wrote for ten minutes at a stretch, #719). Closing the stream, in
                                # the finally below, is what stops llama-server decoding. A tool call being
                                # written is cut at the deadline; an answer already being written gets the
                                # same grace a forced answer would, and its "[DONE]" is let through, as are
                                # the finish and usage chunks before it: nothing was cut. A tool call's
                                # "[DONE]" is not: one that completes as the budget runs out is not searched
                                # either.
                                # Blank content is not an answer being written: a reasoning parser commonly
                                # streams "\n\n" as content once it has stripped a closing think tag.
                                writing_answer = bool(text_buffer.strip()) and not (
                                    tool_calls_accumulated and tool_calls_accumulated[0]["function"]["name"]
                                )
                                answer_done = writing_answer and isinstance(line, str) and _line_carries_no_token(line)
                                cut_at = deadline + _FINAL_ANSWER_GRACE_SECONDS if writing_answer else deadline
                                if not answer_done and _now() >= cut_at:
                                    time_exhausted = True
                                    break
                            if line is _KEEPALIVE_SENTINEL:
                                yield ": keepalive\n\n"
                                last_sent = _now()
                                continue
                            if not line.startswith("data: "):
                                continue
                            data = line[6:]
                            if data.strip() == "[DONE]":
                                break

                            try:
                                chunk = json.loads(data)
                            except json.JSONDecodeError:
                                continue

                            # The usage chunk has no choices at all (an empty array, per the OpenAI spec).
                            choices = chunk.get("choices") or [{}]
                            delta = choices[0].get("delta", {})
                            if chunk.get("usage"):
                                call_usage = chunk["usage"]

                            # Accumulate tool calls from deltas
                            if "tool_calls" in delta:
                                for tc in delta["tool_calls"]:
                                    idx = tc.get("index", 0)
                                    while len(tool_calls_accumulated) <= idx:
                                        tool_calls_accumulated.append(
                                            {
                                                "id": "",
                                                "type": "function",
                                                "function": {
                                                    "name": "",
                                                    "arguments": "",
                                                },
                                            }
                                        )
                                    if "id" in tc and tc["id"]:
                                        tool_calls_accumulated[idx]["id"] = tc["id"]
                                    fn = tc.get("function", {})
                                    if "name" in fn and fn["name"]:
                                        tool_calls_accumulated[idx]["function"]["name"] = fn["name"]
                                    if "arguments" in fn:
                                        tool_calls_accumulated[idx]["function"]["arguments"] += fn["arguments"]

                            # Stream text tokens
                            if "content" in delta and delta["content"]:
                                token_text = delta["content"]
                                text_buffer += token_text
                                yield f"data: {json.dumps({'type': 'token', 'content': token_text})}\n\n"
                                last_sent = _now()
                            elif _now() - last_sent >= _KEEPALIVE_INTERVAL:
                                yield ": keepalive\n\n"
                                last_sent = _now()
                    if call_usage:
                        # Once per call, from the last chunk that carried usage: a build that puts usage on the
                        # finish chunk as well must not count twice.
                        completion_tokens = call_usage.get("completion_tokens")
                        if isinstance(completion_tokens, int):
                            completion_tokens_total += completion_tokens
                            completion_tokens_last = completion_tokens
                        counted = call_usage.get("prompt_tokens")
                        if isinstance(counted, int):
                            if counted < _ESTIMATE_RATIO_MIN * sent_estimate:
                                # Below any density the clamp allows: a build counting something else. Not kept;
                                # the next prompt is predicted from the weighted estimate alone.
                                logger.warning(
                                    "Prompt counted at %d tokens against %d estimated, under %.1fx: count distrusted "
                                    "(conversation=%s)",
                                    counted,
                                    sent_estimate,
                                    _ESTIMATE_RATIO_MIN,
                                    conversation_id,
                                )
                                prompt_tokens_last, counted_upto = None, 0
                            else:
                                if not sent_tool_bearing:
                                    # No tool row anywhere in it: the prose-and-schema density, exactly.
                                    prose_now = _record_conversation_prose_ratio(
                                        conversation_id, active_model_id, counted, sent_estimate
                                    )
                                    logger.debug(
                                        "Prose prompt counted at %d tokens against %d estimated; conversation's prose "
                                        "ratio now %.2f, model's table %.2f (model=%s, conversation=%s)",
                                        counted,
                                        sent_estimate,
                                        prose_now if prose_now is not None else float("nan"),
                                        _prose_ratio(active_model_id),
                                        active_model_id,
                                        conversation_id,
                                    )
                                elif prompt_tokens_last is not None:
                                    # The increment over the last counted prompt of this turn: the tool result and
                                    # the call that asked for it, nothing else (module comment). A turn's first
                                    # counted prompt, holding the history's results, measures nothing.
                                    _measure_increment(
                                        active_model_id, counted, prompt_tokens_last, messages[counted_upto:sent_count]
                                    )
                                prompt_tokens_last, counted_upto = counted, sent_count
                finally:
                    await response_obj.aclose()
                    await client_obj.aclose()

            except httpx.TransportError as exc:
                # Connect, timeout, or a reset mid-stream when the server is swapped or crashes while writing: to
                # the caller these are one event, the server is gone. Anything narrower let a ReadError out of
                # the generator, so the stream dropped with no error event and no stored message (#690).
                error_summary = server_unready_message(
                    "LLM server is not running. Select and activate a model in Settings.", exc=exc
                )
                session.add(
                    ChatMessage(
                        conversation_id=conversation_id,
                        role="assistant",
                        content=f"Error: {error_summary}",
                        model_id=active_model_id,
                    )
                )
                conv = await session.get(Conversation, conversation_id)
                if conv and conv.title == "New conversation":
                    conv.title = _generate_title(user_message)
                await session.commit()
                yield f"data: {json.dumps({'type': 'error', 'message': error_summary})}\n\n"
                early_cites = dedupe_citations(citations_accumulated)
                done_payload: dict = {"type": "done"}
                if early_cites:
                    done_payload["rag_context"] = {"citations": early_cites}
                if conv and conv.title != "New conversation":
                    done_payload["title"] = conv.title
                if active_model_id:
                    done_payload["model_id"] = active_model_id
                yield f"data: {json.dumps(done_payload)}\n\n"
                return

            if time_exhausted:
                # Cut mid-generation. Text the user already watched arrive is the answer, or the start of it
                # when a tool call was being written beside it: that call is dropped and the rest of the
                # answer is forced from what the earlier rounds found, after the text the user saw.
                logger.info(
                    "Time budget of %.0fs spent during tool round %d, stopping the search (conversation=%s)",
                    time_budget,
                    _round + 1,
                    conversation_id,
                )
                if text_buffer.strip() and not (
                    tool_calls_accumulated and tool_calls_accumulated[0]["function"]["name"]
                ):
                    assistant_content = text_buffer
                else:
                    streamed_prefix = text_buffer
                break

            # If we got tool calls, execute them and loop
            if tool_calls_accumulated and tool_calls_accumulated[0]["function"]["name"]:
                # Save assistant tool-call message
                tc_msg = ChatMessage(
                    conversation_id=conversation_id,
                    role="assistant",
                    content="",
                    tool_calls=tool_calls_accumulated,
                )
                session.add(tc_msg)
                messages.append(
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": tool_calls_accumulated,
                    }
                )

                for tc in tool_calls_accumulated:
                    fn_name = tc["function"]["name"]
                    try:
                        fn_args = json.loads(tc["function"]["arguments"])
                    except json.JSONDecodeError:
                        fn_args = {}

                    yield f"data: {json.dumps({'type': 'tool_call', 'name': fn_name, 'arguments': fn_args})}\n\n"

                    result_str = await execute_tool(fn_name, fn_args, user_id, user_scope=user_scope)

                    # Capture citations parsed from this tool result. Best-effort:
                    # tools that don't return citation-shaped JSON contribute nothing.
                    # Dedup happens later, after the whole tool loop, so the same
                    # chunk surfacing in two searches collapses to one cite.
                    citations_accumulated.extend(extract_citations_from_tool_result(result_str))

                    yield f"data: {json.dumps({'type': 'tool_result', 'name': fn_name, 'summary': summarize_tool_result(result_str), 'raw_result': result_str})}\n\n"

                    # Save full result to DB, but truncate for LLM context
                    tool_msg = ChatMessage(
                        conversation_id=conversation_id,
                        role="tool",
                        content=result_str,
                        tool_call_id=tc.get("id", f"call_{fn_name}"),
                    )
                    session.add(tool_msg)
                    searched_this_turn = True
                    truncated_result = _truncate_for_llm(result_str, tool_result_max_chars)
                    messages.append(
                        {
                            "role": "tool",
                            "content": truncated_result,
                            "tool_call_id": tc.get("id", f"call_{fn_name}"),
                        }
                    )

                await session.flush()
                continue  # Next round — let LLM see tool results

            # No tool calls — we have the final text response
            assistant_content = text_buffer
            break

        # If the loop ended without a text response (budget exhausted or
        # hard cap hit), make one final LLM call without tools to force
        # a text response from whatever context we have.
        stop_reason: str | None = None
        if time_exhausted:
            stop_reason = "time_budget"
        elif budget_exhausted:
            stop_reason = "context_budget"
        elif not assistant_content and _round == _MAX_TOOL_ROUNDS - 1:
            stop_reason = "tool_rounds"
        server_failure: str | None = None  # how the forced call itself failed, when it did
        thought_cut_short = False  # the forced answer is a thought cut short, under the app's lead
        produced_nothing = not assistant_content.strip()
        if produced_nothing and stop_reason:
            reason = {
                "time_budget": f"time budget of {time_budget:.0f}s",
                "context_budget": "context budget",
                "tool_rounds": f"{_MAX_TOOL_ROUNDS} tool rounds",
            }[stop_reason]
            logger.info("Forcing final text response after %s (conversation=%s)", reason, conversation_id)
            # The forced answer is bounded too, or the bound is not one. Both inner clocks start from the model's
            # tokens, not from the request: the prompt is re-read in full first (no tools this time, so the prompt
            # cache misses), and on a full context that read is minutes on a slow model, in silence. A keepalive
            # sentinel is our silence, not the model's token: it starts no clock, though a clock already running is
            # checked on it, so a model that falls silent past its grace is cut then. (The client's read timeout of
            # 120 s is a third bound on that silence, older than both.) The thinking bound runs from the model's
            # first token, the grace from its first content token (#742). The outer deadline runs from the moment
            # the search stopped, allowing for the read (#773), and is checked wherever the others are: a model
            # whose first token never comes is cut at a keepalive. A line carrying no token, the finish chunk or
            # "[DONE]", is let through whenever it arrives: it cuts nothing, and cutting it would turn a finished
            # answer into none.
            stopped_at = _now()
            thinking_deadline: float | None = None
            final_deadline: float | None = None
            outer_deadline: float | None = None
            cut: str | None = None
            reasoning_buffer = ""  # the model's reasoning channel: the answer when no content comes (below)
            finish_reason: str | None = None  # "stop" when the model ended its answer, "length" when the server did
            # This turn's results, not the history's: an earlier turn's tool rows are in ``messages`` too, and
            # "the results above" would point the model at them.
            search_over = _SEARCH_OVER_MESSAGE if searched_this_turn else _SEARCH_OVER_UNSEARCHED_MESSAGE
            # The prompt goes as it stands (#774): the estimate runs at half the tokenizer's count on tool results,
            # but trimming on the estimate alone would discard evidence from every forced call to fit the ones the
            # server would refuse. The server refuses before it reads and says how many tokens it counted, so a
            # refused prompt is trimmed against that count and sent once more (below); the rows the conversation
            # keeps are untouched either way.
            forced_messages = [*messages, {"role": "user", "content": search_over}]
            forced_estimate = _estimate_messages_tokens(forced_messages)
            # What the outer deadline reads the prompt at: predicted from the server's last count (#805), which held
            # the tools this prompt does not, so the allowance errs long, not short. Prose by the turn's rule: the
            # model's table once the turn has searched, the conversation's own for a time budget spent before any
            # result.
            deadline_prediction = _predict_prompt_tokens(
                forced_messages,
                prompt_tokens_last,
                counted_upto,
                0,
                _estimate_ratio(active_model_id),
                turn_prose_ratio(),
            )
            if deadline is not None:
                outer_deadline = _forced_call_outer_deadline(stopped_at, deadline_prediction)
            for attempt in range(2):
                retry_shorter = False  # the server refused the prompt as too long: trim harder and send once more
                final_payload = {
                    "messages": forced_messages,
                    "stream": True,
                    "stream_options": {"include_usage": True},
                    "temperature": 0.3,
                    # No thinking before the answer: llama-server reads ``enable_thinking`` for the templates that
                    # take it (Qwen3, Gemma 4) and passes ``reasoning_effort`` to gpt-oss's, which thinks regardless
                    # and reasons least at "low". Not "none": at the pin (b29c606) that turns enable_thinking off
                    # for every template and erases the reasoning_effort kwarg, so gpt-oss would get neither hint.
                    # Templates that know neither ignore both.
                    "chat_template_kwargs": {"enable_thinking": False},
                    "reasoning_effort": "low",
                }
                try:
                    async with (
                        httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client,
                        client.stream(
                            "POST",
                            f"{settings.llama_server_url}/v1/chat/completions",
                            json=final_payload,
                        ) as response,
                    ):
                        if response.status_code >= 400:
                            await response.aread()
                            detail = response.text[:2000]
                            if response.status_code >= 500:
                                report_llm_error(response.status_code)
                            server_failure = f"HTTP {response.status_code}"
                            if attempt == 0 and _is_context_overflow(response.status_code, detail):
                                # The refusal is immediate (the server counts before it reads) and names its
                                # count, so the second trim is calibrated: the estimate is scaled by the server's
                                # count of the prompt it refused, with a safety; a refusal without a count halves
                                # the prompt. Either way the prompt gets shorter, or there is no point sending it.
                                counted = _refused_prompt_tokens(detail)
                                # The context to fit is the server's, when the refusal states it: the client's
                                # figure is the registry's window, which can be 32,768 while the server runs at
                                # 16,384 (#654), and a retry calibrated to the larger one is refused again.
                                server_context = _refused_context_tokens(detail)
                                fits_in = min(context_tokens, server_context) if server_context else context_tokens
                                harder = forced_estimate // 2  # no count, or one that would trim nothing: halve
                                if counted:
                                    ratio = counted / max(1, forced_estimate)
                                    calibrated = int(
                                        fits_in * (1 - _RESPONSE_RESERVE) / (ratio * _FORCED_CALL_RETRY_SAFETY)
                                    )
                                    if calibrated < forced_estimate:
                                        harder = calibrated
                                shorter = _trim_for_forced_call(forced_messages, harder)
                                retried_estimate = _estimate_messages_tokens(shorter)
                                if retried_estimate < forced_estimate:
                                    logger.info(
                                        "Forced final prompt refused as too long (%s tokens by the server against "
                                        "%d estimated, context %s); retrying at %d estimated tokens "
                                        "(conversation=%s)",
                                        counted if counted else "unknown",
                                        forced_estimate,
                                        server_context if server_context else "unknown",
                                        retried_estimate,
                                        conversation_id,
                                    )
                                    forced_messages = shorter
                                    forced_estimate = retried_estimate
                                    server_failure = None
                                    retry_shorter = True
                            if not retry_shorter:
                                logger.warning(
                                    "Forced final answer failed: %s %s (conversation=%s)",
                                    server_failure,
                                    detail[:500],
                                    conversation_id,
                                )
                        else:
                            last_sent = _now()
                            forced_usage: dict | None = None  # the last usage the stream carried: read once, after
                            async with aclosing(_iter_with_keepalive(response.aiter_lines())) as lines:
                                async for line in lines:
                                    if line is _KEEPALIVE_SENTINEL:
                                        if deadline is not None:
                                            cut = _forced_answer_cut(
                                                _now(), thinking_deadline, final_deadline, outer_deadline
                                            )
                                            if cut:
                                                break
                                        yield ": keepalive\n\n"
                                        last_sent = _now()
                                        continue
                                    if not line.startswith("data: "):
                                        continue
                                    data = line[6:]
                                    if data.strip() == "[DONE]":
                                        break
                                    try:
                                        chunk = json.loads(data)
                                    except json.JSONDecodeError:
                                        continue
                                    if chunk.get("usage"):
                                        forced_usage = chunk["usage"]
                                    choice = (chunk.get("choices") or [{}])[0]
                                    finish_reason = choice.get("finish_reason") or finish_reason
                                    delta = choice.get("delta", {})
                                    token = delta.get("content") or ""
                                    thought = delta.get("reasoning_content") or ""
                                    if not token and not thought:
                                        continue
                                    if deadline is not None:
                                        if thinking_deadline is None:
                                            thinking_deadline = _now() + _FINAL_ANSWER_THINKING_SECONDS
                                        cut = _forced_answer_cut(
                                            _now(), thinking_deadline, final_deadline, outer_deadline
                                        )
                                        if cut:
                                            break
                                        if token and final_deadline is None:
                                            final_deadline = _now() + _FINAL_ANSWER_GRACE_SECONDS
                                    if token:
                                        assistant_content += token
                                        yield f"data: {json.dumps({'type': 'token', 'content': token})}\n\n"
                                        last_sent = _now()
                                    else:
                                        reasoning_buffer += thought
                                        if _now() - last_sent >= _KEEPALIVE_INTERVAL:
                                            yield ": keepalive\n\n"
                                            last_sent = _now()
                            if forced_usage:
                                # Bookkeeping, not a measurement (module comment): the forced prompt dropped the
                                # tools, so its count against the last is no increment. The count feeds tokens_used
                                # and the UI percentage; counted_upto is not advanced, since no prediction follows
                                # the forced call and the count is of a prompt ``messages`` does not hold (the
                                # search-over line, and after a refusal the shrunk results of the retry). A count
                                # below the clamp's floor times the estimate is distrusted here as in the loop.
                                completion_tokens = forced_usage.get("completion_tokens")
                                if isinstance(completion_tokens, int):
                                    completion_tokens_total += completion_tokens
                                    completion_tokens_last = completion_tokens
                                counted = forced_usage.get("prompt_tokens")
                                if isinstance(counted, int) and counted >= _ESTIMATE_RATIO_MIN * forced_estimate:
                                    prompt_tokens_last = counted
                            if cut == "grace":
                                logger.info(
                                    "Final answer cut after %.0fs of grace (conversation=%s)",
                                    _FINAL_ANSWER_GRACE_SECONDS,
                                    conversation_id,
                                )
                            elif cut == "thinking":
                                logger.info(
                                    "Final answer cut: no content %.0fs after the model's first token, %d chars of "
                                    "reasoning (conversation=%s)",
                                    _FINAL_ANSWER_THINKING_SECONDS,
                                    len(reasoning_buffer),
                                    conversation_id,
                                )
                            elif cut == "outer":
                                logger.info(
                                    "Final answer cut at its outer bound, %.0fs after the search stopped (%d "
                                    "predicted prompt tokens at %.0f tokens/s, the thinking bound and the grace); "
                                    "%d chars of content, %d of reasoning (conversation=%s)",
                                    _now() - stopped_at,
                                    deadline_prediction,
                                    _PREFILL_TOKENS_PER_SECOND,
                                    len(assistant_content),
                                    len(reasoning_buffer),
                                    conversation_id,
                                )
                except httpx.TransportError as exc:
                    # A swap or a crash mid-answer resets the connection (#690): that is a lost server too, not a
                    # model that wrote nothing.
                    if isinstance(exc, httpx.TimeoutException):
                        server_failure = "timed out"
                    elif isinstance(exc, httpx.ConnectError):
                        server_failure = "unreachable"
                    else:
                        server_failure = "reset the connection"
                    logger.warning(
                        "Forced final answer failed: the model server %s (%s) (conversation=%s)",
                        server_failure,
                        type(exc).__name__,
                        conversation_id,
                    )
                if not retry_shorter:
                    break
            if not assistant_content.strip() and reasoning_buffer.strip():
                # gpt-oss routes its whole answer through the reasoning channel even when asked not to think, and a
                # model that thinks regardless has only that channel to show for its time (research.py's streaming
                # path does the same). What it wrote there is shown: saying the model wrote nothing would be false.
                # An answer the model finished there is a plain answer. A thought cut short, by our bounds or by the
                # server's context, is shown under the app's lead, so it is not read (or judged) as an answer.
                # Streamed live it would have shown thinking as the answer while content might still follow, so it
                # arrives now, in one piece.
                logger.info(
                    "Final answer came as %d chars of reasoning_content and no content (finish_reason=%s, cut=%s) "
                    "(conversation=%s)",
                    len(reasoning_buffer),
                    finish_reason,
                    cut,
                    conversation_id,
                )
                if cut is not None:
                    assistant_content = _cut_thought_answer(reasoning_buffer, "time")
                    thought_cut_short = True
                elif finish_reason == "length":
                    assistant_content = _cut_thought_answer(reasoning_buffer, "context")
                    thought_cut_short = True
                else:
                    assistant_content = reasoning_buffer.strip()
                yield f"data: {json.dumps({'type': 'token', 'content': assistant_content})}\n\n"
            produced_nothing = not assistant_content.strip()
            if streamed_prefix:
                # Whatever the forced call produced, or did not: the stored message begins with what was watched.
                assistant_content = streamed_prefix + assistant_content

        # No answer where one was due: the app says so in its own words, after any text the user watched arrive.
        # The sentence is not the model's, so the note that the model "answered from what it had found" is not
        # appended to it, nor to a thought cut short, whose lead already says the time ran out.
        if produced_nothing:
            fallback = _nothing_produced_fallback(stop_reason, server_failure)
            if assistant_content.strip():
                fallback = "\n\n" + fallback
                assistant_content += fallback
            else:
                assistant_content = fallback
            yield f"data: {json.dumps({'type': 'token', 'content': fallback})}\n\n"
        if time_exhausted and not produced_nothing and not thought_cut_short:
            # The answer says it stopped early, in the text the user keeps (#719).
            note = _stopped_early_note(_now() - started_at)
            assistant_content += note
            yield f"data: {json.dumps({'type': 'token', 'content': note})}\n\n"

        # Context usage for the UI indicator: the server's count of the last prompt and its completion when usage
        # came (one prompt stale when the last call was cut before its usage chunk); otherwise (no usage, a
        # distrusted count, the only call cut before its usage chunk) the prediction with no count, the response at
        # the prose ratio (#805).
        if prompt_tokens_last is not None:
            used_tokens: float = prompt_tokens_last + completion_tokens_last
        else:
            tool_ratio = _estimate_ratio(active_model_id)
            prose_ratio = turn_prose_ratio()
            used_tokens = _predict_prompt_tokens(
                messages, None, 0, _get_tool_schema_tokens(model), tool_ratio, prose_ratio
            )
            used_tokens += _estimate_tokens(assistant_content) * prose_ratio if assistant_content else 0
        context_pct = round(min(used_tokens / context_tokens, 1.0) * 100) if context_tokens > 0 else 0

        # Save assistant response
        final_citations = dedupe_citations(citations_accumulated)
        rag_context_payload = {"citations": final_citations} if final_citations else None
        assistant_msg = None
        if assistant_content:
            assistant_msg = ChatMessage(
                conversation_id=conversation_id,
                role="assistant",
                content=assistant_content,
                tokens_used=(prompt_tokens_last + completion_tokens_total) if prompt_tokens_last is not None else None,
                rag_context=rag_context_payload,
                model_id=active_model_id,
                context_pct=context_pct,
            )
            session.add(assistant_msg)

        # Auto-title if this is the first exchange
        conv = await session.get(Conversation, conversation_id)
        if conv and conv.title == "New conversation":
            conv.title = _generate_title(user_message)

        await session.commit()

        done_payload: dict = {
            "type": "done",
            "message_id": str(assistant_msg.message_id) if assistant_msg else None,
            "context_pct": context_pct,
            "rag_context": rag_context_payload,
        }
        if stop_reason:
            # Why the search stopped before the model chose to answer: "time_budget", "context_budget" or
            # "tool_rounds". Absent when the model answered on its own. The eval harness records a bounded
            # answer as degraded rather than as a plain completion.
            done_payload["stop_reason"] = stop_reason
        if conv and conv.title != "New conversation":
            done_payload["title"] = conv.title
        if active_model_id:
            done_payload["model_id"] = active_model_id
        yield f"data: {json.dumps(done_payload)}\n\n"


def _generate_title(user_message: str) -> str:
    """Generate a short title from the first user message."""
    title = user_message.strip()
    if len(title) > 80:
        title = title[:77] + "..."
    return title


# Arrays a tool result may be cut by, whole rows at a time; "candidates" is kb_verify_identifier's (#831).
_LARGE_ARRAY_KEYS = ("documents", "results", "entities", "related", "passages", "chunks", "headings", "candidates")


def _without_snippet(row: Any) -> Any:
    """A copy of a kb_find_all brief row without its ``snippet`` and the
    ``source.chunk_id`` it brings (only brief rows carry a snippet; a full row
    is returned as it is)."""
    if not isinstance(row, dict) or "snippet" not in row:
        return row
    row = {k: v for k, v in row.items() if k != "snippet"}
    if isinstance(row.get("source"), dict):
        row["source"] = {k: v for k, v in row["source"].items() if k != "chunk_id"}
    return row


def _fit_find_all_snippets(data: dict, max_chars: int) -> str | None:
    """A kb_find_all result (#820) fitted to ``max_chars`` by its snippets alone,
    or None when it does not fit even without any; ``data["results"]`` is left
    without snippets in that case, for the row cut that follows.

    A snippet is triage help, a row is a document the enumeration found, so the
    snippets go before any row. First the compact form (the tool's indented,
    ASCII-escaped string often fits once compacted, with every snippet); then
    snippets come off the lowest-ranked rows first, as few as fit. Other
    results, including any future tool whose rows carry a ``snippet``, are not
    touched: the result must carry find-all's ``total_matches`` and
    ``presentation``."""
    rows = data.get("results")
    if not ("total_matches" in data and "presentation" in data and isinstance(rows, list)):
        return None
    if not any(isinstance(row, dict) and "snippet" in row for row in rows):
        return None
    compact = json.dumps(data, ensure_ascii=False)
    if len(compact) <= max_chars:
        return compact
    bare = [_without_snippet(row) for row in rows]
    # Smallest k (rows stripped from the tail) that fits; the length falls as k grows.
    lo, hi = 1, len(rows)
    best: str | None = None
    while lo <= hi:
        k = (lo + hi) // 2
        data["results"] = rows[: len(rows) - k] + bare[len(rows) - k :]
        candidate = json.dumps(data, ensure_ascii=False)
        if len(candidate) <= max_chars:
            best, hi = candidate, k - 1
        else:
            lo = k + 1
    data["results"] = rows if best is not None else bare
    return best


def _truncate_for_llm(result_str: str, max_chars: int = 28_000) -> str:
    """Truncate a tool result so it fits within the LLM context window.

    For JSON with large array fields, truncates the array and adds metadata.
    For non-JSON or unrecognised structure, hard-truncates the string.
    """
    if len(result_str) <= max_chars:
        return result_str

    try:
        data = json.loads(result_str)
    except (json.JSONDecodeError, TypeError):
        return result_str[:max_chars] + f"\n... [truncated — {len(result_str)} chars total]"

    if not isinstance(data, dict):
        return result_str[:max_chars] + f"\n... [truncated — {len(result_str)} chars total]"

    # Find-all snippets go before any row (#820).
    fitted = _fit_find_all_snippets(data, max_chars)
    if fitted is not None:
        return fitted

    # Find the largest array field to truncate
    target_key = None
    target_len = 0
    for key in _LARGE_ARRAY_KEYS:
        if key in data and isinstance(data[key], list) and len(data[key]) > target_len:
            target_key = key
            target_len = len(data[key])

    if not target_key or target_len == 0:
        return result_str[:max_chars] + f"\n... [truncated — {len(result_str)} chars total]"

    # Binary-search for how many items fit
    original_array = data[target_key]
    original_count = len(original_array)
    data["_truncated"] = True
    # A result cut once already (the forced call's trim re-cuts the loop's) keeps the count it started with.
    data["_original_count"] = data.get("_original_count", original_count)
    lo, hi = 0, original_count
    while lo < hi:
        mid = (lo + hi + 1) // 2
        data[target_key] = original_array[:mid]
        if len(json.dumps(data, ensure_ascii=False)) <= max_chars:
            lo = mid
        else:
            hi = mid - 1

    data[target_key] = original_array[:lo] if lo > 0 else []
    result = json.dumps(data, ensure_ascii=False)
    if len(result) > max_chars:
        return result_str[:max_chars] + f"\n... [truncated — {len(result_str)} chars total]"
    return result

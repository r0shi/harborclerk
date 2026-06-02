"""Stage-1 verifier loop — unit tests.

Covers the pure-Python helpers (`_excerpt_around`) and the
`_verify_citations` async generator with a mocked `_llm_complete`. The
revision pass (stage 2) lives in a follow-up PR and gets its own test
file when it lands.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from harbor_clerk.llm.research import (
    _excerpt_around,
    _verify_citations,
)

# ---------------------------------------------------------------------------
# _excerpt_around — substring locator with bounded context windows
# ---------------------------------------------------------------------------


def test_excerpt_around_returns_window_around_needle():
    text = "A" * 1000 + "FOOBAR" + "B" * 1000
    out = _excerpt_around(text, "FOOBAR", context_chars=20)
    assert "FOOBAR" in out
    # Window is 20 chars on each side plus the needle
    assert len(out) == 20 + len("FOOBAR") + 20


def test_excerpt_around_returns_empty_when_needle_missing():
    assert _excerpt_around("nothing here", "absent", context_chars=10) == ""


def test_excerpt_around_returns_empty_on_empty_inputs():
    assert _excerpt_around("", "FOOBAR", context_chars=10) == ""
    assert _excerpt_around("FOOBAR", "", context_chars=10) == ""


def test_excerpt_around_handles_needle_at_start():
    text = "FOOBAR" + "B" * 100
    out = _excerpt_around(text, "FOOBAR", context_chars=30)
    assert out.startswith("FOOBAR")
    assert len(out) == len("FOOBAR") + 30


def test_excerpt_around_handles_needle_at_end():
    text = "A" * 100 + "FOOBAR"
    out = _excerpt_around(text, "FOOBAR", context_chars=30)
    assert out.endswith("FOOBAR")


# ---------------------------------------------------------------------------
# _verify_citations — async generator producing per-citation verdicts
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_client():
    """An httpx.AsyncClient stand-in — verifier doesn't touch it directly,
    only passes it to the mocked _llm_complete."""
    return AsyncMock(spec=httpx.AsyncClient)


async def _collect(agen):
    out = []
    async for v in agen:
        out.append(v)
    return out


async def test_verify_citations_skips_when_report_does_not_mention_doc(fake_client):
    """If the synthesizer didn't reference a cited doc in the final report,
    there's no claim to verify — skip."""
    cites = [{"doc_id": "D1", "doc_title": "MissingDoc", "page": "1"}]
    notes = "[MissingDoc, page 1] some evidence"
    report = "report text that never mentions the doc title"

    out = await _collect(
        _verify_citations(
            client=fake_client,
            llm_url="http://x",
            report_content=report,
            research_citations=cites,
            notes=notes,
        )
    )
    assert len(out) == 1
    assert out[0]["verdict"] == "skipped"
    assert "not referenced" in out[0]["reason"]


async def test_verify_citations_skips_when_notes_missing_doc(fake_client):
    """If notes don't have the doc's text, can't verify — skip without LLM."""
    cites = [{"doc_id": "D1", "doc_title": "MissingDoc", "page": "1"}]
    notes = "notes that don't mention this doc title at all"
    report = "the report cites MissingDoc heavily but notes are empty for it"

    out = await _collect(
        _verify_citations(
            client=fake_client,
            llm_url="http://x",
            report_content=report,
            research_citations=cites,
            notes=notes,
        )
    )
    assert len(out) == 1
    assert out[0]["verdict"] == "skipped"
    assert "not located" in out[0]["reason"]


async def test_verify_citations_emits_supported_verdict(fake_client):
    """LLM returns supported — verdict propagates."""
    cites = [{"doc_id": "D1", "doc_title": "ContractA", "page": "3"}]
    notes = "[ContractA, page 3] The agreement was signed on 2024-01-15."
    report = "ContractA was signed in early 2024 [ContractA, p3]."

    with patch(
        "harbor_clerk.llm.research._llm_complete",
        new=AsyncMock(return_value=json.dumps({"verdict": "supported", "reason": "Date matches."})),
    ):
        out = await _collect(
            _verify_citations(
                client=fake_client,
                llm_url="http://x",
                report_content=report,
                research_citations=cites,
                notes=notes,
            )
        )

    assert len(out) == 1
    assert out[0]["verdict"] == "supported"
    assert out[0]["reason"] == "Date matches."
    assert out[0]["doc_id"] == "D1"
    assert out[0]["index"] == 0
    assert out[0]["total"] == 1


async def test_verify_citations_fails_open_on_unparseable_llm_output(fake_client):
    """A brittle LLM that returns prose instead of JSON shouldn't drop a real
    report. Stage 1 fails open with verdict='supported' and a logged reason."""
    cites = [{"doc_id": "D1", "doc_title": "DocX", "page": "1"}]
    notes = "[DocX, page 1] some evidence"
    report = "The report says DocX is relevant."

    with patch(
        "harbor_clerk.llm.research._llm_complete",
        new=AsyncMock(return_value="not json at all, the model rambled"),
    ):
        out = await _collect(
            _verify_citations(
                client=fake_client,
                llm_url="http://x",
                report_content=report,
                research_citations=cites,
                notes=notes,
            )
        )

    assert len(out) == 1
    assert out[0]["verdict"] == "supported"  # fail-open
    assert "Verifier call failed" in out[0]["reason"]


async def test_verify_citations_rejects_invalid_verdict_string(fake_client):
    """Model returns a JSON object with a verdict outside the allowed
    vocabulary — fall back to 'supported' (fail-open) rather than
    propagating garbage."""
    cites = [{"doc_id": "D1", "doc_title": "DocX", "page": "1"}]
    notes = "[DocX, page 1] some evidence"
    report = "The report says DocX is relevant."

    with patch(
        "harbor_clerk.llm.research._llm_complete",
        new=AsyncMock(return_value=json.dumps({"verdict": "maybe", "reason": "unsure"})),
    ):
        out = await _collect(
            _verify_citations(
                client=fake_client,
                llm_url="http://x",
                report_content=report,
                research_citations=cites,
                notes=notes,
            )
        )

    assert out[0]["verdict"] == "supported"  # fell back from 'maybe'


async def test_verify_citations_handles_multiple_citations_in_order(fake_client):
    """Each citation gets its own LLM call; output preserves order with
    index/total reflecting position."""
    cites = [
        {"doc_id": "D1", "doc_title": "AlphaDoc", "page": "1"},
        {"doc_id": "D2", "doc_title": "BetaDoc", "page": "2"},
        {"doc_id": "D3", "doc_title": "GammaDoc", "page": "3"},
    ]
    notes = "[AlphaDoc, p1] a [BetaDoc, p2] b [GammaDoc, p3] c"
    report = "AlphaDoc says X. BetaDoc says Y. GammaDoc says Z."

    with patch(
        "harbor_clerk.llm.research._llm_complete",
        new=AsyncMock(return_value=json.dumps({"verdict": "supported", "reason": "ok"})),
    ):
        out = await _collect(
            _verify_citations(
                client=fake_client,
                llm_url="http://x",
                report_content=report,
                research_citations=cites,
                notes=notes,
            )
        )

    assert [v["doc_id"] for v in out] == ["D1", "D2", "D3"]
    assert [v["index"] for v in out] == [0, 1, 2]
    assert all(v["total"] == 3 for v in out)


async def test_verify_citations_propagates_partial_verdict(fake_client):
    """Verdicts other than supported/unsupported (i.e. 'partial') must
    propagate verbatim — they're the lever the revision pass acts on."""
    cites = [{"doc_id": "D1", "doc_title": "DocX", "page": "1"}]
    notes = "[DocX, page 1] approximate evidence"
    report = "DocX says X happened on Tuesday."

    with patch(
        "harbor_clerk.llm.research._llm_complete",
        new=AsyncMock(return_value=json.dumps({"verdict": "partial", "reason": "no date in source"})),
    ):
        out = await _collect(
            _verify_citations(
                client=fake_client,
                llm_url="http://x",
                report_content=report,
                research_citations=cites,
                notes=notes,
            )
        )

    assert out[0]["verdict"] == "partial"
    assert out[0]["reason"] == "no date in source"


async def test_verify_citations_truncates_long_reason(fake_client):
    """A verbose LLM that puts an essay in 'reason' should be capped so
    the SSE payload stays bounded."""
    cites = [{"doc_id": "D1", "doc_title": "DocX", "page": "1"}]
    notes = "[DocX, page 1] some evidence"
    report = "DocX is relevant"
    long_reason = "x" * 5000

    with patch(
        "harbor_clerk.llm.research._llm_complete",
        new=AsyncMock(return_value=json.dumps({"verdict": "supported", "reason": long_reason})),
    ):
        out = await _collect(
            _verify_citations(
                client=fake_client,
                llm_url="http://x",
                report_content=report,
                research_citations=cites,
                notes=notes,
            )
        )

    assert len(out[0]["reason"]) <= 300


# ---------------------------------------------------------------------------
# Stage-2 revision pass — _needs_revision + _revise_with_feedback
# ---------------------------------------------------------------------------


def test_needs_revision_false_on_empty():
    from harbor_clerk.llm.research import _needs_revision

    assert _needs_revision([]) is False


def test_needs_revision_false_on_all_supported():
    from harbor_clerk.llm.research import _needs_revision

    verdicts = [
        {"verdict": "supported", "reason": "ok"},
        {"verdict": "supported", "reason": "ok"},
    ]
    assert _needs_revision(verdicts) is False


def test_needs_revision_true_on_any_partial():
    from harbor_clerk.llm.research import _needs_revision

    verdicts = [
        {"verdict": "supported", "reason": "ok"},
        {"verdict": "partial", "reason": "missing date"},
    ]
    assert _needs_revision(verdicts) is True


def test_needs_revision_true_on_any_unsupported():
    from harbor_clerk.llm.research import _needs_revision

    verdicts = [{"verdict": "unsupported", "reason": "fabricated"}]
    assert _needs_revision(verdicts) is True


def test_needs_revision_ignores_skipped():
    """`skipped` means the verifier couldn't judge; treat as no problem
    to fix (the original draft stays)."""
    from harbor_clerk.llm.research import _needs_revision

    verdicts = [
        {"verdict": "supported", "reason": "ok"},
        {"verdict": "skipped", "reason": "source not located"},
    ]
    assert _needs_revision(verdicts) is False


async def _collect_revision(verdicts, draft="DRAFT", notes="NOTES", question="q?", llm_tokens=None):
    """Test helper: call _revise_with_feedback with a mocked token stream
    and collect the yielded tokens into a string."""
    from harbor_clerk.llm.research import _revise_with_feedback

    if llm_tokens is None:
        llm_tokens = ["REVISED ", "REPORT"]

    async def fake_stream(client, url, messages, **kwargs):
        for t in llm_tokens:
            yield t

    captured: dict = {}

    async def fake_stream_with_capture(client, url, messages, **kwargs):
        captured["messages"] = messages
        async for t in fake_stream(client, url, messages, **kwargs):
            yield t

    out = []
    with patch("harbor_clerk.llm.research._stream_llm_tokens", new=fake_stream_with_capture):
        async for token in _revise_with_feedback(
            client=AsyncMock(spec=httpx.AsyncClient),
            llm_url="http://x",
            user_question=question,
            draft_report=draft,
            notes=notes,
            verdicts=verdicts,
        ):
            out.append(token)
    return "".join(out), captured.get("messages")


async def test_revise_with_feedback_filters_to_partial_and_unsupported_only():
    """The revision prompt should only include actionable feedback —
    supported and skipped verdicts are dropped before the prompt is built."""
    verdicts = [
        {"doc_title": "DocA", "page": "1", "verdict": "supported", "reason": "ok"},
        {"doc_title": "DocB", "page": "2", "verdict": "partial", "reason": "missing $X figure"},
        {"doc_title": "DocC", "page": "3", "verdict": "skipped", "reason": "not located"},
        {"doc_title": "DocD", "page": "4", "verdict": "unsupported", "reason": "fabricated"},
    ]
    text, messages = await _collect_revision(verdicts)
    assert text == "REVISED REPORT"
    user_content = messages[1]["content"]
    assert "DocB" in user_content
    assert "DocD" in user_content
    # Supported and skipped verdicts must not appear in the feedback list
    assert "DocA" not in user_content
    assert "DocC" not in user_content


async def test_revise_with_feedback_yields_tokens_in_order():
    """The streamed tokens come out in the exact order the LLM emits them
    — concatenation reconstructs the full revised report."""
    verdicts = [{"doc_title": "DocB", "page": "2", "verdict": "partial", "reason": "x"}]
    tokens = ["# ", "Revised ", "report\n\n", "Body."]
    text, _ = await _collect_revision(verdicts, llm_tokens=tokens)
    assert text == "# Revised report\n\nBody."


async def test_revise_with_feedback_includes_original_question_and_draft():
    """The revision prompt must carry both the original question and the
    full previous draft so the model can produce a coherent rewrite, not a
    standalone new report."""
    verdicts = [{"doc_title": "DocB", "page": "2", "verdict": "partial", "reason": "x"}]
    _, messages = await _collect_revision(
        verdicts,
        question="What were the Q3 board decisions?",
        draft="DRAFT REPORT BODY",
        notes="NOTES BODY",
    )
    user_content = messages[1]["content"]
    assert "What were the Q3 board decisions?" in user_content
    assert "DRAFT REPORT BODY" in user_content
    assert "NOTES BODY" in user_content


# ---------------------------------------------------------------------------
# Settings — revision pass is gated separately from verifier emission.
# Regression coverage for the 2026-06-01 A/B finding that the revision
# pass is net-negative on quality.
# ---------------------------------------------------------------------------


def test_research_verifier_settings_default_off():
    """Both stage-1 and stage-2 flags default False so a fresh install gets
    the unverified research path without surprise extra LLM calls."""
    from harbor_clerk.config import Settings

    s = Settings()
    assert s.research_verifier_enabled is False
    assert s.research_verifier_revision_enabled is False


def test_research_verifier_revision_setting_is_separately_gated():
    """The revision pass must be opt-in via its own flag, NOT bundled into
    `research_verifier_enabled`. The A/B (2026-06-01) showed revision is
    net-negative on overall groundedness; users who want verifier-only
    transparency should not also get the revision degradation."""
    from harbor_clerk.config import Settings

    # Verifier on, revision off — the recommended config for shipping
    s = Settings(research_verifier_enabled=True, research_verifier_revision_enabled=False)
    assert s.research_verifier_enabled is True
    assert s.research_verifier_revision_enabled is False


def test_refresh_llm_settings_reads_research_verifier_revision_enabled(tmp_path, monkeypatch):
    """Like the stage-1 setting, the revision flag must be runtime-mutable
    via config.json so users can toggle it without a process restart."""
    import json

    from harbor_clerk.config import get_settings, refresh_llm_settings

    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"research_verifier_revision_enabled": True}))

    settings = get_settings()
    monkeypatch.setattr(settings, "native_config_file", str(cfg))
    settings.research_verifier_revision_enabled = False  # baseline before refresh

    refresh_llm_settings()
    assert settings.research_verifier_revision_enabled is True

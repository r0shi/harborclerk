"""Tests for adaptive document summarization."""

from unittest.mock import MagicMock, patch

import httpx
import pytest

from harbor_clerk.llm.summarize import (
    AppleIntelligenceUnavailableError,
    _compute_max_input_chars,
    _extractive_fallback,
    _group_chunks_for_mapreduce,
    _sample_chunks,
    _select_tier,
    _Tier,
    _truncate_at_sentence,
    generate_summary,
)

# --- Helper tests ---


class TestComputeMaxInputChars:
    def test_small_context(self):
        # 8192 * 0.75 * 3.5 = 21504
        assert _compute_max_input_chars(8192) == 21504

    def test_large_context_capped(self):
        # 128000 * 0.75 * 3.5 = 336000, capped at 80000
        assert _compute_max_input_chars(128_000) == 80_000

    def test_default_context(self):
        # None → 32768 * 0.75 * 3.5 = 86016, capped at 80000
        assert _compute_max_input_chars(None) == 80_000

    def test_32k_context(self):
        # 32768 * 0.75 * 3.5 = 86016, capped at 80000
        assert _compute_max_input_chars(32_768) == 80_000

    def test_16k_context(self):
        # 16384 * 0.75 * 3.5 = 43008
        assert _compute_max_input_chars(16_384) == 43_008


class TestSelectTier:
    def test_short(self):
        assert _select_tier(1) == _Tier.SHORT
        assert _select_tier(19) == _Tier.SHORT

    def test_medium(self):
        assert _select_tier(20) == _Tier.MEDIUM
        assert _select_tier(99) == _Tier.MEDIUM

    def test_long(self):
        assert _select_tier(100) == _Tier.LONG
        assert _select_tier(500) == _Tier.LONG


class TestSampleChunks:
    def test_empty(self):
        assert _sample_chunks([], 5000) == ""

    def test_few_chunks(self):
        chunks = ["a", "b", "c"]
        result = _sample_chunks(chunks, 5000)
        assert result == "a\n\nb\n\nc"

    def test_many_chunks_includes_head_and_tail(self):
        chunks = [f"chunk_{i}" for i in range(50)]
        result = _sample_chunks(chunks, 100_000)
        # First 3 and last 2 must be present
        assert "chunk_0" in result
        assert "chunk_1" in result
        assert "chunk_2" in result
        assert "chunk_48" in result
        assert "chunk_49" in result

    def test_respects_char_budget(self):
        chunks = ["x" * 100 for _ in range(50)]
        result = _sample_chunks(chunks, 500)
        assert len(result) <= 500

    def test_includes_middle_samples(self):
        chunks = [f"chunk_{i:03d}" for i in range(50)]
        result = _sample_chunks(chunks, 100_000)
        # Should have some middle chunks (not just first 3 + last 2)
        parts = result.split("\n\n")
        assert len(parts) > 5


class TestGroupChunksForMapreduce:
    def test_single_group(self):
        chunks = ["hello", "world"]
        groups = _group_chunks_for_mapreduce(chunks, 1000)
        assert len(groups) == 1
        assert groups[0] == "hello\n\nworld"

    def test_multiple_groups(self):
        chunks = ["a" * 100 for _ in range(10)]
        groups = _group_chunks_for_mapreduce(chunks, 250)
        assert len(groups) > 1
        for g in groups:
            assert len(g) <= 250

    def test_large_single_chunk(self):
        chunks = ["x" * 500]
        groups = _group_chunks_for_mapreduce(chunks, 250)
        # Single chunk exceeds limit but still gets its own group
        assert len(groups) == 1

    def test_empty(self):
        assert _group_chunks_for_mapreduce([], 1000) == []


class TestExtractiveFallback:
    def test_finds_substantial_paragraph(self):
        chunks = ["Short.", "A" * 100, "Also short."]
        result = _extractive_fallback(chunks, 500)
        assert result == "A" * 100

    def test_respects_max_chars(self):
        chunks = ["A" * 200]
        result = _extractive_fallback(chunks, 50)
        assert len(result) == 50

    def test_falls_back_to_raw_text(self):
        chunks = ["hi", "yo"]
        result = _extractive_fallback(chunks, 500)
        assert result == "hi\n\nyo"

    def test_uses_first_5_chunks_only(self):
        chunks = [f"chunk_{i}" for i in range(20)]
        result = _extractive_fallback(chunks, 10000)
        assert "chunk_0" in result
        assert "chunk_4" in result
        assert "chunk_5" not in result


class TestCallLlmRetry:
    """Test retry behavior of _call_llm for transient failures."""

    @pytest.fixture(autouse=True)
    def _llm_active(self, monkeypatch):
        """Default to "LLM is active" for the retry-behavior tests below.

        Without this, `_call_llm`'s ConnectError branch would consult
        `_llm_intentionally_deactivated()` (which reads settings.llm_model_id
        from disk), see the test environment's empty default, and fail-fast
        instead of retrying — which is the correct production behavior but
        the wrong test setup for "does the retry loop work on transient
        errors?". The dedicated fail-fast test below explicitly opts in.
        """
        monkeypatch.setattr(
            "harbor_clerk.llm.summarize._llm_intentionally_deactivated",
            lambda: False,
        )

    def test_succeeds_first_attempt(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"choices": [{"message": {"content": "Summary."}}]}

        with patch("harbor_clerk.llm.summarize.httpx.post", return_value=mock_response) as mock_post:
            from harbor_clerk.llm.summarize import _call_llm

            result = _call_llm("system", "user", timeout=10.0, max_attempts=3)
            assert result == "Summary."
            assert mock_post.call_count == 1

    def test_retries_on_timeout(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"choices": [{"message": {"content": "Summary."}}]}

        with (
            patch(
                "harbor_clerk.llm.summarize.httpx.post",
                side_effect=[httpx.TimeoutException("timeout"), mock_response],
            ) as mock_post,
            patch("harbor_clerk.llm.summarize.time.sleep"),
        ):
            from harbor_clerk.llm.summarize import _call_llm

            result = _call_llm("system", "user", timeout=10.0, max_attempts=3)
            assert result == "Summary."
            assert mock_post.call_count == 2

    def test_retries_on_connect_error(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"choices": [{"message": {"content": "Summary."}}]}

        with (
            patch(
                "harbor_clerk.llm.summarize.httpx.post",
                side_effect=[httpx.ConnectError("refused"), mock_response],
            ) as mock_post,
            patch("harbor_clerk.llm.summarize.time.sleep"),
        ):
            from harbor_clerk.llm.summarize import _call_llm

            result = _call_llm("system", "user", timeout=10.0, max_attempts=3)
            assert result == "Summary."
            assert mock_post.call_count == 2

    def test_retries_on_503(self):
        busy_response = MagicMock()
        busy_response.status_code = 503

        ok_response = MagicMock()
        ok_response.status_code = 200
        ok_response.raise_for_status = MagicMock()
        ok_response.json.return_value = {"choices": [{"message": {"content": "Summary."}}]}

        with (
            patch(
                "harbor_clerk.llm.summarize.httpx.post",
                side_effect=[busy_response, ok_response],
            ) as mock_post,
            patch("harbor_clerk.llm.summarize.time.sleep"),
        ):
            from harbor_clerk.llm.summarize import _call_llm

            result = _call_llm("system", "user", timeout=10.0, max_attempts=3)
            assert result == "Summary."
            assert mock_post.call_count == 2

    def test_gives_up_after_max_attempts(self):
        with (
            patch(
                "harbor_clerk.llm.summarize.httpx.post",
                side_effect=httpx.TimeoutException("timeout"),
            ) as mock_post,
            patch("harbor_clerk.llm.summarize.time.sleep"),
        ):
            from harbor_clerk.llm.summarize import _call_llm

            result = _call_llm("system", "user", timeout=10.0, max_attempts=3)
            assert result is None
            assert mock_post.call_count == 3

    def test_no_retry_on_non_retryable_error(self):
        with patch(
            "harbor_clerk.llm.summarize.httpx.post",
            side_effect=ValueError("bad json"),
        ) as mock_post:
            from harbor_clerk.llm.summarize import _call_llm

            result = _call_llm("system", "user", timeout=10.0, max_attempts=3)
            assert result is None
            assert mock_post.call_count == 1


class TestCallLlmDeactivatedFailFast:
    """ConnectError + user-deactivated LLM → return None on the FIRST attempt,
    skipping the ~95 s of jittered retries that would otherwise just fall
    through to the AFM / extractive fallback path anyway.

    The 'loading' state (model_id set, llama-server still warming up) is NOT
    handled — see `_llm_intentionally_deactivated` docstring for why."""

    def test_connect_error_when_deactivated_skips_remaining_retries(self, monkeypatch):
        monkeypatch.setattr(
            "harbor_clerk.llm.summarize._llm_intentionally_deactivated",
            lambda: True,  # user took the LLM down
        )
        sleeps: list[float] = []
        monkeypatch.setattr("harbor_clerk.llm.summarize.time.sleep", sleeps.append)
        with patch(
            "harbor_clerk.llm.summarize.httpx.post",
            side_effect=httpx.ConnectError("refused"),
        ) as mock_post:
            from harbor_clerk.llm.summarize import _call_llm

            result = _call_llm("system", "user", timeout=10.0, max_attempts=3)
        assert result is None
        # Exactly ONE post — no retry sleeps after the deactivation check.
        assert mock_post.call_count == 1
        assert sleeps == []

    def test_connect_error_when_active_still_retries(self, monkeypatch):
        """Sanity check the fixture: when the LLM is active, we get the
        existing retry behavior (not the fail-fast path). Mirrors the
        autouse-fixture'd TestCallLlmRetry.test_retries_on_connect_error
        but inline so this test class is self-contained."""
        monkeypatch.setattr(
            "harbor_clerk.llm.summarize._llm_intentionally_deactivated",
            lambda: False,
        )
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {"choices": [{"message": {"content": "OK"}}]}
        with (
            patch(
                "harbor_clerk.llm.summarize.httpx.post",
                side_effect=[httpx.ConnectError("refused"), mock_response],
            ) as mock_post,
            patch("harbor_clerk.llm.summarize.time.sleep"),
        ):
            from harbor_clerk.llm.summarize import _call_llm

            result = _call_llm("system", "user", timeout=10.0, max_attempts=3)
        assert result == "OK"
        assert mock_post.call_count == 2

    def test_timeout_still_retries_even_when_deactivated(self, monkeypatch):
        """The fail-fast guard only fires on ConnectError, not TimeoutException.
        A timeout against llama-server could mean the model is loading slowly
        (long mmap on big GGUFs); the user may have deactivated AND re-activated
        in the meantime. Conservative: only short-circuit when we're definitely
        not going to hit a working server."""
        monkeypatch.setattr(
            "harbor_clerk.llm.summarize._llm_intentionally_deactivated",
            lambda: True,
        )
        with (
            patch(
                "harbor_clerk.llm.summarize.httpx.post",
                side_effect=httpx.TimeoutException("timeout"),
            ) as mock_post,
            patch("harbor_clerk.llm.summarize.time.sleep"),
        ):
            from harbor_clerk.llm.summarize import _call_llm

            result = _call_llm("system", "user", timeout=10.0, max_attempts=3)
        assert result is None
        assert mock_post.call_count == 3  # full retry budget


# --- generate_summary tests ---


def _mock_settings(llm_model_id="test-model", summary_force_apple_intelligence=False):
    s = MagicMock()
    s.llm_model_id = llm_model_id
    s.summary_max_chars = 500
    s.llama_server_url = "http://localhost:8102"
    s.summary_force_apple_intelligence = summary_force_apple_intelligence
    return s


class TestAppleIntelligenceCircuitBreaker:
    def test_timeout_disables_followup_daemon_calls(self, monkeypatch):
        from harbor_clerk.llm import summarize as sum_mod

        class FakePipe:
            def write(self, _value):
                return None

            def flush(self):
                return None

            def readline(self):
                return '{"result": "too late"}\n'

        class FakeDaemon:
            stdin = FakePipe()
            stdout = FakePipe()

        start_daemon = MagicMock(return_value=FakeDaemon())
        stop_daemon = MagicMock()

        monkeypatch.setattr(sum_mod, "_afm_disabled_until_monotonic", 0.0)
        monkeypatch.setattr(sum_mod, "_afm_disabled_reason", "")
        monkeypatch.setattr(sum_mod, "_AFM_CIRCUIT_BREAKER_SECONDS", 60.0)
        monkeypatch.setattr(sum_mod, "_DAEMON_READ_TIMEOUT_S", 0.01)
        monkeypatch.setattr(sum_mod, "_get_or_start_daemon", start_daemon)
        monkeypatch.setattr(sum_mod, "_stop_daemon", stop_daemon)
        monkeypatch.setattr(sum_mod.select, "select", lambda *_args: ([], [], []))

        assert sum_mod._apple_intelligence_call("hello", mode="summary") is None
        assert start_daemon.call_count == 1
        assert stop_daemon.call_count == 1
        assert sum_mod._apple_intelligence_disabled_reason() == "daemon timed out"

        assert sum_mod._apple_intelligence_call("hello again", mode="doc_type") is None
        assert start_daemon.call_count == 1

    def test_disabled_apple_intelligence_falls_back_to_mime_type(self, monkeypatch):
        from harbor_clerk.llm import summarize as sum_mod

        monkeypatch.setattr(sum_mod, "_afm_disabled_until_monotonic", sum_mod.time.monotonic() + 60.0)
        monkeypatch.setattr(sum_mod, "_afm_disabled_reason", "daemon timed out")
        monkeypatch.setattr(sum_mod, "_get_or_start_daemon", MagicMock(side_effect=AssertionError("daemon called")))

        with (
            patch("harbor_clerk.llm.summarize.get_settings", return_value=_mock_settings(llm_model_id="")),
            patch("harbor_clerk.llm.summarize._call_llm", return_value=None),
        ):
            assert sum_mod.classify_doc_type(["Some text content"], mime_type="application/pdf") == "PDF Document"


class TestGenerateSummary:
    def test_empty_chunks(self):
        with patch("harbor_clerk.llm.summarize.get_settings", return_value=_mock_settings()):
            summary, model = generate_summary([])
            assert summary == ""
            assert model == ""

    def test_whitespace_only_chunks(self):
        with patch("harbor_clerk.llm.summarize.get_settings", return_value=_mock_settings()):
            summary, model = generate_summary(["  ", "\n", "\t"])
            assert summary == ""
            assert model == ""

    def test_no_model_extractive_fallback(self):
        with patch("harbor_clerk.llm.summarize.get_settings", return_value=_mock_settings(llm_model_id="")):
            chunks = ["A" * 100]
            summary, model = generate_summary(chunks)
            assert model == "extractive"
            assert len(summary) > 0

    def test_short_tier_single_call(self):
        with (
            patch("harbor_clerk.llm.summarize.get_settings", return_value=_mock_settings()),
            patch("harbor_clerk.llm.summarize.get_model", return_value=MagicMock(context_window=32768)),
            patch("harbor_clerk.llm.summarize._call_llm", return_value="A great summary.") as mock_call,
        ):
            chunks = [f"chunk_{i}" for i in range(5)]
            summary, model = generate_summary(chunks)
            assert summary == "A great summary."
            assert model == "test-model"
            assert mock_call.call_count == 1

    def test_medium_tier_single_call(self):
        with (
            patch("harbor_clerk.llm.summarize.get_settings", return_value=_mock_settings()),
            patch("harbor_clerk.llm.summarize.get_model", return_value=MagicMock(context_window=32768)),
            patch("harbor_clerk.llm.summarize._call_llm", return_value="Medium summary.") as mock_call,
        ):
            chunks = [f"chunk_{i}" for i in range(50)]
            summary, model = generate_summary(chunks)
            assert summary == "Medium summary."
            assert model == "test-model"
            assert mock_call.call_count == 1

    def test_long_tier_map_reduce(self):
        with (
            patch("harbor_clerk.llm.summarize.get_settings", return_value=_mock_settings()),
            patch("harbor_clerk.llm.summarize.get_model", return_value=MagicMock(context_window=32768)),
            patch(
                "harbor_clerk.llm.summarize._call_llm",
                side_effect=lambda prompt, text, **kw: (
                    "Final summary." if "summaries of different" in prompt.lower() else "Section summary."
                ),
            ) as mock_call,
        ):
            # Each chunk ~1000 chars × 150 = ~150K, forcing multiple groups at 80K limit
            chunks = [f"chunk_{i} " * 100 for i in range(150)]
            summary, model = generate_summary(chunks)
            assert summary == "Final summary."
            assert model == "test-model"
            # Map calls + 1 reduce call
            assert mock_call.call_count > 2

    def test_llm_failure_falls_back_to_extractive(self):
        with (
            patch("harbor_clerk.llm.summarize.get_settings", return_value=_mock_settings()),
            patch("harbor_clerk.llm.summarize.get_model", return_value=MagicMock(context_window=32768)),
            patch("harbor_clerk.llm.summarize._call_llm", return_value=None),
        ):
            chunks = ["A" * 100]
            summary, model = generate_summary(chunks)
            assert model == "extractive"
            assert len(summary) > 0

    def test_respects_max_chars(self):
        with (
            patch("harbor_clerk.llm.summarize.get_settings", return_value=_mock_settings()),
            patch("harbor_clerk.llm.summarize.get_model", return_value=MagicMock(context_window=32768)),
            patch("harbor_clerk.llm.summarize._call_llm", return_value="X" * 1000),
        ):
            chunks = ["Some text"]
            summary, model = generate_summary(chunks, max_chars=100)
            assert len(summary) <= 100

    def test_zero_width_space_summary_falls_back(self):
        """Regression: a wedged LLM emitting only a zero-width space (or other
        invisible Unicode) must NOT persist as a "blank summary attributed to
        the current model" — should fall through to extractive instead."""
        with (
            patch("harbor_clerk.llm.summarize.get_settings", return_value=_mock_settings()),
            patch("harbor_clerk.llm.summarize.get_model", return_value=MagicMock(context_window=32768)),
            # Simulate LLM returning a single zero-width space as the summary value.
            patch("harbor_clerk.llm.summarize._call_llm", return_value="​"),
        ):
            chunks = ["A" * 100]
            summary, model = generate_summary(chunks)
            # Must NOT attribute to the model — should fall back.
            assert model == "extractive"
            assert summary  # extractive always returns content for non-empty chunks

    def test_whitespace_only_summary_falls_back(self):
        """Regression: a model returning whitespace-only summary must fall back."""
        with (
            patch("harbor_clerk.llm.summarize.get_settings", return_value=_mock_settings()),
            patch("harbor_clerk.llm.summarize.get_model", return_value=MagicMock(context_window=32768)),
            patch("harbor_clerk.llm.summarize._call_llm", return_value="   \n\t"),
        ):
            chunks = ["A" * 100]
            summary, model = generate_summary(chunks)
            assert model == "extractive"
            assert summary

    def test_force_afm_skips_llm_and_uses_apple_intelligence(self):
        """When summary_force_apple_intelligence is set, the LLM is never called
        and the result is attributed to apple-intelligence."""
        mock_call = MagicMock()
        with (
            patch(
                "harbor_clerk.llm.summarize.get_settings",
                return_value=_mock_settings(summary_force_apple_intelligence=True),
            ),
            patch("harbor_clerk.llm.summarize._call_llm", mock_call),
            patch(
                "harbor_clerk.llm.summarize._apple_intelligence_summary",
                return_value="AFM-generated summary.",
            ),
        ):
            summary, model = generate_summary(["A" * 100])
            assert summary == "AFM-generated summary."
            assert model == "apple-intelligence"
            mock_call.assert_not_called()

    def test_force_afm_errors_when_afm_unavailable(self):
        """If AFM is unavailable while forced, fail closed instead of writing extractive summaries."""
        mock_call = MagicMock()
        with (
            patch(
                "harbor_clerk.llm.summarize.get_settings",
                return_value=_mock_settings(summary_force_apple_intelligence=True),
            ),
            patch("harbor_clerk.llm.summarize._call_llm", mock_call),
            patch("harbor_clerk.llm.summarize._apple_intelligence_summary", return_value=None),
            patch("harbor_clerk.llm.summarize._apple_intelligence_disabled_reason", return_value="daemon timed out"),
        ):
            chunks = ["A" * 100]
            with pytest.raises(AppleIntelligenceUnavailableError, match="daemon timed out"):
                generate_summary(chunks)
            mock_call.assert_not_called()


def test_summarize_long_calls_progress_callback_per_map_step(monkeypatch):
    """Map-reduce summarize must report progress between sub-calls so the
    queue tray's progress bar can fill in chunks."""
    from harbor_clerk.llm import summarize as sum_mod

    # Stub _call_llm so each map call returns a fixed string and the
    # reduce returns the joined result. We don't actually hit a model.
    def fake_call_llm(*args, **kwargs):
        return "section-summary"

    monkeypatch.setattr(sum_mod, "_call_llm", fake_call_llm)

    # Force long-tier path: > _LONG_THRESHOLD chunks worth of input.
    chunks = ["x" * 8000 for _ in range(150)]

    progress_calls: list[tuple[int, int]] = []

    def record(current: int, total: int) -> None:
        progress_calls.append((current, total))

    sum_mod._summarize_long(chunks, max_input_chars=80_000, progress_callback=record)

    # Expect at least one progress update per map call + one before reduce.
    # Specifically: total should be the same on every call (number of
    # groups + 1 for reduce); current should monotonically increase.
    assert len(progress_calls) >= 2
    totals = {t for _, t in progress_calls}
    assert len(totals) == 1, f"total varied: {totals}"
    currents = [c for c, _ in progress_calls]
    assert currents == sorted(currents), "current must be monotonic"
    assert currents[-1] == currents[0] + len(progress_calls) - 1
    # total must equal number of map groups + 1 (the reduce slot). If
    # _summarize_long ever stopped firing the reduce callback or
    # miscounted total_chunks, this catches it.
    n_total = totals.pop()
    assert n_total == len(progress_calls), f"expected total={len(progress_calls)} (groups+reduce), got {n_total}"
    assert currents[-1] == n_total - 1, "reduce step must fire as the final progress event"


class TestTruncateAtSentence:
    def test_short_text_unchanged(self):
        assert _truncate_at_sentence("Hello world.", 100) == "Hello world."

    def test_truncates_at_sentence_boundary(self):
        text = "First sentence. Second sentence. Third sentence."
        result = _truncate_at_sentence(text, 35)
        assert result == "First sentence. Second sentence."

    def test_truncates_at_period_with_space(self):
        text = "Version 3.5 is here. It has new features."
        result = _truncate_at_sentence(text, 25)
        assert result == "Version 3.5 is here."

    def test_falls_back_to_word_boundary(self):
        text = "no punctuation at all in this text"
        result = _truncate_at_sentence(text, 20)
        assert result.endswith("\u2026")
        assert " " not in result[-2:]  # didn't cut mid-word

    def test_handles_exclamation_and_question(self):
        text = "What is this? It is great! Done."
        assert _truncate_at_sentence(text, 16) == "What is this?"
        assert _truncate_at_sentence(text, 28) == "What is this? It is great!"

    def test_exact_length_no_truncation(self):
        text = "Exact."
        assert _truncate_at_sentence(text, 6) == "Exact."


class TestClassifyDocType:
    def test_mime_fallback_pdf(self):
        from harbor_clerk.llm.summarize import _mime_to_doc_type

        assert _mime_to_doc_type("application/pdf") == "PDF Document"

    def test_mime_fallback_text(self):
        from harbor_clerk.llm.summarize import _mime_to_doc_type

        assert _mime_to_doc_type("text/plain") == "Text File"

    def test_mime_fallback_csv(self):
        from harbor_clerk.llm.summarize import _mime_to_doc_type

        assert _mime_to_doc_type("text/csv") == "Spreadsheet"

    def test_mime_fallback_image(self):
        from harbor_clerk.llm.summarize import _mime_to_doc_type

        assert _mime_to_doc_type("image/jpeg") == "Image"

    def test_mime_fallback_unknown_image(self):
        from harbor_clerk.llm.summarize import _mime_to_doc_type

        assert _mime_to_doc_type("image/webp") == "Image"

    def test_mime_fallback_word(self):
        from harbor_clerk.llm.summarize import _mime_to_doc_type

        assert "Word" in _mime_to_doc_type("application/vnd.openxmlformats-officedocument.wordprocessingml.document")

    def test_mime_fallback_email(self):
        from harbor_clerk.llm.summarize import _mime_to_doc_type

        assert _mime_to_doc_type("message/rfc822") == "Email"

    def test_mime_fallback_unknown(self):
        from harbor_clerk.llm.summarize import _mime_to_doc_type

        assert _mime_to_doc_type("application/x-unknown-thing") == "Document"

    def test_classify_falls_back_without_llm(self):
        """When no LLM model is configured, classify_doc_type uses MIME fallback."""
        from harbor_clerk.llm.summarize import classify_doc_type

        with patch("harbor_clerk.llm.summarize.get_settings") as mock_settings:
            mock_settings.return_value.llm_model_id = ""
            mock_settings.return_value.summary_force_apple_intelligence = False
            result = classify_doc_type(["Some text content"], mime_type="application/pdf")
            assert result == "PDF Document"

    def test_classify_force_apple_skips_local_llm_when_apple_unavailable(self):
        """The forced-Apple summary mode must not pay local LLM doc_type latency."""
        from harbor_clerk.llm.summarize import classify_doc_type

        mock_call = MagicMock()
        with (
            patch(
                "harbor_clerk.llm.summarize.get_settings",
                return_value=_mock_settings(summary_force_apple_intelligence=True),
            ),
            patch("harbor_clerk.llm.summarize._apple_intelligence_doc_type", return_value=None),
            patch("harbor_clerk.llm.summarize._call_llm", mock_call),
        ):
            result = classify_doc_type(["Some text content"], mime_type="application/pdf")
            assert result == "PDF Document"
            mock_call.assert_not_called()

    def test_classify_force_apple_uses_apple_doc_type(self):
        from harbor_clerk.llm.summarize import classify_doc_type

        mock_call = MagicMock()
        with (
            patch(
                "harbor_clerk.llm.summarize.get_settings",
                return_value=_mock_settings(summary_force_apple_intelligence=True),
            ),
            patch("harbor_clerk.llm.summarize._apple_intelligence_doc_type", return_value="Meeting Notes"),
            patch("harbor_clerk.llm.summarize._call_llm", mock_call),
        ):
            result = classify_doc_type(["Some text content"], mime_type="application/pdf")
            assert result == "Meeting Notes"
            mock_call.assert_not_called()


class TestUserLLMActiveQueriesChatMessage:
    """Regression: PR #327's _user_llm_active() used ChatMessage.id, but the
    model defines message_id (not id). Every summarize call crashed
    immediately with AttributeError: type object 'ChatMessage' has no
    attribute 'id', failing the yield-to-interactive guard before the
    stage did any work. Visible in production as 100% summarize failure
    on a fresh corpus ingest.
    """

    @pytest.mark.asyncio
    async def test_no_chat_messages_returns_false_without_attribute_error(self, db_session):
        """The query must execute against the real ChatMessage model without
        crashing. Empty table → False (no recent user activity).
        The db_session fixture exists purely to ensure Alembic has run and
        the chat_messages table is present before _user_llm_active opens
        its own sync session."""
        from harbor_clerk.worker.stages.summarize import _user_llm_active

        assert _user_llm_active() is False

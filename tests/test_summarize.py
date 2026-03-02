"""Tests for adaptive document summarization."""

from unittest.mock import MagicMock, patch

from harbor_clerk.llm.summarize import (
    _compute_max_input_chars,
    _extractive_fallback,
    _group_chunks_for_mapreduce,
    _sample_chunks,
    _select_tier,
    _Tier,
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


# --- generate_summary tests ---


def _mock_settings(llm_model_id="test-model"):
    s = MagicMock()
    s.llm_model_id = llm_model_id
    s.summary_max_chars = 500
    s.llama_server_url = "http://localhost:8102"
    return s


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

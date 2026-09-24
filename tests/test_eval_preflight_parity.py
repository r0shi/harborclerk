"""The eval harness is its own project and does not import the app, so its machine preflight carries a copy
of the registry's fit arithmetic (Swift carries another). This holds the copy to the original."""

import pytest

from harbor_clerk.llm.models import (
    HOST_HEADROOM_BYTES,
    MODELS,
    RUNTIME_OVERHEAD_BYTES,
    ModelInfo,
    fixed_bytes,
    max_context,
)
from scripts.test_corpora import preflight

GIB = 1024**3


def test_the_harness_reads_every_model_and_its_memory_figures_from_the_registry():
    read = preflight.registry()
    assert list(read) == list(MODELS), "every model, in the registry's order"
    for model_id, m in MODELS.items():
        assert read[model_id] == {
            "size_bytes": m.size_bytes,
            "kv_bytes_per_token": m.kv_bytes_per_token,
            "kv_fixed_bytes": fixed_bytes(m),  # the checkpoints the launcher keeps are part of it
        }
    assert preflight._constant("RUNTIME_OVERHEAD_BYTES") == RUNTIME_OVERHEAD_BYTES
    assert preflight._constant("HOST_HEADROOM_BYTES") == HOST_HEADROOM_BYTES


@pytest.mark.parametrize("ram_gib", [8, 12, 16, 18, 24, 32, 36, 48, 64, 128])
def test_the_preflight_says_a_model_fits_exactly_when_the_app_would_load_it(ram_gib):
    read = preflight.registry()
    for model_id, m in MODELS.items():
        ours = preflight.fits(
            read[model_id], ram_gib * GIB, overhead=RUNTIME_OVERHEAD_BYTES, headroom=HOST_HEADROOM_BYTES
        )
        assert ours == (max_context(m, ram_gib * GIB) > 0), f"{model_id} at {ram_gib} GiB"


def test_the_two_agree_at_the_edge_where_a_few_thousand_tokens_is_not_worth_running():
    """No curated model at a real memory size lands between 1 and 4095 tokens of room, so the models above
    cannot see the 4096-token floor or the round-down to 1024. A made-up model, walked across the edge in
    10 MB steps, can."""
    figures = {"size_bytes": 5_000_000_000, "kv_bytes_per_token": 147_456, "kv_fixed_bytes": 250_000_000}
    model = ModelInfo(
        id="edge",
        name="Edge",
        huggingface_repo="r",
        filename="f.gguf",
        size_bytes=figures["size_bytes"],
        context_window=32768,
        supports_tools=True,
        kv_bytes_per_token=figures["kv_bytes_per_token"],
        kv_fixed_bytes=figures["kv_fixed_bytes"],
    )
    verdicts = set()
    # 5 GB of weights, 1 GB of overhead and the machine's 8 GB: the edge is a little past 14 GB.
    for ram in range(14_000_000_000, 15_200_000_000, 10_000_000):
        ours = preflight.fits(figures, ram, overhead=RUNTIME_OVERHEAD_BYTES, headroom=HOST_HEADROOM_BYTES)
        assert ours == (max_context(model, ram) > 0), f"{ram} bytes"
        verdicts.add(ours)
    assert verdicts == {True, False}, "the walk crosses the edge"


def test_the_harness_counts_the_checkpoints_of_every_slot(tmp_path, monkeypatch):
    """Every curated model runs one slot, so the real registry cannot show the harness's copy ignoring the
    slot count (the same blind spot the Swift mirror had). A made-up registry with a two-slot model can."""
    source = tmp_path / "src/harbor_clerk/llm"
    source.mkdir(parents=True)
    (source / "models.py").write_text(
        "LLAMA_CTX_CHECKPOINTS = 3\n"
        'M = [ModelInfo(id="two", size_bytes=5, kv_bytes_per_token=1, kv_fixed_bytes=10, checkpoint_bytes=7, '
        "parallel_slots=2)]\n"
    )
    monkeypatch.setattr(preflight, "REPO", tmp_path)
    two = ModelInfo(
        id="two",
        name="Two",
        huggingface_repo="r",
        filename="f.gguf",
        size_bytes=5,
        context_window=4096,
        supports_tools=True,
        kv_bytes_per_token=1,
        kv_fixed_bytes=10,
        checkpoint_bytes=7,
        parallel_slots=2,
    )
    assert preflight.registry()["two"]["kv_fixed_bytes"] == fixed_bytes(two) == 10 + 2 * 3 * 7

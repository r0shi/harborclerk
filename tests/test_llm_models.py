import struct
from pathlib import Path

import pytest

from harbor_clerk.llm.models import (
    HOST_HEADROOM_BYTES,
    MODELS,
    RUNTIME_OVERHEAD_BYTES,
    ModelInfo,
    YarnConfig,
    kv_bytes,
    max_context,
    memory_bytes,
    min_ram_gb,
    ram_required_bytes,
    system_ram_bytes,
)


def test_modelinfo_has_find_all_default_max_results():
    """New optional field; defaults to None on all curated models."""
    info = ModelInfo(
        id="dummy",
        name="Dummy",
        huggingface_repo="repo",
        filename="dummy.gguf",
        size_bytes=1,
        context_window=4096,
        supports_tools=True,
    )
    assert info.find_all_default_max_results is None


def test_all_curated_models_default_find_all_max_to_none():
    for m in MODELS.values():
        assert m.find_all_default_max_results is None, m.id


def test_modelinfo_parallel_slots_defaults_to_one():
    """Heavy-tier-safe default — no model gets implicit extra KV cache."""
    info = ModelInfo(
        id="dummy",
        name="Dummy",
        huggingface_repo="repo",
        filename="dummy.gguf",
        size_bytes=1,
        context_window=4096,
        supports_tools=True,
    )
    assert info.parallel_slots == 1


def test_curated_models_parallel_slots_tiered_by_size():
    """Per-model `-np` values follow the size-based tier table:

    - Small (≤4 GB GGUF): 4 slots
    - Mid (5-12 GB, ≤32K context): 2 slots
    - Heavy (>15 GB OR 128K+ context): 1 slot

    Context window matters as much as parameter count because llama-server
    allocates KV cache for all slots upfront. GPT-OSS 20B has small MoE
    active params but a 128K context window, so 2 slots' KV would risk
    OOM on 18 GB unified memory — it lives in the heavy tier with the
    big dense models.
    """
    expected = {
        # Small — but exception: qwen3-4b is -np 1 (not 4) because the
        # 8K per-slot budget under -np 4 was too tight for the chat tools
        # schema plus an ambiguous search result. See models.py.
        "qwen3-4b": 1,
        # Mid (≤32K native context)
        "qwen3-8b": 2,
        # Heavy
        "gpt-oss-20b": 1,  # 128K context → KV cache too big for 2 slots on 18 GB
        "gemma4-26b-a4b": 1,
        "qwen36-35b-a3b": 1,
    }
    for model_id, slots in expected.items():
        m = MODELS[model_id]
        assert m.parallel_slots == slots, (
            f"{model_id}: expected parallel_slots={slots} (size={m.size_bytes / 1e9:.1f} GB), got {m.parallel_slots}"
        )
    # Belt-and-suspenders: every curated model must have an explicit
    # parallel_slots entry above (no silent default-to-1).
    assert set(expected.keys()) == set(MODELS.keys()), (
        f"parallel_slots tier table out of sync with MODELS registry: "
        f"missing={set(MODELS.keys()) - set(expected.keys())}, "
        f"extra={set(expected.keys()) - set(MODELS.keys())}"
    )


# --- memory budget (#556) ---------------------------------------------------------------------------------------

MODELS_DIR = Path.home() / "Library/Application Support/Harbor Clerk/models"


def _info(**over) -> ModelInfo:
    base = dict(
        id="dummy",
        name="Dummy",
        huggingface_repo="repo",
        filename="dummy.gguf",
        size_bytes=5_000_000_000,
        context_window=32768,
        supports_tools=True,
        kv_bytes_per_token=147_456,
    )
    return ModelInfo(**{**base, **over})


def test_every_curated_model_states_its_kv_cost():
    """Without it the budget is weights alone, which is what let a 19 GB KV cache be launched by hand."""
    for m in MODELS.values():
        assert m.kv_bytes_per_token > 0, m.id


def test_gemma_context_window_is_the_ggufs_not_the_small_models():
    assert MODELS["gemma4-26b-a4b"].context_window == 262144  # #548
    assert MODELS["gpt-oss-20b"].context_window == 128000  # correct as is; #548 says so


def test_memory_arithmetic_is_weights_plus_kv_plus_overhead():
    m = _info()
    assert kv_bytes(m, 32768) == 147_456 * 32768
    assert memory_bytes(m) == 5_000_000_000 + 147_456 * 32768 + RUNTIME_OVERHEAD_BYTES
    assert memory_bytes(m, 131072) - memory_bytes(m, 32768) == 147_456 * (131072 - 32768)
    assert ram_required_bytes(m) == memory_bytes(m) + HOST_HEADROOM_BYTES
    fixed = _info(kv_bytes_per_token=20_480, kv_fixed_bytes=209_715_200)
    assert kv_bytes(fixed, 0) == 209_715_200 and kv_bytes(fixed, 1000) == 209_715_200 + 20_480_000


def test_min_ram_rounds_up_to_a_size_macs_come_with():
    assert min_ram_gb(_info()) == 16  # 5 + 4.8 + 1 + 6 = 16.8e9 bytes = 15.7 GiB: a "16 GB" Mac has 16 GiB
    assert min_ram_gb(_info(size_bytes=2_500_000_000)) == 16
    assert min_ram_gb(_info(size_bytes=6_000_000_000)) == 18, "17.8e9 bytes is 16.6 GiB: past a 16 GB Mac"
    assert min_ram_gb(MODELS["qwen36-35b-a3b"]) == 36, "the 35B-A3B at full context is a 36 GB model, not a 32 GB one"
    assert min_ram_gb(MODELS["gemma4-26b-a4b"]) == 32, "#548 does not move Gemma out of the 32 GB tier"
    assert min_ram_gb(_info(size_bytes=600_000_000_000)) == 570, "past the largest Mac, the exact number of GiB"


def test_max_context_is_what_fits_never_more_than_the_model_offers_and_zero_when_the_weights_alone_do_not():
    m = _info()
    plenty = 64 * 1024**3
    assert max_context(m, plenty) == 32768
    # 5 GB weights + 1 GB overhead + 6 GB headroom = 12 GB; 16 GB leaves 4 GB = 27126 tokens, floored to 1024s.
    assert max_context(m, 16_000_000_000) == 26624
    assert max_context(m, 12_500_000_000) == 0, "3389 tokens is not worth running"
    assert max_context(m, 11_000_000_000) == 0
    assert max_context(_info(kv_bytes_per_token=0), 16_000_000_000) == 32768, (
        "no per-token cost: the model's own window"
    )
    assert max_context(_info(kv_bytes_per_token=0), 11_000_000_000) == 0
    fixed = _info(kv_bytes_per_token=20_480, kv_fixed_bytes=209_715_200, context_window=262144)
    assert max_context(fixed, 16_000_000_000) == (16_000_000_000 - 12_209_715_200) // 20_480 // 1024 * 1024


def test_effective_context_is_the_requested_window_clamped_to_what_fits():
    from harbor_clerk.llm.models import effective_context, requested_context

    m = _info(yarn=YarnConfig(extended_context=131072, rope_scale=4.0, original_context=32768))
    assert requested_context(m, False) == 32768 and requested_context(m, True) == 131072
    assert requested_context(_info(), True) == 32768, "YaRN on, but the model has none"
    plenty = 64 * 1024**3
    assert effective_context(m, True, plenty) == 131072
    assert effective_context(m, True, 16_000_000_000) == 26624, "the YaRN window, clamped to what 16 GB fits"
    assert effective_context(m, False, 16_000_000_000) == 26624
    assert effective_context(m, False, 0) == 32768, "memory unknown: the requested window"
    assert effective_context(m, True, 11_000_000_000) == 131072, (
        "does not fit at all: the launcher refuses; nothing to budget by"
    )
    assert max_context(m, plenty, requested=131072) == 131072 and max_context(m, plenty) == 32768


def test_this_machines_ram_is_readable():
    assert system_ram_bytes() > 4 * 1024**3


def _gguf_header(path: Path, prefix: str) -> dict:
    """The architecture keys of a GGUF header. Only the header is read."""
    with path.open("rb") as f:
        return _read_header(f, prefix)


def _read_header(f, prefix: str) -> dict:
    def u32():
        return struct.unpack("<I", f.read(4))[0]

    def u64():
        return struct.unpack("<Q", f.read(8))[0]

    def string():
        return f.read(u64()).decode("utf-8", "replace")

    sizes = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f", 7: "<B", 10: "<Q", 11: "<q", 12: "<d"}

    def value(t):
        if t == 8:
            return string()
        if t == 9:
            et, n = u32(), u64()
            return [value(et) for _ in range(n)]
        return struct.unpack(sizes[t], f.read(struct.calcsize(sizes[t])))[0]

    assert f.read(4) == b"GGUF"
    u32()
    u64()
    out = {}
    for _ in range(u64()):
        k, t = string(), u32()
        v = value(t)
        if k.startswith(prefix + "."):
            out[k[len(prefix) + 1 :]] = v
    return out


# (model id, architecture prefix, layers whose KV grows with context out of block_count)
GEOMETRY = {
    "qwen3-8b": ("qwen3", "all"),
    "qwen3-4b": ("qwen3", "all"),
    "gemma4-26b-a4b": ("gemma4", "global"),  # sliding_window_pattern: 1 = windowed, 0 = global
    "gpt-oss-20b": ("gpt-oss", "half"),  # llama.cpp alternates window and full layers for this architecture
    "qwen36-35b-a3b": ("qwen35moe", "interval"),  # full_attention_interval
}


@pytest.mark.parametrize("model_id", sorted(GEOMETRY))
def test_kv_cost_matches_the_gguf_header_when_the_file_is_here(model_id: str):
    """The constants were read from these headers; if a file changes under
    the same name, this notices. Skipped where the model is not downloaded."""
    m = MODELS[model_id]
    path = MODELS_DIR / m.filename
    if not path.is_file():
        pytest.skip(f"{m.filename} is not downloaded here")
    prefix, growing = GEOMETRY[model_id]
    h = _gguf_header(path, prefix)
    layers = h["block_count"]
    heads = h["attention.head_count_kv"]
    k, v = h["attention.key_length"], h.get("attention.value_length", h["attention.key_length"])
    if growing == "all":
        per_token = layers * heads * (k + v) * 2
    elif growing == "half":
        per_token = layers // 2 * heads * (k + v) * 2
    elif growing == "interval":
        per_token = layers // h["full_attention_interval"] * heads * (k + v) * 2
    else:
        pattern = h["attention.sliding_window_pattern"]
        per_token = sum(hd * (k + v) * 2 for hd, windowed in zip(heads, pattern, strict=True) if not windowed)
    assert m.kv_bytes_per_token == per_token, f"{model_id}: header says {per_token} bytes per token"
    assert m.context_window <= h["context_length"] or m.yarn is not None, (
        f"{model_id}: registry context exceeds the GGUF's"
    )


def test_prompt_budgets_come_from_one_function_that_follows_the_launcher(monkeypatch):
    """chat, research and summarize used to read the registry window each on
    their own; after the launcher started clamping -c, prompts were budgeted
    against a context llama-server was not running with."""
    import re

    from harbor_clerk.llm import chat, research, summarize
    from harbor_clerk.llm.models import context_budget

    monkeypatch.setattr("harbor_clerk.llm.models.system_ram_bytes", lambda: 16_000_000_000)
    assert context_budget(None, False) == 32768
    assert context_budget(_info(), False) == max_context(_info(), 16_000_000_000) == 26624, (
        "clamped, as the launcher clamps"
    )
    for module in (chat, research, summarize):
        code = "\n".join(
            line for line in Path(module.__file__).read_text().splitlines() if not line.lstrip().startswith("#")
        )
        assert "context_budget(" in code, module.__name__
        assert not re.search(r"\.context_window\b|\.extended_context\b", code), (
            f"{module.__name__} reads the registry window directly"
        )

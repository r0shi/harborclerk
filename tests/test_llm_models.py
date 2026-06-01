from harbor_clerk.llm.models import MODELS, ModelInfo


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

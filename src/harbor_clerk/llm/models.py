"""Curated model registry for local LLM inference.

Notes from the 2026-04-28 cross-topic research sweep:

- Best for narrative/historical questions: Qwen3.6 35B-A3B. Strongest
  document-shaped output (executive summary, themed sections). Prefer
  for questions like "trace the evolution of X" or "what does the
  corpus document about Y over time".
- Best for comparative/tabular questions: GPT-OSS 20B. Natively uses
  markdown tables; highest citation density across the sweep.
- Reliable mid-size pick: Gemma 4 26B-A4B. Consistent, somewhat terse.
- Not recommended for research mode: SmolLM3 3B. Confabulated
  off-topic content from query keywords without producing citations
  on 2 of 3 sweep topics. Marked supports_research=False below.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass

# What a running llama-server needs beyond weights and KV cache: compute
# buffers, Metal scratch, the process itself. Measured on the mini as the gap
# between file + KV and the server's RSS (about 0.8 GB for an 8B at 32K).
RUNTIME_OVERHEAD_BYTES = 1_000_000_000
# What the rest of the machine needs while the model runs: the OS, the
# embedder and reranker, Postgres, the Tika JVM and the API. Six GB is the
# floor observed on the mini with the whole app up and nothing else.
HOST_HEADROOM_BYTES = 6_000_000_000
# Physical memory Apple sells Macs with, for rounding a requirement up. A
# "16 GB" Mac has 16 GiB, so requirements are compared in GiB.
MAC_RAM_TIERS_GB = (8, 16, 18, 24, 32, 36, 48, 64, 96, 128, 192, 256, 512)
GIB = 1024**3


@dataclass(frozen=True)
class YarnConfig:
    """YaRN context extension parameters for llama-server."""

    extended_context: int  # target context size with YaRN enabled
    rope_scale: float  # RoPE scaling factor (e.g. 4.0 for 32K→131K)
    original_context: int  # original training context
    attn_factor: float | None = None  # attention scaling (model-specific)


@dataclass(frozen=True)
class ModelInfo:
    id: str
    name: str
    huggingface_repo: str
    filename: str
    size_bytes: int
    context_window: int
    supports_tools: bool
    # KV cache the model needs per token of context, in bytes, for the layers
    # whose cache grows with context: K and V, f16, summed over those layers
    # (2 × heads_kv × (key_length + value_length) × 2 bytes per layer). Read
    # from each GGUF's header; tests/test_llm_models.py checks the value
    # against the file when it is present. Sliding-window layers and
    # linear-attention state do not grow and go in kv_fixed_bytes. The app
    # passes context_window to llama-server as -c, which is the total across
    # slots, so slots do not multiply this.
    kv_bytes_per_token: int = 0
    kv_fixed_bytes: int = 0
    yarn: YarnConfig | None = None  # None = YaRN not applicable
    # Whether this model is recommended for Research mode. Set False for
    # models that have demonstrated unreliable behaviour in research (e.g.
    # confabulating from query keywords without citations). Chat mode and
    # MCP-driven external use is unaffected. The API blocks starting a
    # Research task when the active model has supports_research=False.
    supports_research: bool = True
    # Per-model override for kb_find_all's default max_results. None means
    # "use the tool's static default of 100". The MCP server still clamps
    # at settings.find_all_max_results_cap regardless. This is the per-model
    # experimentation surface — small local models may want 30, larger
    # ones 150. All 8 curated models start at None.
    find_all_default_max_results: int | None = None
    # llama-server `-np` value. Heavy models (>15 GB GGUF) can't afford
    # more than 1 KV cache slot on consumer Macs (each slot adds ~3-5 GB of
    # KV memory). Mid models (5-12 GB) tolerate 2 slots. Small models
    # (≤4 GB) handle 4 slots comfortably even on 18 GB unified memory.
    # Per-model so we don't bottleneck small models on a global -np 1, and
    # don't OOM heavy ones on a global -np 4. With 1 LLM worker today, the
    # batch-summarize gain requires also scaling worker count (deferred);
    # the immediate benefit is the chat-while-summarize case, where chat
    # gets its own slot instead of queueing behind the worker's request.
    parallel_slots: int = 1


MODELS: dict[str, ModelInfo] = {
    m.id: m
    for m in [
        ModelInfo(
            id="qwen3-8b",
            name="Qwen3 8B",
            huggingface_repo="Qwen/Qwen3-8B-GGUF",
            filename="Qwen3-8B-Q4_K_M.gguf",
            size_bytes=5_027_783_488,  # the file's exact size: the launcher measures the file, so Python must match
            context_window=32768,
            supports_tools=True,
            # 36 layers × 8 KV heads × (128 + 128) × 2 bytes: 144 KB per token, 4.7 GB at 32K, 18.9 GB with YaRN at 131K.
            kv_bytes_per_token=147_456,
            yarn=YarnConfig(extended_context=131072, rope_scale=4.0, original_context=32768),
            parallel_slots=2,  # mid tier (5-12 GB)
        ),
        ModelInfo(
            id="qwen3-4b",
            name="Qwen3 4B",
            huggingface_repo="Qwen/Qwen3-4B-GGUF",
            filename="Qwen3-4B-Q4_K_M.gguf",
            size_bytes=2_497_280_256,
            context_window=32768,
            supports_tools=True,
            kv_bytes_per_token=147_456,  # same attention geometry as the 8B: 36 layers × 8 KV heads × 128
            yarn=YarnConfig(extended_context=131072, rope_scale=4.0, original_context=32768),
            # Exception to the small-tier "-np 4" rule: at -np 4 the per-slot
            # context is 32K/4 = 8K, which the 2026-05-31 v3 sweep showed is
            # too tight for the chat tools schema (~2K tokens) + an ambiguous
            # search result on the synthetic corpus, causing the model to
            # emit empty answers when the prompt overflows. -np 1 restores
            # the full 32K per request and lets qwen3-4b handle ambiguous
            # queries that 3.5× its slot budget at -np 4. Throughput cost:
            # summarize + chat serialize for this model only.
            parallel_slots=1,
        ),
        ModelInfo(
            id="gemma4-26b-a4b",
            name="Gemma 4 26B-A4B",
            huggingface_repo="bartowski/google_gemma-4-26B-A4B-it-GGUF",
            filename="google_gemma-4-26B-A4B-it-Q4_K_M.gguf",
            size_bytes=17_035_038_112,
            # 262144 is what the GGUF and Google's card give for 26B-A4B (#548); 128K is the E2B/E4B figure.
            context_window=262144,
            supports_tools=True,
            # 5 of 30 layers attend globally (2 KV heads × (512 + 512) × 2 bytes each): 20 KB per token, 5.2 GB
            # at 262K. The other 25 use a 1024-token sliding window at 8 KV heads × (256 + 256) × 2 bytes: fixed.
            kv_bytes_per_token=20_480,
            kv_fixed_bytes=209_715_200,
            parallel_slots=1,  # heavy tier (>15 GB)
        ),
        ModelInfo(
            id="gpt-oss-20b",
            name="GPT-OSS 20B",
            huggingface_repo="unsloth/gpt-oss-20b-GGUF",
            filename="gpt-oss-20b-Q4_K_M.gguf",
            size_bytes=11_624_759_488,
            context_window=128000,
            supports_tools=True,
            # Layers alternate full attention and a 128-token sliding window (llama.cpp's gpt-oss pattern; the
            # header carries only the window). 12 full layers × 8 KV heads × (64 + 64) × 2 bytes: 24 KB per token,
            # 3.1 GB at 128K; the 12 window layers hold 3 MB between them.
            kv_bytes_per_token=24_576,
            kv_fixed_bytes=3_145_728,
            # Heavy tier by the 2026-05 tuning: with 128K of context, splitting
            # the KV cache across two slots leaves each too little, and the
            # extra slot was not worth it on 18 GB Macs. (-c is the total
            # across slots, so slots do not add memory; they divide context.)
            parallel_slots=1,
        ),
        ModelInfo(
            id="qwen36-35b-a3b",
            name="Qwen3.6 35B-A3B",
            huggingface_repo="unsloth/Qwen3.6-35B-A3B-GGUF",
            filename="Qwen3.6-35B-A3B-UD-Q4_K_M.gguf",
            size_bytes=22_134_528_992,
            context_window=262144,
            supports_tools=True,
            # Hybrid: one layer in four keeps a KV cache (full_attention_interval = 4, so 10 of 40), at 2 KV heads
            # × (256 + 256) × 2 bytes: 20 KB per token, 5.2 GB at 262K. The 30 linear-attention layers carry a
            # fixed recurrent state (ssm inner 4096 × state 128, plus a conv window), about 300 MB.
            kv_bytes_per_token=20_480,
            kv_fixed_bytes=300_000_000,
            parallel_slots=1,  # heavy tier (>15 GB)
        ),
    ]
}


def get_model(model_id: str) -> ModelInfo | None:
    return MODELS.get(model_id)


def kv_bytes(model: ModelInfo, context: int) -> int:
    return model.kv_fixed_bytes + model.kv_bytes_per_token * context


def memory_bytes(model: ModelInfo, context: int | None = None) -> int:
    """What llama-server needs to serve `model` at `context` tokens: weights,
    KV cache and runtime overhead. Unified memory lets it allocate more than
    the Mac has; the machine then swaps until the kernel watchdog panics
    (the mini did, 2026-09-17), so this number is what decides, not the
    server."""
    return (
        model.size_bytes
        + kv_bytes(model, model.context_window if context is None else context)
        + RUNTIME_OVERHEAD_BYTES
    )


def ram_required_bytes(model: ModelInfo, context: int | None = None) -> int:
    """Physical memory a Mac needs to run `model` at `context` with the rest
    of the app and the OS still resident."""
    return memory_bytes(model, context) + HOST_HEADROOM_BYTES


def min_ram_gb(model: ModelInfo, context: int | None = None) -> int:
    """`ram_required_bytes` rounded up to a memory size Macs come with."""
    need = ram_required_bytes(model, context) / GIB
    return next((tier for tier in MAC_RAM_TIERS_GB if tier >= need), math.ceil(need))


def requested_context(model: ModelInfo, yarn_enabled: bool) -> int:
    """The context the app asks llama-server for: the YaRN-extended window when
    YaRN is on and the model has one, else the model's own."""
    return model.yarn.extended_context if yarn_enabled and model.yarn else model.context_window


def max_context(model: ModelInfo, ram_bytes: int, requested: int | None = None) -> int:
    """The largest context, up to `requested` (the model's own by default),
    that fits a Mac with `ram_bytes` of physical memory; 0 when the weights
    alone do not fit. Rounded down to a multiple of 1024, and never below
    4096 unless 0: a smaller context is not worth running."""
    requested = model.context_window if requested is None else requested
    spare = ram_bytes - HOST_HEADROOM_BYTES - RUNTIME_OVERHEAD_BYTES - model.size_bytes - model.kv_fixed_bytes
    if spare <= 0:
        return 0
    tokens = requested if model.kv_bytes_per_token == 0 else min(requested, spare // model.kv_bytes_per_token)
    tokens -= tokens % 1024
    return int(tokens) if tokens >= 4096 else 0


def effective_context(model: ModelInfo, yarn_enabled: bool, ram_bytes: int | None = None) -> int:
    """The context llama-server is running with on a Mac running the app,
    which is what prompts must be budgeted against: the requested window,
    clamped by the macOS launcher to what fits physical memory with the same
    arithmetic (`MemoryBudget` in Settings.swift). When memory cannot be read,
    or the model does not fit at all (the launcher then refuses to start it),
    the requested window is returned: there is nothing better to budget by.
    Under Compose nothing clamps: llama-server runs at the fixed `-c` in
    docker-compose.yml, and this is an upper bound on what it accepts (#654)."""
    requested = requested_context(model, yarn_enabled)
    ram = system_ram_bytes() if ram_bytes is None else ram_bytes
    fit = max_context(model, ram, requested)  # 0 when memory is unknown, or the model does not fit at all
    return fit if fit > 0 else requested


def context_budget(model: ModelInfo | None, yarn_enabled: bool) -> int:
    """What chat, research and summarize budget prompts against: the effective
    context of the active model, or 32768 when no model is active. One
    function, so the three cannot drift from each other or from the launcher."""
    return effective_context(model, yarn_enabled) if model else 32768


def system_ram_bytes() -> int:
    """Physical memory of this machine, 0 when it cannot be read."""
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        return 0


def list_models() -> list[ModelInfo]:
    return list(MODELS.values())


# Default research settings per model tier.
# Per-model overrides stored in model_settings DB table.
_LARGE_MODEL_THRESHOLD_BYTES = 4_000_000_000  # ~4GB → 8B+ params


def default_research_strategy(model_id: str) -> str:
    """Return 'search' for large models, 'sweep' for small."""
    model = MODELS.get(model_id)
    if model is None:
        return "search"
    return "search" if model.size_bytes >= _LARGE_MODEL_THRESHOLD_BYTES else "sweep"


DEFAULT_RESEARCH_MAX_ROUNDS = 20

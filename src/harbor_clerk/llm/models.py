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
MIB = 1024**2
# llama-server's prompt cache: host RAM where it keeps the KV state of a conversation it has put aside,
# so that coming back to it restores the prefix instead of reading it again. Its default limit is 8 GiB
# and nothing budgeted it (#657). Measured on the mini, 2026-09-18, Qwen3-8B, three conversations taken in
# turn: left alone it grew 2.2 GB in nine requests (it keeps a state per version of a conversation) and
# every return was restored, 30 s instead of 100 to 130. So it is worth having, and the launcher now
# gives it what the context leaves, never more than llama-server's own default: a Mac with room behaves as
# it did, and a tight one is bounded by what it has.
PROMPT_CACHE_MAX_BYTES = 8 * GIB
# The limit is also a limit on each state: at the pin, a state larger than --cache-ram is not cached at all
# ("exceeds cache size limit, skipping", server_prompt_cache::alloc). A bound that cannot hold a
# conversation of this many tokens for the active model is a cache in name only, and is switched off.
# A saved state carries the slot's context checkpoints with it, which is why the threshold is measured with
# `kv_bytes` (fixed cost and checkpoints included). The launcher keeps LLAMA_CTX_CHECKPOINTS of them, so the
# checkpoints no longer make a state outgrow a bound that held its first turns, as 32 of them could. Its
# per-token KV still grows with the conversation: under a small bound, a long one is still not restored.
PROMPT_CACHE_MIN_TOKENS = 4096
# The llama.cpp release whose prompt-cache behaviour the two lines above were read from. The May build
# (b9018) kept one state whatever the limit, which would make a small bound unsafe. A pin bump re-reads
# server_prompt_cache::alloc and moves this with it; tests/test_llm_models.py holds the two together.
PROMPT_CACHE_SEMANTICS_READ_AT = "v0.4.1"


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
    # Bytes of one context checkpoint: a copy of the part of the model's memory llama-server cannot roll
    # back (the sliding-window cache, or the recurrent state). 0 for plain attention, which makes none.
    # The launcher keeps LLAMA_CTX_CHECKPOINTS of them per slot; `fixed_bytes()` is what the budget uses.
    checkpoint_bytes: int = 0
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
    # llama-server `-np` value. `-c` is the total across slots, so a slot does
    # not add KV memory: it divides the context, and a request can use only
    # context / slots tokens (`context_budget` accounts for that). Every
    # curated model runs one slot (owner's decision, 2026-09-18): the product
    # barely issues parallel requests, and on a Mac the whole window for each
    # job is worth more than chat not queueing behind a summary.
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
            parallel_slots=1,
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
            # The evidence behind one slot everywhere: at -np 4 this model had
            # 32K/4 = 8K per request, which the 2026-05-31 v3 sweep showed is
            # too tight for the chat tools schema (~2K tokens) + an ambiguous
            # search result, and the model emitted empty answers when the
            # prompt overflowed.
            parallel_slots=1,
        ),
        ModelInfo(
            id="qwen35-9b",
            name="Qwen3.5 9B",
            huggingface_repo="unsloth/Qwen3.5-9B-GGUF",
            filename="Qwen3.5-9B-Q4_K_M.gguf",
            size_bytes=5_680_522_464,
            # The GGUF's native window; no YaRN needed. Hybrid: one layer in four keeps a KV cache
            # (full_attention_interval = 4, so 8 of 32) at 4 KV heads x (256 + 256) x 2 bytes: 32 KB per token,
            # 8.6 GB at 262K, which is more than the weights. The launcher clamps -c to what the Mac fits
            # (see below). The 24 linear-attention layers carry a fixed recurrent state per
            # slot: (state 128 x inner 4096 + conv 3 x (4096 + 2 x 16 groups x 128)) x 4 bytes = 2,195,456 bytes
            # per layer, 52,690,944 for the slot, and as much again for each context checkpoint (a copy of it).
            context_window=262144,
            supports_tools=True,
            kv_bytes_per_token=32_768,
            kv_fixed_bytes=52_690_944,
            checkpoint_bytes=52_690_944,
            parallel_slots=1,
        ),
        ModelInfo(
            id="qwen35-4b",
            name="Qwen3.5 4B",
            huggingface_repo="unsloth/Qwen3.5-4B-GGUF",
            filename="Qwen3.5-4B-Q4_K_M.gguf",
            size_bytes=2_740_937_888,
            context_window=262144,
            supports_tools=True,
            kv_bytes_per_token=32_768,  # same attention geometry as the 9B: 8 of 32 layers, 4 KV heads x 512
            kv_fixed_bytes=52_690_944,
            checkpoint_bytes=52_690_944,
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
            # at 262K. The other 25 use a 1024-token sliding window at 8 KV heads × (256 + 256) × 2 bytes per
            # cell, over swa_cache_cells(1024) = 1536 cells, not 1024: fixed.
            kv_bytes_per_token=20_480,
            kv_fixed_bytes=314_572_800,
            checkpoint_bytes=314_572_800,  # a checkpoint copies the sliding-window cache: at most all of it
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
            # 3.1 GB at 128K; the 12 window layers hold the same per cell over swa_cache_cells(128) = 768
            # cells, not 128: 19 MB between them.
            kv_bytes_per_token=24_576,
            kv_fixed_bytes=18_874_368,
            checkpoint_bytes=18_874_368,
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
            # fixed recurrent state: (state 128 × inner 4096 + conv 3 × (4096 + 2 × 16 groups × 128)) × 4 bytes =
            # 2,195,456 per layer, 65,863,680 for the slot, and as much again for each context checkpoint.
            # (It was "about 300 MB", an estimate; this is from the header.)
            kv_bytes_per_token=20_480,
            kv_fixed_bytes=65_863_680,
            checkpoint_bytes=65_863_680,
            parallel_slots=1,  # heavy tier (>15 GB)
        ),
    ]
}


def get_model(model_id: str) -> ModelInfo | None:
    return MODELS.get(model_id)


# Context checkpoints the launcher lets llama-server keep per slot (--ctx-checkpoints). Its default is 32,
# in host RAM, and nothing bounded or budgeted them (#657): for the 26B that is up to 10 GB.
#
# A model whose memory cannot be rolled back (sliding window, recurrent) can only reuse a prefix from a
# checkpoint at or before the point where the new prompt departs from the old one. At the pin
# (tools/server/server-context.cpp, v0.4.1) a request leaves up to three that matter: one at the last user
# message, and two just before the end of the prompt (4 + n_ubatch and 4 tokens before it). The next turn
# departs where the previous reply was re-rendered, so it restores from the newest of those; the oldest are
# evicted first, which never takes that one. Three is one request's worth. Read from source, not measured.
LLAMA_CTX_CHECKPOINTS = 3

# llama-server's default physical batch (-ub). The launcher does not pass one.
LLAMA_UBATCH = 512


def swa_cache_cells(window: int, ubatch: int = LLAMA_UBATCH) -> int:
    """Cells llama.cpp allocates for a sliding-window layer's KV cache: not the window, but the window plus a
    micro-batch, padded to 256 (`llama_kv_cache_iswa`, v0.4.1: GGML_PAD(min(size_base, n_swa + n_ubatch),
    256), one sequence). A `kv_fixed_bytes` for a sliding-window model is its bytes per cell times this.
    The registry had used the window alone, which under-budgeted Gemma 4 by a third."""
    return -(-(window + ubatch) // 256) * 256


def fixed_bytes(model: ModelInfo) -> int:
    """What the model holds whatever the context: its fixed KV (sliding-window cache, recurrent state) and
    the context checkpoints the launcher lets llama-server keep of it, per slot."""
    return model.kv_fixed_bytes + model.parallel_slots * LLAMA_CTX_CHECKPOINTS * model.checkpoint_bytes


def kv_bytes(model: ModelInfo, context: int) -> int:
    return fixed_bytes(model) + model.kv_bytes_per_token * context


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
    spare = ram_bytes - HOST_HEADROOM_BYTES - RUNTIME_OVERHEAD_BYTES - model.size_bytes - fixed_bytes(model)
    if spare <= 0:
        return 0
    tokens = requested if model.kv_bytes_per_token == 0 else min(requested, spare // model.kv_bytes_per_token)
    tokens -= tokens % 1024
    return int(tokens) if tokens >= 4096 else 0


def prompt_cache_mib(model: ModelInfo, ram_bytes: int, context: int) -> int:
    """What the launcher passes as `--cache-ram`, in MiB: what is left of this Mac's memory once the model,
    its KV cache at `context`, the runtime and the rest of the machine have theirs, up to llama-server's own
    default (PROMPT_CACHE_MAX_BYTES). 0, which switches the cache off, when what is left could not hold a
    PROMPT_CACHE_MIN_TOKENS-token state of this model, or memory cannot be read.

    Context first, cache second. The cache is a speed-up and the context is what the model can do:
    reserving even 512 MiB ahead of the context would cut gpt-oss-20b on an 18 GB Mac from 27K tokens to
    6K. So a Mac whose context is already clamped to what fits gets no cache, and says so in its log.

    Nothing in the Python app calls this: only the Swift launcher starts llama-server. It is the reference
    that `MemoryBudget.promptCacheMiB` is tested against, case for case."""
    left = ram_bytes - HOST_HEADROOM_BYTES - memory_bytes(model, context)
    mib = min(PROMPT_CACHE_MAX_BYTES, left) // MIB
    return int(mib) if mib * MIB >= kv_bytes(model, PROMPT_CACHE_MIN_TOKENS) else 0


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
    """What chat, research and summarize budget prompts against: what ONE
    request can use, which is the effective context divided by the slots
    (llama-server splits -c across -np slots; `-c 32768 -np 2` reports 16384
    per slot). 32768 when no model is active. One function, so the three
    cannot drift from each other or from the launcher."""
    if not model:
        return 32768
    return effective_context(model, yarn_enabled) // max(1, model.parallel_slots)


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

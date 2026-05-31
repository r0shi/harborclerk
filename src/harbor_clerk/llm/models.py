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

from dataclasses import dataclass


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
            size_bytes=5_030_000_000,
            context_window=32768,
            supports_tools=True,
            yarn=YarnConfig(extended_context=131072, rope_scale=4.0, original_context=32768),
            parallel_slots=2,  # mid tier (5-12 GB)
        ),
        ModelInfo(
            id="qwen3-4b",
            name="Qwen3 4B",
            huggingface_repo="Qwen/Qwen3-4B-GGUF",
            filename="Qwen3-4B-Q4_K_M.gguf",
            size_bytes=2_500_000_000,
            context_window=32768,
            supports_tools=True,
            yarn=YarnConfig(extended_context=131072, rope_scale=4.0, original_context=32768),
            parallel_slots=4,  # small tier (≤4 GB)
        ),
        ModelInfo(
            id="deepseek-r1-0528-8b",
            name="DeepSeek R1 0528 8B",
            huggingface_repo="unsloth/DeepSeek-R1-0528-Qwen3-8B-GGUF",
            filename="DeepSeek-R1-0528-Qwen3-8B-Q4_K_M.gguf",
            size_bytes=5_400_000_000,
            context_window=32768,
            supports_tools=True,
            yarn=YarnConfig(extended_context=131072, rope_scale=4.0, original_context=32768, attn_factor=0.8782),
            parallel_slots=2,  # mid tier (5-12 GB)
        ),
        ModelInfo(
            id="gemma4-26b-a4b",
            name="Gemma 4 26B-A4B",
            huggingface_repo="bartowski/google_gemma-4-26B-A4B-it-GGUF",
            filename="google_gemma-4-26B-A4B-it-Q4_K_M.gguf",
            size_bytes=17_000_000_000,
            context_window=128000,
            supports_tools=True,
            parallel_slots=1,  # heavy tier (>15 GB)
        ),
        ModelInfo(
            id="gpt-oss-20b",
            name="GPT-OSS 20B",
            huggingface_repo="unsloth/gpt-oss-20b-GGUF",
            filename="gpt-oss-20b-Q4_K_M.gguf",
            size_bytes=11_600_000_000,
            context_window=128000,
            supports_tools=True,
            # Heavy tier despite MoE active-params being smaller, because the
            # native context window is 128K and llama-server allocates KV cache
            # for all slots upfront. ~7 GB weights + 2 × 128K KV would push
            # past the ~11 GB free after weights on 18 GB unified memory. If
            # post-deploy memory headroom proves comfortable on YaRN-disabled
            # 24/36 GB Macs, this can be re-evaluated to 2.
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
            parallel_slots=1,  # heavy tier (>15 GB)
        ),
    ]
}


def get_model(model_id: str) -> ModelInfo | None:
    return MODELS.get(model_id)


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

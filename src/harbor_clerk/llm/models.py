"""Curated model registry for local LLM inference."""

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
        ),
        ModelInfo(
            id="phi4-mini",
            name="Phi-4 Mini 3.8B",
            huggingface_repo="bartowski/microsoft_Phi-4-mini-instruct-GGUF",
            filename="microsoft_Phi-4-mini-instruct-Q4_K_M.gguf",
            size_bytes=2_670_000_000,
            context_window=128000,
            supports_tools=True,
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
        ),
        ModelInfo(
            id="gemma3-4b",
            name="Gemma 3 4B",
            huggingface_repo="bartowski/google_gemma-3-4b-it-GGUF",
            filename="google_gemma-3-4b-it-Q4_K_M.gguf",
            size_bytes=2_670_000_000,
            context_window=128000,
            supports_tools=True,
        ),
        ModelInfo(
            id="smollm3-3b",
            name="SmolLM3 3B",
            huggingface_repo="bartowski/HuggingFaceTB_SmolLM3-3B-GGUF",
            filename="HuggingFaceTB_SmolLM3-3B-Q4_K_M.gguf",
            size_bytes=2_060_000_000,
            context_window=65536,
            supports_tools=True,
            yarn=YarnConfig(extended_context=131072, rope_scale=2.0, original_context=65536),
        ),
        ModelInfo(
            id="llama3.1-8b",
            name="Llama 3.1 8B Instruct",
            huggingface_repo="bartowski/Meta-Llama-3.1-8B-Instruct-GGUF",
            filename="Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf",
            size_bytes=4_920_000_000,
            context_window=128000,
            supports_tools=True,
        ),
    ]
}


def get_model(model_id: str) -> ModelInfo | None:
    return MODELS.get(model_id)


def list_models() -> list[ModelInfo]:
    return list(MODELS.values())

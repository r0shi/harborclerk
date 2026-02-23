"""Curated model registry for local LLM inference."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelInfo:
    id: str
    name: str
    huggingface_repo: str
    filename: str
    size_bytes: int
    context_window: int
    supports_tools: bool


MODELS: dict[str, ModelInfo] = {
    m.id: m
    for m in [
        ModelInfo(
            id="qwen2.5-7b",
            name="Qwen 2.5 7B Instruct",
            huggingface_repo="Qwen/Qwen2.5-7B-Instruct-GGUF",
            filename="qwen2.5-7b-instruct-q4_k_m.gguf",
            size_bytes=4_680_000_000,
            context_window=8192,
            supports_tools=True,
        ),
        ModelInfo(
            id="qwen2.5-3b",
            name="Qwen 2.5 3B Instruct",
            huggingface_repo="Qwen/Qwen2.5-3B-Instruct-GGUF",
            filename="qwen2.5-3b-instruct-q4_k_m.gguf",
            size_bytes=2_060_000_000,
            context_window=8192,
            supports_tools=True,
        ),
        ModelInfo(
            id="llama3.2-3b",
            name="Llama 3.2 3B Instruct",
            huggingface_repo="bartowski/Llama-3.2-3B-Instruct-GGUF",
            filename="Llama-3.2-3B-Instruct-Q4_K_M.gguf",
            size_bytes=2_020_000_000,
            context_window=8192,
            supports_tools=True,
        ),
        ModelInfo(
            id="mistral-7b",
            name="Mistral 7B Instruct v0.3",
            huggingface_repo="bartowski/Mistral-7B-Instruct-v0.3-GGUF",
            filename="Mistral-7B-Instruct-v0.3-Q4_K_M.gguf",
            size_bytes=4_370_000_000,
            context_window=8192,
            supports_tools=True,
        ),
        ModelInfo(
            id="deepseek-r1-8b",
            name="DeepSeek R1 8B",
            huggingface_repo="bartowski/DeepSeek-R1-Distill-Qwen-8B-GGUF",
            filename="DeepSeek-R1-Distill-Qwen-8B-Q4_K_M.gguf",
            size_bytes=4_940_000_000,
            context_window=8192,
            supports_tools=False,
        ),
    ]
}


def get_model(model_id: str) -> ModelInfo | None:
    return MODELS.get(model_id)


def list_models() -> list[ModelInfo]:
    return list(MODELS.values())

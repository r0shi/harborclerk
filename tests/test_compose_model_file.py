"""Under Docker Compose the app follows the model and context llama-server is started with (#654).

Compose starts llama-server with `-m /models/$LLM_MODEL_FILE -c $LLM_CONTEXT_TOKENS` and gives `app`
and `worker-llm` the same two variables. The active model is that file's curated model while the file
is in the models volume, and none while it is not; prompts are budgeted to that context. Under the
macOS app (a native config.json) neither applies.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

import harbor_clerk.config as config
from harbor_clerk.llm.models import GIB, context_budget, effective_context, get_model, max_context

MODEL_FILE = "Qwen3.5-4B-Q4_K_M.gguf"  # qwen35-4b


@pytest.fixture
def compose_env(monkeypatch, tmp_path: Path) -> Path:
    """A Compose-like environment: no native config, LLM_MODEL_FILE set, an empty models volume."""
    models = tmp_path / "models"
    models.mkdir()
    monkeypatch.delenv("NATIVE_CONFIG_FILE", raising=False)
    monkeypatch.delenv("LLM_MODEL_ID", raising=False)
    monkeypatch.setenv("MODELS_DIR", str(models))
    monkeypatch.setenv("LLM_MODEL_FILE", MODEL_FILE)
    monkeypatch.setattr(config, "_model_file_reported", None)
    config._settings = None
    return models


def test_the_model_is_active_while_its_file_is_in_the_volume(compose_env: Path) -> None:
    (compose_env / MODEL_FILE).write_bytes(b"GGUF")
    assert config.get_settings().llm_model_id == "qwen35-4b"


def test_no_model_is_active_until_the_file_is_there(compose_env: Path, monkeypatch, caplog) -> None:
    # A stale LLM_MODEL_ID does not make a model active that llama-server is not serving: chat would
    # send requests to a server that is not listening, and every summary would spend its retries.
    monkeypatch.setenv("LLM_MODEL_ID", "qwen35-9b")
    config._settings = None
    with caplog.at_level(logging.INFO, logger="harbor_clerk.config"):
        assert config.get_settings().llm_model_id == ""
    assert "LLM_MODEL_ID=qwen35-9b is ignored" in caplog.text
    assert f"LLM_MODEL_FILE {MODEL_FILE} is not in" in caplog.text


def test_a_download_is_picked_up_by_the_next_refresh(compose_env: Path) -> None:
    settings = config.get_settings()
    assert settings.llm_model_id == ""
    (compose_env / MODEL_FILE).write_bytes(b"GGUF")
    config.refresh_llm_settings()
    assert settings.llm_model_id == "qwen35-4b"
    (compose_env / MODEL_FILE).unlink()
    config.refresh_llm_settings()
    assert settings.llm_model_id == ""


def test_a_file_that_is_not_curated_activates_nothing(compose_env: Path, monkeypatch, caplog) -> None:
    monkeypatch.setenv("LLM_MODEL_FILE", "my-own-model.gguf")
    (compose_env / "my-own-model.gguf").write_bytes(b"GGUF")
    config._settings = None
    with caplog.at_level(logging.WARNING, logger="harbor_clerk.config"):
        assert config.get_settings().llm_model_id == ""
    assert "not a curated model's file" in caplog.text


def test_an_empty_model_file_leaves_the_model_id_alone(compose_env: Path, monkeypatch) -> None:
    monkeypatch.setenv("LLM_MODEL_FILE", "")
    monkeypatch.setenv("LLM_MODEL_ID", "qwen35-9b")
    config._settings = None
    assert config.get_settings().llm_model_id == "qwen35-9b"
    config.refresh_llm_settings()
    assert config.get_settings().llm_model_id == "qwen35-9b"


def test_the_macos_app_ignores_llm_model_file(compose_env: Path, monkeypatch, tmp_path: Path) -> None:
    (compose_env / MODEL_FILE).write_bytes(b"GGUF")
    native = tmp_path / "config.json"
    native.write_text(json.dumps({"llm_model_id": "gpt-oss-20b"}))
    monkeypatch.setenv("NATIVE_CONFIG_FILE", str(native))
    config._settings = None
    assert config.compose_model_file(config.get_settings()) == ""
    assert config.get_settings().llm_model_id == "gpt-oss-20b"
    config.refresh_llm_settings()
    assert config.get_settings().llm_model_id == "gpt-oss-20b"


# --- the context ---


def test_prompts_are_budgeted_to_the_context_llama_server_runs_at(monkeypatch) -> None:
    model = get_model("qwen35-4b")
    roomy = 64 * GIB
    assert max_context(model, roomy) > 8192  # the memory arithmetic alone would budget far more
    monkeypatch.setenv("LLM_CONTEXT_TOKENS", "8192")
    config._settings = None
    assert effective_context(model, False, ram_bytes=roomy) == 8192
    assert context_budget(model, False) == 8192


def test_the_context_is_never_more_than_the_model_asks_for(monkeypatch) -> None:
    model = get_model("qwen3-8b")  # a 32,768-token window
    monkeypatch.setenv("LLM_CONTEXT_TOKENS", "65536")
    config._settings = None
    assert effective_context(model, False, ram_bytes=64 * GIB) == 32768


def test_without_a_server_context_the_memory_arithmetic_decides(monkeypatch) -> None:
    model = get_model("qwen35-4b")
    monkeypatch.setenv("LLM_CONTEXT_TOKENS", "0")
    config._settings = None
    assert effective_context(model, False, ram_bytes=32 * GIB) == max_context(model, 32 * GIB)

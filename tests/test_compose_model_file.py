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
    monkeypatch.setenv("HC_DEPLOYMENT", "compose")  # what docker-compose.yml sets for app and worker-llm
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
    # The file is there and served: "download it" would be wrong advice.
    assert config.no_model_message(config.get_settings()) == (
        "LLM_MODEL_FILE my-own-model.gguf is not one of the models listed in Settings → Models; "
        "set it to one of their files in .env."
    )


def test_a_dot_slash_model_file_is_the_bare_file(compose_env: Path, monkeypatch) -> None:
    # llama-server reads /models/./name as /models/name, so the app treats `./name` as `name`.
    (compose_env / MODEL_FILE).write_bytes(b"GGUF")
    monkeypatch.setenv("LLM_MODEL_FILE", f"./{MODEL_FILE}")
    config._settings = None
    assert config.get_settings().llm_model_id == "qwen35-4b"
    assert config.compose_model_file(config.get_settings()) == MODEL_FILE


@pytest.mark.parametrize("written", [f"nosuch/../{MODEL_FILE}", f"{MODEL_FILE}/.", f"././{MODEL_FILE}"])
def test_only_a_leading_dot_slash_is_normalised(compose_env: Path, monkeypatch, written: str) -> None:
    # A lexical normalisation would claim the curated model for paths the kernel does not open as that file
    # (`nosuch/` does not exist; `name/.` needs a directory). Any other path component is no curated file name.
    (compose_env / MODEL_FILE).write_bytes(b"GGUF")
    monkeypatch.setenv("LLM_MODEL_FILE", written)
    config._settings = None
    settings = config.get_settings()
    assert settings.llm_model_id == ""
    assert config.compose_model_file(settings) != MODEL_FILE
    assert config.no_model_message(settings) == (
        f"LLM_MODEL_FILE {written} is not one of the models listed in Settings → Models; "
        "set it to one of their files in .env."
    )


def test_a_model_file_in_a_subdirectory_is_kept_as_written(compose_env: Path, monkeypatch) -> None:
    monkeypatch.setenv("LLM_MODEL_FILE", f"sub/{MODEL_FILE}")
    config._settings = None
    assert config.compose_model_file(config.get_settings()) == f"sub/{MODEL_FILE}"
    assert config.get_settings().llm_model_id == ""  # no curated file lives in a directory


def test_an_empty_model_file_means_no_model_whatever_llm_model_id_says(compose_env: Path, monkeypatch, caplog) -> None:
    # The documented "no local model": llama-server is not started, so the UI must not show one active.
    (compose_env / "Qwen3.5-9B-Q4_K_M.gguf").write_bytes(b"GGUF")
    monkeypatch.setenv("LLM_MODEL_FILE", "")
    monkeypatch.setenv("LLM_MODEL_ID", "qwen35-9b")
    config._settings = None
    with caplog.at_level(logging.INFO, logger="harbor_clerk.config"):
        assert config.get_settings().llm_model_id == ""
        config.refresh_llm_settings()
        assert config.get_settings().llm_model_id == ""
    assert "LLM_MODEL_ID=qwen35-9b is ignored" in caplog.text
    assert caplog.text.count("LLM_MODEL_FILE is empty") == 1  # said once, not on every refresh


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


def test_prompts_are_budgeted_to_the_context_llama_server_runs_at(compose_env: Path, monkeypatch) -> None:
    model = get_model("qwen35-4b")
    roomy = 64 * GIB
    assert max_context(model, roomy) > 8192  # the memory arithmetic alone would budget far more
    monkeypatch.setenv("LLM_CONTEXT_TOKENS", "8192")
    config._settings = None
    assert effective_context(model, False, ram_bytes=roomy) == 8192
    assert context_budget(model, False) == 8192


def test_the_context_is_never_more_than_the_model_asks_for(compose_env: Path, monkeypatch) -> None:
    model = get_model("qwen3-8b")  # a 32,768-token window
    monkeypatch.setenv("LLM_CONTEXT_TOKENS", "65536")
    config._settings = None
    assert effective_context(model, False, ram_bytes=64 * GIB) == 32768


def test_yarn_does_not_stretch_the_compose_context(compose_env: Path, monkeypatch) -> None:
    # The Compose server runs without RoPE scaling, and the YaRN toggle is the app's in-memory setting, which
    # worker-llm never sees: the window budgeted to is the unscaled one, toggle or not.
    model = get_model("qwen3-8b")  # 32,768 tokens, 131,072 with YaRN
    monkeypatch.setenv("LLM_CONTEXT_TOKENS", "65536")
    config._settings = None
    assert effective_context(model, True, ram_bytes=64 * GIB) == 32768
    assert context_budget(model, True) == 32768


def test_a_zero_context_under_compose_is_refused_for_the_compose_default(
    compose_env: Path, monkeypatch, caplog
) -> None:
    # llama-server reads `-c 0` as the model's training context; the app must not budget from anything else
    # bigger than the compose default, and says so.
    model = get_model("qwen35-4b")
    monkeypatch.setenv("LLM_CONTEXT_TOKENS", "0")
    config._settings = None
    with caplog.at_level(logging.ERROR, logger="harbor_clerk.config"):
        assert config.get_settings().llm_context_tokens == config.COMPOSE_DEFAULT_CONTEXT_TOKENS == 8192
    assert "LLM_CONTEXT_TOKENS=0 is not supported under Docker Compose" in caplog.text
    assert effective_context(model, False, ram_bytes=32 * GIB) == 8192


def test_outside_compose_a_zero_context_means_not_set(monkeypatch) -> None:
    # Unset (0) is the macOS app's normal state: the launcher sizes -c, and the memory arithmetic budgets.
    model = get_model("qwen35-4b")
    monkeypatch.setenv("LLM_CONTEXT_TOKENS", "0")
    config._settings = None
    assert config.get_settings().llm_context_tokens == 0
    assert effective_context(model, False, ram_bytes=32 * GIB) == max_context(model, 32 * GIB)


def test_a_model_file_ending_in_a_slash_is_not_configured(compose_env: Path, monkeypatch, caplog) -> None:
    # `/models/name.gguf/` is a directory: the entrypoint will not start llama-server on it, so the app must not
    # claim the model is active, and says why.
    (compose_env / MODEL_FILE).write_bytes(b"GGUF")
    monkeypatch.setenv("LLM_MODEL_FILE", f"{MODEL_FILE}/")
    config._settings = None
    with caplog.at_level(logging.ERROR, logger="harbor_clerk.config"):
        assert config.get_settings().llm_model_id == ""
    assert f"{MODEL_FILE}/" in caplog.text
    assert config.no_model_message(config.get_settings()) == (
        f"No local model is configured: LLM_MODEL_FILE {MODEL_FILE}/ ends in '/'; set it to a model's file name in .env."
    )


def test_the_macos_app_keeps_its_launchers_context(compose_env: Path, monkeypatch, tmp_path: Path) -> None:
    # Even with the marker and a context set, a native config.json means the launcher sized -c.
    native = tmp_path / "config.json"
    native.write_text("{}")
    monkeypatch.setenv("NATIVE_CONFIG_FILE", str(native))
    monkeypatch.setenv("LLM_CONTEXT_TOKENS", "8192")
    config._settings = None
    model = get_model("qwen35-4b")
    assert effective_context(model, False, ram_bytes=64 * GIB) == max_context(model, 64 * GIB) > 8192


# --- a .env beside a source run ---


def test_a_copied_env_file_does_not_make_a_source_run_compose(monkeypatch, tmp_path: Path) -> None:
    """The Docker quick start copies .env.example to .env, and Settings reads .env from the working
    directory, so a source run started there reads it too. Without HC_DEPLOYMENT, which only the compose
    file sets, nothing in it is Compose's: LLM_MODEL_ID stands, the context is the memory arithmetic's,
    and Activate is not refused (tests/api/test_chat_models_compose.py)."""
    models = tmp_path / "models"
    models.mkdir()
    (models / MODEL_FILE).write_bytes(b"GGUF")
    (tmp_path / ".env").write_text(
        f"LLM_MODEL_ID=qwen35-9b\nMODELS_DIR={models}\nLLM_MODEL_FILE={MODEL_FILE}\nLLM_CONTEXT_TOKENS=8192\n"
    )
    for name in ("LLM_MODEL_ID", "MODELS_DIR", "LLM_MODEL_FILE", "LLM_CONTEXT_TOKENS", "NATIVE_CONFIG_FILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("HC_DEPLOYMENT", raising=False)  # conftest pins it empty; a source run has none
    monkeypatch.chdir(tmp_path)
    config._settings = None
    settings = config.get_settings()
    assert settings.llm_model_file == MODEL_FILE and settings.llm_context_tokens == 8192  # the file was read
    assert not config.is_compose(settings)
    assert settings.llm_model_id == "qwen35-9b"
    config.refresh_llm_settings()
    assert settings.llm_model_id == "qwen35-9b"
    model = get_model("qwen35-4b")
    assert effective_context(model, False, ram_bytes=64 * GIB) == max_context(model, 64 * GIB)


def test_the_summarizer_says_to_download_the_compose_model_not_to_activate(
    compose_env: Path, monkeypatch, caplog
) -> None:
    from harbor_clerk.llm import summarize

    monkeypatch.setattr(summarize, "_apple_intelligence_summary", lambda *a, **k: None)
    config._settings = None
    with caplog.at_level(logging.WARNING, logger="harbor_clerk.llm.summarize"):
        _, used = summarize.generate_summary(["A short invoice from Acme for three widgets."])
    assert used == "extractive"
    assert f"Download {MODEL_FILE} in Settings" in caplog.text
    assert "Activate a model" not in caplog.text


# `²` and `٨١٩٢` are digits to str.isdigit() (and int() reads the second as 8192), but not to llama-server.
MALFORMED_CONTEXTS = ["8k", "16,384", "-1", "\u00b2", "\u0668\u0661\u0669\u0662"]


@pytest.mark.parametrize("bad", MALFORMED_CONTEXTS)
def test_a_malformed_context_does_not_stop_settings_from_constructing(monkeypatch, caplog, bad) -> None:
    # Every service reads .env, so a typo here must not crash-loop the app, the watcher and the workers.
    monkeypatch.setenv("LLM_CONTEXT_TOKENS", bad)
    config._settings = None
    with caplog.at_level(logging.ERROR, logger="harbor_clerk.config"):
        settings = config.get_settings()
    assert settings.llm_context_tokens == 0  # unset
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and repr(bad) in errors[0].getMessage()


@pytest.mark.parametrize("bad", MALFORMED_CONTEXTS)
def test_a_malformed_context_under_compose_budgets_to_the_compose_default(
    compose_env: Path, monkeypatch, caplog, bad
) -> None:
    monkeypatch.setenv("LLM_CONTEXT_TOKENS", bad)
    config._settings = None
    with caplog.at_level(logging.ERROR, logger="harbor_clerk.config"):
        settings = config.get_settings()
    assert settings.llm_context_tokens == config.COMPOSE_DEFAULT_CONTEXT_TOKENS
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and repr(bad) in errors[0].getMessage(), [r.getMessage() for r in errors]

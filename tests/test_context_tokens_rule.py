"""One rule for LLM_CONTEXT_TOKENS, applied identically by the app and by the llama-server entrypoint (#654).

The entrypoint decides the context llama-server runs at; the app decides the context prompts are budgeted to.
When the two disagree, prompts overflow (llama-server answers 400 and summaries go extractive) or llama-server
crashes. So both apply the same rule, `config.parse_context_tokens` and the entrypoint's `context_problem`:
trim ASCII whitespace, then 1 to 7 ASCII digits whose value is at least 1. Anything else is logged and runs as
the compose default. This table runs every row through both and requires the same effective context.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest

import harbor_clerk.config as config
from tests.test_docker_compose_llama import ENTRYPOINT, _env, _fake_server, needs_dash

DEFAULT = 8192  # config.COMPOSE_DEFAULT_CONTEXT_TOKENS, held to the entrypoint's by test_docker_compose_llama.py

# (LLM_CONTEXT_TOKENS as written in .env, the context both sides must run at)
RULE = [
    (" 16384", 16384),  # a quoted value with a space: trimmed on both sides, not rejected on one
    ("16384 ", 16384),
    ("\t8192", 8192),
    (" 4096", DEFAULT),  # a no-break space is not ASCII whitespace: str.strip() would trim it, dash does not
    ("4096", 4096),
    ("9999999", 9999999),  # the cap: 7 digits
    ("10000000", DEFAULT),  # 8 digits
    ("00", DEFAULT),  # zero is llama-server's full training context
    ("0", DEFAULT),
    ("", DEFAULT),
    ("8k", DEFAULT),  # std::stoi would read 8
    ("2147483648", DEFAULT),  # above INT_MAX: std::stoi throws
    ("99999999", DEFAULT),
    ("²", DEFAULT),  # a digit to str.isdigit(), not to llama-server
    ("-1", DEFAULT),
    ("+4096", DEFAULT),
]


def _entrypoint_context(tmp_path: Path, value: str) -> tuple[int, bytes]:
    """The context the entrypoint execs llama-server with for `-c value`, and what it printed."""
    fake, out = _fake_server(tmp_path)
    model = tmp_path / "Qwen3.5-4B-Q4_K_M.gguf"
    model.write_bytes(b"GGUF")
    result = subprocess.run(
        ["dash", str(ENTRYPOINT), "-m", str(model), "-c", value, "--port", "8080"],
        env=_env(fake),
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    args = out.read_text().splitlines()
    return int(args[args.index("-c") + 1]), result.stderr


def _app_context(monkeypatch, tmp_path: Path, value: str, caplog) -> tuple[int, list[str]]:
    """The context the app budgets to under Compose for LLM_CONTEXT_TOKENS=value, and the errors it logged."""
    models = tmp_path / "models"
    models.mkdir()
    monkeypatch.delenv("NATIVE_CONFIG_FILE", raising=False)
    monkeypatch.setenv("HC_DEPLOYMENT", "compose")
    monkeypatch.setenv("MODELS_DIR", str(models))
    monkeypatch.setenv("LLM_MODEL_FILE", "Qwen3.5-4B-Q4_K_M.gguf")
    monkeypatch.setenv("LLM_CONTEXT_TOKENS", value)
    config._settings = None
    with caplog.at_level(logging.ERROR, logger="harbor_clerk.config"):
        tokens = config.get_settings().llm_context_tokens
    return tokens, [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]


@needs_dash
@pytest.mark.parametrize(("value", "expected"), RULE, ids=[repr(v) for v, _ in RULE])
def test_the_app_and_the_entrypoint_run_the_same_context(monkeypatch, tmp_path: Path, caplog, value, expected) -> None:
    server, printed = _entrypoint_context(tmp_path, value)
    app, errors = _app_context(monkeypatch, tmp_path, value, caplog)
    assert server == app == expected, f"llama-server runs {server}, the app budgets {app}"
    valid = config.parse_context_tokens(value) is not None
    # An invalid value is said on both sides; a valid one on neither.
    assert (b"LLM_CONTEXT_TOKENS=" in printed) == (not valid), printed
    assert bool(errors) == (not valid), errors
    if not valid:
        assert f"LLM_CONTEXT_TOKENS='{value}'".encode() in printed  # names the value as written

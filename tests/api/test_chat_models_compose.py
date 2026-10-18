"""Under Docker Compose, Activate cannot switch to a model llama-server is not serving (#654).

Compose starts llama-server once, with LLM_MODEL_FILE, and nothing in the app can restart it with
another file. An activation would show one model while another answered, so a switch is refused with
the change to make instead; activating the served model is allowed. Under the macOS app (a native
config.json) activation is unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import harbor_clerk.config as config
from harbor_clerk.llm.models import GIB
from tests.conftest import auth_header

SERVED = "Qwen3.5-4B-Q4_K_M.gguf"  # qwen35-4b
OTHER = "Qwen3.5-9B-Q4_K_M.gguf"  # qwen35-9b


@pytest.fixture
def models_dir(monkeypatch, tmp_path: Path) -> Path:
    models = tmp_path / "models"
    models.mkdir()
    (models / SERVED).write_bytes(b"GGUF")
    (models / OTHER).write_bytes(b"GGUF")
    monkeypatch.setenv("MODELS_DIR", str(models))
    monkeypatch.delenv("LLM_MODEL_ID", raising=False)
    monkeypatch.setattr("harbor_clerk.api.routes.chat.system_ram_bytes", lambda: 64 * GIB)
    restarts: list[str] = []
    monkeypatch.setattr("harbor_clerk.llm.health.request_llm_restart", restarts.append)
    return models


@pytest.fixture
def compose(monkeypatch, models_dir: Path) -> None:
    monkeypatch.delenv("NATIVE_CONFIG_FILE", raising=False)
    monkeypatch.setenv("HC_DEPLOYMENT", "compose")
    monkeypatch.setenv("LLM_MODEL_FILE", SERVED)
    config._settings = None


@pytest.fixture
def source_run(monkeypatch, models_dir: Path) -> None:
    """A source run whose environment carries the Compose variables (a .env copied for Docker) but not the
    marker only docker-compose.yml sets."""
    monkeypatch.delenv("NATIVE_CONFIG_FILE", raising=False)
    monkeypatch.delenv("HC_DEPLOYMENT", raising=False)
    monkeypatch.setenv("LLM_MODEL_FILE", SERVED)
    monkeypatch.setenv("LLM_CONTEXT_TOKENS", "8192")
    config._settings = None


@pytest.fixture
def macos(monkeypatch, models_dir: Path, tmp_path: Path) -> Path:
    native = tmp_path / "config.json"
    native.write_text(json.dumps({"llm_model_id": "qwen35-4b"}))
    monkeypatch.setenv("NATIVE_CONFIG_FILE", str(native))
    monkeypatch.setenv("LLM_MODEL_FILE", SERVED)  # set by mistake: the macOS app ignores it
    config._settings = None
    return native


async def test_compose_refuses_a_switch_to_another_model(client, admin_user, admin_token, compose) -> None:
    resp = await client.put("/api/chat/models/qwen35-9b/activate", headers=auth_header(admin_token))
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert f"LLM_MODEL_FILE={SERVED}" in detail
    assert f"set LLM_MODEL_FILE={OTHER}" in detail
    assert config.get_settings().llm_model_id == "qwen35-4b"


async def test_compose_allows_activating_the_served_model(client, admin_user, admin_token, compose) -> None:
    resp = await client.put("/api/chat/models/qwen35-4b/activate", headers=auth_header(admin_token))
    assert resp.status_code == 200
    assert config.get_settings().llm_model_id == "qwen35-4b"


async def test_compose_refuses_a_deactivation(client, admin_user, admin_token, compose) -> None:
    resp = await client.put("/api/chat/models/deactivate", headers=auth_header(admin_token))
    assert resp.status_code == 409
    assert "LLM_MODEL_FILE= (empty)" in resp.json()["detail"]
    assert config.get_settings().llm_model_id == "qwen35-4b"


async def test_compose_lists_the_served_model_as_active(client, admin_user, admin_token, compose) -> None:
    resp = await client.get("/api/chat/models", headers=auth_header(admin_token))
    assert resp.status_code == 200
    assert [m["id"] for m in resp.json() if m["active"]] == ["qwen35-4b"]


async def test_macos_switches_models_as_before(client, admin_user, admin_token, macos: Path) -> None:
    resp = await client.put("/api/chat/models/qwen35-9b/activate", headers=auth_header(admin_token))
    assert resp.status_code == 200
    assert config.get_settings().llm_model_id == "qwen35-9b"
    assert json.loads(macos.read_text())["llm_model_id"] == "qwen35-9b"
    resp = await client.put("/api/chat/models/deactivate", headers=auth_header(admin_token))
    assert resp.status_code == 200
    assert json.loads(macos.read_text())["llm_model_id"] == ""


async def test_compose_refuses_deleting_the_served_model(
    client, admin_user, admin_token, compose, models_dir: Path
) -> None:
    # llama-server has it mapped: unlinking frees nothing while the app would report no model.
    resp = await client.delete("/api/chat/models/qwen35-4b", headers=auth_header(admin_token))
    assert resp.status_code == 409
    assert f"serving {SERVED}" in resp.json()["detail"]
    assert (models_dir / SERVED).is_file()
    assert config.get_settings().llm_model_id == "qwen35-4b"


async def test_compose_still_deletes_other_models(client, admin_user, admin_token, compose, models_dir: Path) -> None:
    resp = await client.delete("/api/chat/models/qwen35-9b", headers=auth_header(admin_token))
    assert resp.status_code == 204
    assert not (models_dir / OTHER).exists()


async def test_compose_refuses_deleting_a_served_uncurated_file(
    client, admin_user, admin_token, compose, models_dir: Path, monkeypatch
) -> None:
    # The orphan route deletes files the registry does not know, which LLM_MODEL_FILE may name.
    (models_dir / "my-own.gguf").write_bytes(b"GGUF")
    monkeypatch.setenv("LLM_MODEL_FILE", "my-own.gguf")
    config._settings = None
    resp = await client.delete("/api/chat/models/orphaned/my-own.gguf", headers=auth_header(admin_token))
    assert resp.status_code == 409
    assert (models_dir / "my-own.gguf").is_file()


async def test_a_source_run_with_the_compose_variables_is_not_compose(
    client, admin_user, admin_token, source_run, models_dir: Path
) -> None:
    resp = await client.put("/api/chat/models/qwen35-9b/activate", headers=auth_header(admin_token))
    assert resp.status_code == 200
    assert config.get_settings().llm_model_id == "qwen35-9b"
    resp = await client.put("/api/chat/models/deactivate", headers=auth_header(admin_token))
    assert resp.status_code == 200
    resp = await client.delete("/api/chat/models/qwen35-4b", headers=auth_header(admin_token))
    assert resp.status_code == 204


async def test_macos_deletes_the_active_model_as_before(
    client, admin_user, admin_token, macos: Path, models_dir: Path
) -> None:
    resp = await client.delete("/api/chat/models/qwen35-4b", headers=auth_header(admin_token))
    assert resp.status_code == 204
    assert not (models_dir / SERVED).exists()
    assert json.loads(macos.read_text())["llm_model_id"] == ""

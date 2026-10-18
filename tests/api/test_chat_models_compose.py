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
from harbor_clerk.llm.models import GIB, memory_bytes, min_ram_gb
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


@pytest.fixture
def compose_no_model(monkeypatch, models_dir: Path) -> None:
    """Compose with LLM_MODEL_FILE set empty, the documented "no local model", and a stale LLM_MODEL_ID."""
    monkeypatch.delenv("NATIVE_CONFIG_FILE", raising=False)
    monkeypatch.setenv("HC_DEPLOYMENT", "compose")
    monkeypatch.setenv("LLM_MODEL_FILE", "")
    monkeypatch.setenv("LLM_MODEL_ID", "qwen35-9b")
    config._settings = None


async def test_compose_without_a_model_file_refuses_activation(
    client, admin_user, admin_token, compose_no_model
) -> None:
    # Nothing listens on llama-server's port: an activation would show a model no server serves.
    resp = await client.put("/api/chat/models/qwen35-9b/activate", headers=auth_header(admin_token))
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "no local model is configured (LLM_MODEL_FILE is empty)" in detail
    assert f"set LLM_MODEL_FILE={OTHER}" in detail
    assert config.get_settings().llm_model_id == ""
    listed = await client.get("/api/chat/models", headers=auth_header(admin_token))
    assert [m["id"] for m in listed.json() if m["active"]] == []


async def test_compose_without_a_model_file_allows_deactivation(
    client, admin_user, admin_token, compose_no_model
) -> None:
    # Already the state: nothing is served, so deactivating changes nothing and is not refused.
    resp = await client.put("/api/chat/models/deactivate", headers=auth_header(admin_token))
    assert resp.status_code == 200
    assert config.get_settings().llm_model_id == ""


async def test_research_sees_a_download_that_finished_after_startup(
    client, admin_user, admin_token, compose, models_dir: Path, monkeypatch
) -> None:
    # The route reads the active model fresh, as chat does: Research started right after the download
    # must not answer "No LLM model configured".
    (models_dir / SERVED).unlink()
    config._settings = None
    assert config.get_settings().llm_model_id == ""
    (models_dir / SERVED).write_bytes(b"GGUF")  # the download lands

    async def no_stream(*args, **kwargs):
        yield b'data: {"type": "done"}\n\n'

    monkeypatch.setattr("harbor_clerk.api.routes.research.research_stream", no_stream)
    resp = await client.post(
        "/api/research",
        json={"question": "What are the termination clauses?", "time_limit_minutes": 15, "depth": "light"},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 200, resp.text
    assert config.get_settings().llm_model_id == "qwen35-4b"


async def test_compose_deactivate_is_a_no_op_before_the_model_is_downloaded(
    client, admin_user, admin_token, compose, models_dir: Path
) -> None:
    # LLM_MODEL_FILE is set but its file is not in the volume yet: nothing is active, so nothing to refuse.
    (models_dir / SERVED).unlink()
    config._settings = None
    assert config.get_settings().llm_model_id == ""
    resp = await client.put("/api/chat/models/deactivate", headers=auth_header(admin_token))
    assert resp.status_code == 200
    assert config.get_settings().llm_model_id == ""


async def test_compose_activating_the_served_model_skips_the_mac_memory_check(
    client, admin_user, admin_token, compose, monkeypatch
) -> None:
    # A small Docker VM: the Mac arithmetic says the model does not fit, but the server is already running it.
    monkeypatch.setattr("harbor_clerk.api.routes.chat.system_ram_bytes", lambda: 2 * GIB)
    resp = await client.put("/api/chat/models/qwen35-4b/activate", headers=auth_header(admin_token))
    assert resp.status_code == 200, resp.text
    assert config.get_settings().llm_model_id == "qwen35-4b"


async def test_a_source_run_still_gets_the_memory_check(
    client, admin_user, admin_token, source_run, monkeypatch
) -> None:
    monkeypatch.setattr("harbor_clerk.api.routes.chat.system_ram_bytes", lambda: 2 * GIB)
    resp = await client.put("/api/chat/models/qwen35-4b/activate", headers=auth_header(admin_token))
    assert resp.status_code == 409
    assert "GB of memory" in resp.json()["detail"]


async def test_compose_deactivate_reads_the_active_model_fresh(
    client, admin_user, admin_token, compose, models_dir: Path
) -> None:
    # The settings were built while the file was there; it has gone since (removed in the volume by hand).
    assert config.get_settings().llm_model_id == "qwen35-4b"
    (models_dir / SERVED).unlink()
    resp = await client.put("/api/chat/models/deactivate", headers=auth_header(admin_token))
    assert resp.status_code == 200, "deactivate judged a model active that the volume no longer holds"


@pytest.mark.parametrize("ram_gib", [2, 64])
async def test_compose_models_page_shows_the_servers_context_and_an_honest_fit(
    client, admin_user, admin_token, compose, monkeypatch, ram_gib
) -> None:
    # Under Compose the server runs every model at LLM_CONTEXT_TOKENS, so that is the context shown. Whether it
    # fits is the same arithmetic against the memory this container sees, which is what llama-server can use:
    # nothing fits a 2 GB VM, everything fits 64 GB. The page words it for Docker.
    from harbor_clerk.llm.models import MODELS

    monkeypatch.setattr("harbor_clerk.api.routes.chat.system_ram_bytes", lambda: ram_gib * GIB)
    monkeypatch.setenv("LLM_CONTEXT_TOKENS", "8192")
    config._settings = None
    resp = await client.get("/api/chat/models", headers=auth_header(admin_token))
    assert resp.status_code == 200
    for m in resp.json():
        assert m["fits_here"] is (ram_gib == 64), m["id"]
        assert m["compose_model_file"] == SERVED, m["id"]
        assert m["max_context_here"] == min(MODELS[m["id"]].context_window, 8192), m["id"]
        # The memory figures are both at that context, never at a YaRN window the server does not use.
        assert m["memory_bytes"] == memory_bytes(MODELS[m["id"]], m["max_context_here"]), m["id"]
        assert m["min_ram_gb"] == min_ram_gb(MODELS[m["id"]], m["max_context_here"]), m["id"]


async def test_a_source_run_models_page_keeps_the_mac_arithmetic(
    client, admin_user, admin_token, source_run, monkeypatch
) -> None:
    monkeypatch.setattr("harbor_clerk.api.routes.chat.system_ram_bytes", lambda: 2 * GIB)
    resp = await client.get("/api/chat/models", headers=auth_header(admin_token))
    by_id = {m["id"]: m for m in resp.json()}
    assert by_id["qwen35-4b"]["fits_here"] is False and by_id["qwen35-4b"]["max_context_here"] == 0
    assert by_id["qwen35-4b"]["compose_model_file"] is None


async def test_compose_with_no_model_yet_says_to_download_it(
    client, admin_user, admin_token, compose, models_dir: Path
) -> None:
    # A fresh install: LLM_MODEL_FILE is set but not downloaded. "Select and activate a model" is the wrong
    # advice under Compose (activating is refused); the action is to download that file.
    (models_dir / SERVED).unlink()
    config._settings = None
    status = (await client.get("/api/chat/models/status", headers=auth_header(admin_token))).json()
    assert status["state"] == "deactivated"
    assert status["setup_hint"] == f"No local model is loaded yet. Download {SERVED} in Settings → Models."
    assert status["compose_model_file"] == SERVED
    resp = await client.post(
        "/api/research",
        json={"question": "What are the termination clauses?", "time_limit_minutes": 15, "depth": "light"},
        headers=auth_header(admin_token),
    )
    assert resp.status_code == 503
    assert resp.json()["detail"] == status["setup_hint"]


async def test_outside_compose_no_model_keeps_its_advice(
    client, admin_user, admin_token, source_run, models_dir: Path
) -> None:
    (models_dir / SERVED).unlink()
    status = (await client.get("/api/chat/models/status", headers=auth_header(admin_token))).json()
    assert status["setup_hint"] is None
    assert status["compose_model_file"] is None


async def test_compose_activates_the_served_model_written_with_dot_slash(
    client, admin_user, admin_token, compose, monkeypatch
) -> None:
    monkeypatch.setenv("LLM_MODEL_FILE", f"./{SERVED}")
    config._settings = None
    resp = await client.put("/api/chat/models/qwen35-4b/activate", headers=auth_header(admin_token))
    assert resp.status_code == 200, resp.text

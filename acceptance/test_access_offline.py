"""The access helpers, offline: the tier contract's shape, the CLI environment
mapping, the CLI-access toggle's restore, and the empty-folder cleanup."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from acceptance import access


def test_tier_contract_is_nested_and_excludes_admin_tools() -> None:
    assert access.SEARCH_TOOLS < access.READ_TOOLS < access.FULL_TOOLS
    assert not (access.FULL_TOOLS & access.ADMIN_ONLY_TOOLS)
    assert len(access.SEARCH_TOOLS) == 6 and len(access.READ_TOOLS) == 11 and len(access.FULL_TOOLS) == 17


def test_cli_env_maps_the_suite_settings_onto_the_cli_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARBOR_CLERK_API_KEY", "stale-from-the-shell")
    monkeypatch.setenv("HARBOR_CLERK_URL", "https://stale")
    env = access.cli_env("http://localhost:8100", "hc_new", insecure=True)
    assert env["HARBOR_CLERK_URL"] == "http://localhost:8100"
    assert env["HARBOR_CLERK_API_KEY"] == "hc_new"
    assert env["HARBOR_CLERK_INSECURE_SKIP_VERIFY"] == "1"
    assert "HARBOR_CLERK_INSECURE_SKIP_VERIFY" not in access.cli_env("http://x", "hc_k")
    assert "PATH" in env, "the CLI still needs the rest of the environment"


def test_cli_env_drops_a_foreign_virtualenv(monkeypatch: pytest.MonkeyPatch) -> None:
    """With VIRTUAL_ENV pointing elsewhere, `uv run` prints a warning on stderr,
    where the CLI's structured error also goes."""
    monkeypatch.setenv("VIRTUAL_ENV", "/somewhere/else")
    assert "VIRTUAL_ENV" not in access.cli_env("http://x", "hc_k")


def test_cli_error_is_parsed_past_tooling_noise_on_stderr() -> None:
    result = access.CliResult(
        code=access.EXIT_AUTH,
        stdout="",
        stderr='warning: `VIRTUAL_ENV=/x` does not match the project environment path\n{"error_kind": "auth", "message": "401"}\n',
    )
    assert result.error["error_kind"] == "auth"
    with pytest.raises(ValueError, match="no JSON error"):
        _ = access.CliResult(code=1, stdout="", stderr="harbor-clerk: Missing API key").error


def test_cli_access_toggle_restores_the_original_value(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text('{\n  "api_port": 8100,\n  "enable_cli_access": true\n}\n')
    with access.cli_access_toggled(cfg, False):
        assert json.loads(cfg.read_text())["enable_cli_access"] is False
        assert json.loads(cfg.read_text())["api_port"] == 8100, "other settings must survive the flip"
    assert json.loads(cfg.read_text()) == {"api_port": 8100, "enable_cli_access": True}


def test_cli_access_toggle_restores_even_when_the_block_raises(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text('{"enable_cli_access": true}')
    with pytest.raises(RuntimeError, match="boom"), access.cli_access_toggled(cfg, False):
        assert json.loads(cfg.read_text())["enable_cli_access"] is False
        raise RuntimeError("boom")
    assert json.loads(cfg.read_text()) == {"enable_cli_access": True}, "a failing check must not leave the gate flipped"


def test_cli_access_toggle_keeps_what_other_writers_changed_meanwhile(tmp_path: Path) -> None:
    """The API and the Swift app both write this file; the restore must put
    back only the one key it flipped, not the whole file as it was."""
    cfg = tmp_path / "config.json"
    cfg.write_text('{"enable_cli_access": true, "llm_model_id": "a"}')
    with access.cli_access_toggled(cfg, False):
        data = json.loads(cfg.read_text())
        data["llm_model_id"] = "b"  # the app changed a setting during the block
        cfg.write_text(json.dumps(data))
    assert json.loads(cfg.read_text()) == {"enable_cli_access": True, "llm_model_id": "b"}


def test_cli_access_toggle_removes_the_key_it_added(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text('{"api_port": 8100}')
    with access.cli_access_toggled(cfg, True):
        assert json.loads(cfg.read_text())["enable_cli_access"] is True
    assert json.loads(cfg.read_text()) == {"api_port": 8100}
    assert not list(tmp_path.glob("*.acceptance-tmp")), "the atomic write must not leave its temp file"


class FakeAdmin:
    def __init__(self, *, create_error: Exception | None = None):
        self.create_error = create_error
        self.created: list[str] = []
        self.deleted: list[str] = []

    def folder_create(self, path: str, *, recursive: bool = True) -> dict[str, Any]:
        if self.create_error:
            raise self.create_error
        self.created.append(path)
        return {"folder_id": "empty-1", "path": path}

    def folder_delete(self, folder_id: str) -> None:
        self.deleted.append(folder_id)


def test_empty_folder_session_registers_then_removes(tmp_path: Path) -> None:
    admin = FakeAdmin()
    folder = tmp_path / "empty"
    with access.empty_folder_session(admin, folder, "/instance/empty") as f:
        assert f["folder_id"] == "empty-1" and folder.is_dir() and admin.created == ["/instance/empty"]
    assert admin.deleted == ["empty-1"] and not folder.exists()


def test_empty_folder_session_cleans_up_when_the_block_raises(tmp_path: Path) -> None:
    admin = FakeAdmin()
    folder = tmp_path / "empty"
    with pytest.raises(RuntimeError), access.empty_folder_session(admin, folder, "/instance/empty"):
        raise RuntimeError("check failed")
    assert admin.deleted == ["empty-1"] and not folder.exists()


def test_empty_folder_session_removes_the_directory_when_registration_fails(tmp_path: Path) -> None:
    admin = FakeAdmin(create_error=RuntimeError("409"))
    folder = tmp_path / "empty"
    with pytest.raises(RuntimeError, match="409"), access.empty_folder_session(admin, folder, "/instance/empty"):
        pass
    assert admin.deleted == [] and not folder.exists()

"""The access helpers, offline: the tier contract's shape, the CLI environment
mapping, the CLI-access toggle's restore, and the empty-folder cleanup."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import httpx
import pytest

from acceptance import access
from acceptance.config import AcceptanceConfig
from acceptance.conftest import CliAccess


def test_tier_contract_is_nested_and_excludes_admin_tools() -> None:
    assert access.SEARCH_TOOLS < access.READ_TOOLS < access.FULL_TOOLS
    assert not (access.FULL_TOOLS & access.ADMIN_ONLY_TOOLS)
    assert len(access.SEARCH_TOOLS) == 6 and len(access.READ_TOOLS) == 11 and len(access.FULL_TOOLS) == 17


def test_tier_contract_matches_the_source() -> None:
    """The constants are spelled out so drift is a failing check; this makes
    the drift visible in CI rather than only on a live run."""
    from harbor_clerk.api import scope

    assert access.SEARCH_TOOLS == scope.SEARCH_TIER_TOOLS
    assert access.READ_TOOLS == scope.READ_TIER_TOOLS
    assert access.FULL_TOOLS == scope.FULL_TIER_TOOLS
    assert access.ADMIN_ONLY_TOOLS == scope.ADMIN_ONLY_TOOLS


def test_cli_env_maps_the_suite_settings_onto_the_cli_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARBOR_CLERK_API_KEY", "stale-from-the-shell")
    monkeypatch.setenv("HARBOR_CLERK_URL", "https://stale")
    env = access.cli_env("http://localhost:8100", "hc_new", insecure=True)
    assert env["HARBOR_CLERK_URL"] == "http://localhost:8100"
    assert env["HARBOR_CLERK_API_KEY"] == "hc_new"
    assert env["HARBOR_CLERK_INSECURE_SKIP_VERIFY"] == "1"
    assert "HARBOR_CLERK_INSECURE_SKIP_VERIFY" not in access.cli_env("http://x", "hc_k")
    assert "PATH" in env, "the CLI still needs the rest of the environment"


def test_cli_env_never_forwards_the_suite_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HC_PASSWORD", "admin-secret")
    monkeypatch.setenv("HC_API_BASE", "http://localhost:8100")
    env = access.cli_env("http://x", "hc_k")
    assert not any(k.startswith("HC_") for k in env), "the admin password must not reach a CLI subprocess"


def test_cli_command_puts_json_after_the_subcommand() -> None:
    cmd = access.cli_command("search", "q", "-k", "1")
    assert cmd[-1] == "--json" and cmd.index("harbor-clerk") < cmd.index("search") < cmd.index("--json")
    assert "--frozen" in cmd and "--no-sync" in cmd, "the CLI runner must never re-lock or install"


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
    trailing = access.CliResult(code=2, stdout="", stderr='{"error_kind": "connection"}\nwarning: something after\n')
    assert trailing.error["error_kind"] == "connection", "noise after the JSON must not break parsing either"
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


def test_cli_access_toggle_restores_an_absent_key_as_an_explicit_false(tmp_path: Path) -> None:
    """The API applies only keys present in the file (config._apply_to), so
    removing the key would leave the running API on the value this block set.
    False is the setting's default and the Swift toggle's default reading."""
    cfg = tmp_path / "config.json"
    cfg.write_text('{"api_port": 8100}')
    with access.cli_access_toggled(cfg, True):
        assert json.loads(cfg.read_text())["enable_cli_access"] is True
    assert json.loads(cfg.read_text()) == {"api_port": 8100, "enable_cli_access": False}
    assert not list(tmp_path.glob("*.acceptance-tmp")), "the atomic write must not leave its temp file"


def test_cli_access_toggle_preserves_the_file_mode(tmp_path: Path) -> None:
    """config.json holds secret_key and the product writes it 0600."""
    cfg = tmp_path / "config.json"
    cfg.write_text('{"enable_cli_access": false, "secret_key": "s"}')
    os.chmod(cfg, 0o600)
    with access.cli_access_toggled(cfg, True):
        assert stat.S_IMODE(cfg.stat().st_mode) == 0o600
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600


def test_cli_access_toggle_restores_even_if_the_file_is_unreadable_at_the_end(tmp_path: Path) -> None:
    """A raise in the finally would skip the restore write and mask the
    check's own failure; the restore falls back to what the file was."""
    cfg = tmp_path / "config.json"
    cfg.write_text('{"enable_cli_access": false, "api_port": 8100}')
    with access.cli_access_toggled(cfg, True):
        cfg.write_text("{ not json")  # a torn write by another process, say
    assert json.loads(cfg.read_text()) == {"enable_cli_access": False, "api_port": 8100}


def test_cli_access_toggle_keeps_non_ascii_values_readable(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"enable_cli_access": False, "watch_label": "Dossiers – clés"}, ensure_ascii=False))
    with access.cli_access_toggled(cfg, True):
        pass
    assert "Dossiers – clés" in cfg.read_text(encoding="utf-8")


def test_cli_access_toggle_tolerates_a_non_object_file(tmp_path: Path) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text("[]")
    with access.cli_access_toggled(cfg, True):
        assert json.loads(cfg.read_text()) == {"enable_cli_access": True}
    assert json.loads(cfg.read_text()) == {"enable_cli_access": False}


def test_mcp_probe_builds_both_auth_surfaces() -> None:
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers.get("Authorization")))
        body = json.loads(request.content)
        assert body["method"] == "initialize"
        return httpx.Response(200, json={})

    t = httpx.MockTransport(handler)
    assert access.mcp_probe("http://test/", "hc_k", transport=t) == 200
    assert access.mcp_probe("http://test", "hc_k", url_token=True, transport=t) == 200
    assert seen == [("/mcp/", "Bearer hc_k"), ("/t/hc_k", None)]


# ── CliAccess with a fake CLI ───────────────────────────────────────────────


class _Result:
    def __init__(self, code: int):
        self.code = code


class _FakeCli:
    """A CLI whose exit code follows the file: 0 when enable_cli_access is
    true, 3 otherwise. `stuck_on` simulates an API whose in-memory gate stopped
    following the file."""

    def __init__(self, config_json: Path | None, *, stuck_on: int | None = None):
        self.config_json = config_json
        self.stuck_on = stuck_on
        self.calls = 0

    def __call__(self, api_base: str, raw_key: str, *args: str, insecure: bool = False) -> _Result:
        self.calls += 1
        if self.stuck_on is not None:
            return _Result(self.stuck_on)
        if self.config_json is None:
            return _Result(access.EXIT_CLI_DISABLED)
        data = json.loads(self.config_json.read_text() or "{}")
        return _Result(access.EXIT_OK if data.get("enable_cli_access") else access.EXIT_CLI_DISABLED)


def _cfg(tmp_path: Path, config_json: Path | None) -> AcceptanceConfig:
    return AcceptanceConfig(
        api_base="http://localhost:8100",
        username="a@b.c",
        password="x",
        folder_root=tmp_path,
        folder_root_in_instance=None,
        insecure=False,
        disposable=False,
        wipe=False,
        keep=False,
        config_json=config_json,
        allow_model_swap=False,
        run_id="offline",
        ingest_timeout_s=1,
        ask_timeout_s=1,
    )


def test_cli_access_enabled_is_read_from_the_exit_code(tmp_path: Path) -> None:
    on = CliAccess(_cfg(tmp_path, None), "hc_k", runner=lambda *a, **k: _Result(access.EXIT_OK))
    off = CliAccess(_cfg(tmp_path, None), "hc_k", runner=lambda *a, **k: _Result(access.EXIT_CLI_DISABLED))
    odd = CliAccess(_cfg(tmp_path, None), "hc_k", runner=lambda *a, **k: _Result(access.EXIT_CONNECTION))
    assert on.enabled is True and off.enabled is False
    with pytest.raises(RuntimeError, match="exited 2"):
        _ = odd.enabled


def test_cli_access_toggle_skips_without_the_config_path(tmp_path: Path) -> None:
    ca = CliAccess(_cfg(tmp_path, None), "hc_k", runner=_FakeCli(None))
    with pytest.raises(pytest.skip.Exception, match="HC_ACCEPTANCE_CONFIG_JSON"):
        ca.toggled(True)
    with pytest.raises(pytest.skip.Exception, match="disabled"), ca.ensured():
        pass


def test_cli_access_ensured_flips_on_and_restores_off(tmp_path: Path) -> None:
    cfg_json = tmp_path / "config.json"
    cfg_json.write_text("{}")  # gate off by default, key absent: the case that bit
    fake = _FakeCli(cfg_json)
    ca = CliAccess(_cfg(tmp_path, cfg_json), "hc_k", runner=fake)
    with ca.ensured():
        assert json.loads(cfg_json.read_text())["enable_cli_access"] is True
    assert json.loads(cfg_json.read_text())["enable_cli_access"] is False
    assert fake.calls >= 2, "a probe before and a healing probe after"


def test_cli_access_ensured_heals_even_when_the_block_raises(tmp_path: Path) -> None:
    cfg_json = tmp_path / "config.json"
    cfg_json.write_text('{"enable_cli_access": false}')
    fake = _FakeCli(cfg_json)
    ca = CliAccess(_cfg(tmp_path, cfg_json), "hc_k", runner=fake)
    calls_before = 0
    with pytest.raises(RuntimeError, match="check failed"), ca.ensured():
        calls_before = fake.calls
        raise RuntimeError("check failed")
    assert fake.calls == calls_before + 1, "the healing probe must run when the block fails"
    assert json.loads(cfg_json.read_text())["enable_cli_access"] is False


def test_cli_access_verify_settled_waits_and_checks_only_after_a_flip(tmp_path: Path) -> None:
    cfg_json = tmp_path / "config.json"
    cfg_json.write_text('{"enable_cli_access": false}')
    slept: list[float] = []
    fake = _FakeCli(cfg_json)
    ca = CliAccess(_cfg(tmp_path, cfg_json), "hc_k", runner=fake, sleeper=slept.append)
    ca.verify_settled(False)
    assert slept == [], "no flip, nothing to wait for"
    with ca.ensured():
        pass
    ca.verify_settled(False)
    assert slept == [3.5], "after a flip, wait past the Swift app's 3 s poll before re-checking"


def test_cli_access_verify_settled_fails_when_a_writer_restored_the_flipped_value(tmp_path: Path) -> None:
    cfg_json = tmp_path / "config.json"
    cfg_json.write_text('{"enable_cli_access": false}')
    fake = _FakeCli(cfg_json)
    ca = CliAccess(_cfg(tmp_path, cfg_json), "hc_k", runner=fake, sleeper=lambda s: None)
    with ca.ensured():
        pass
    cfg_json.write_text('{"enable_cli_access": true}')  # the app saved its cached, flipped copy
    with pytest.raises(pytest.fail.Exception, match="a writer restored a flipped value"):
        ca.verify_settled(False)


def test_cli_access_ensured_fails_loudly_when_the_gate_does_not_follow_the_file(tmp_path: Path) -> None:
    """A restore that the API did not pick up is the state this suite must
    never leave silently."""
    cfg_json = tmp_path / "config.json"
    cfg_json.write_text('{"enable_cli_access": false}')
    fake = _FakeCli(cfg_json)
    ca = CliAccess(_cfg(tmp_path, cfg_json), "hc_k", runner=fake)
    with pytest.raises(pytest.fail.Exception, match="did not return to disabled"), ca.ensured():
        fake.stuck_on = access.EXIT_OK  # from here on the API keeps saying enabled


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


class FakePopulatedAdmin(FakeAdmin):
    """Adds what `populated_folder_session` needs: an ingest wait, the document
    mapping, and a folder delete that can 404 (the folder was already removed)."""

    def __init__(self, *, gone_on_delete: bool = False):
        super().__init__()
        self.gone_on_delete = gone_on_delete

    def wait_for_folder_ingest(self, folder_id: str, *, expected_files: int, timeout_s: float, poll_s: float = 3.0):
        return {"completed_files": expected_files}

    def documents_under(self, folder_path: str, expected=None):
        return {access.SECOND_FOLDER_SOURCE: {"doc_id": "d-second"}}

    def folder_delete(self, folder_id: str) -> None:
        if self.gone_on_delete:
            raise httpx.HTTPStatusError(
                "404", request=httpx.Request("DELETE", "http://x"), response=httpx.Response(404)
            )
        super().folder_delete(folder_id)


def test_populated_folder_session_renders_registers_and_cleans_up(tmp_path: Path) -> None:
    admin = FakePopulatedAdmin()
    folder = tmp_path / "second"
    with access.populated_folder_session(admin, folder, "/instance/second", source_text="x", timeout_s=1) as f:
        assert f.doc_id == "d-second" and f.phrase == access.SECOND_FOLDER_PHRASE
        assert (folder / access.SECOND_FOLDER_SOURCE).read_text() == "x"
    assert admin.deleted == ["empty-1"] and not folder.exists()


def test_populated_folder_session_tolerates_a_folder_a_check_already_deleted(tmp_path: Path) -> None:
    """H2 deletes the folder itself; the teardown's 404 must not mask H2's result."""
    admin = FakePopulatedAdmin(gone_on_delete=True)
    folder = tmp_path / "second"
    with access.populated_folder_session(admin, folder, "/instance/second", source_text="x", timeout_s=1):
        pass
    assert not folder.exists(), "the files must go even when the folder was already gone"

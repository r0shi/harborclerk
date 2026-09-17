"""The suite's safety switches, checked offline."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from acceptance import config
from acceptance.config import AcceptanceConfig


def _env(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    for name in list(os.environ):
        if name.startswith("HC_"):
            monkeypatch.delenv(name, raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_skips_without_an_instance(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch)
    with pytest.raises(pytest.skip.Exception, match="HC_API_BASE"):
        config.load_config()


def test_skips_without_admin_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, HC_API_BASE="http://localhost:8100")
    with pytest.raises(pytest.skip.Exception, match="HC_USERNAME"):
        config.load_config()


def test_wipe_requires_the_disposable_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run that would empty the instance fails loudly, it does not skip."""
    _env(monkeypatch, HC_API_BASE="http://localhost:8100", HC_USERNAME="a@b.c", HC_PASSWORD="x", HC_ACCEPTANCE_WIPE="1")
    with pytest.raises(pytest.fail.Exception, match="DISPOSABLE"):
        config.load_config()


def test_wipe_is_refused_against_a_non_loopback_instance(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The disposable instance is local by definition; a remote wipe is a typo."""
    _env(
        monkeypatch,
        HC_API_BASE="https://mini.local:8443",
        HC_USERNAME="a@b.c",
        HC_PASSWORD="x",
        HC_ACCEPTANCE_FOLDER_ROOT=str(tmp_path),
        HC_ACCEPTANCE_WIPE="1",
        HC_ACCEPTANCE_DISPOSABLE="1",
    )
    with pytest.raises(pytest.fail.Exception, match="loopback"):
        config.load_config()


def test_wipe_is_allowed_on_loopback_with_both_flags(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _env(
        monkeypatch,
        HC_API_BASE="http://127.0.0.1:8100",
        HC_USERNAME="a@b.c",
        HC_PASSWORD="x",
        HC_ACCEPTANCE_FOLDER_ROOT=str(tmp_path),
        HC_ACCEPTANCE_WIPE="1",
        HC_ACCEPTANCE_DISPOSABLE="1",
        HC_ACCEPTANCE_KEEP="1",
    )
    cfg = config.load_config()
    assert cfg.wipe and cfg.disposable and cfg.keep


def test_folder_root_must_exist(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _env(
        monkeypatch,
        HC_API_BASE="http://localhost:8100",
        HC_USERNAME="a@b.c",
        HC_PASSWORD="x",
        HC_ACCEPTANCE_FOLDER_ROOT=str(tmp_path / "missing"),
    )
    with pytest.raises(pytest.skip.Exception, match="not a directory"):
        config.load_config()


def test_compose_target_is_skipped_with_the_reason(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _env(
        monkeypatch,
        HC_API_BASE="https://localhost",
        HC_USERNAME="a@b.c",
        HC_PASSWORD="x",
        HC_ACCEPTANCE_FOLDER_ROOT=str(tmp_path),
        HC_ACCEPTANCE_FOLDER_ROOT_IN_INSTANCE="/data/watch",
    )
    with pytest.raises(pytest.skip.Exception, match="Compose"):
        config.load_config()


def test_config_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _env(
        monkeypatch,
        HC_API_BASE="http://localhost:8100/",
        HC_USERNAME="a@b.c",
        HC_PASSWORD="x",
        HC_ACCEPTANCE_FOLDER_ROOT=str(tmp_path),
        HC_ACCEPTANCE_RUN_ID="abc123",
    )
    cfg = config.load_config()
    assert cfg.api_base == "http://localhost:8100"  # trailing slash stripped
    assert cfg.folder_path == tmp_path.resolve() / "hc-acceptance-abc123"
    assert cfg.folder_path_in_instance == str(cfg.folder_path)
    assert not cfg.wipe and not cfg.disposable and not cfg.insecure and not cfg.keep
    assert cfg.ingest_timeout_s == 900 and cfg.ask_timeout_s == 300


def test_folder_path_in_instance_maps_to_the_container_side(tmp_path: Path) -> None:
    cfg = AcceptanceConfig(
        api_base="https://localhost",
        username="a@b.c",
        password="x",
        folder_root=tmp_path,
        folder_root_in_instance="/data/watch",
        insecure=True,
        disposable=False,
        wipe=False,
        keep=False,
        run_id="abc123",
        ingest_timeout_s=1,
        ask_timeout_s=1,
    )
    assert cfg.folder_path == tmp_path / "hc-acceptance-abc123"
    assert cfg.folder_path_in_instance == "/data/watch/hc-acceptance-abc123"

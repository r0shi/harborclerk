"""The suite's safety switches, checked offline."""

from __future__ import annotations

import pytest

from tests.acceptance import config


def _env(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    for name in (
        "HC_API_BASE",
        "HC_USERNAME",
        "HC_PASSWORD",
        "HC_ACCEPTANCE_FOLDER_ROOT",
        "HC_INSECURE",
        "HC_ACCEPTANCE_DISPOSABLE",
        "HC_ACCEPTANCE_WIPE",
        "HC_ACCEPTANCE_RUN_ID",
    ):
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


def test_folder_root_must_exist(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _env(
        monkeypatch,
        HC_API_BASE="http://localhost:8100",
        HC_USERNAME="a@b.c",
        HC_PASSWORD="x",
        HC_ACCEPTANCE_FOLDER_ROOT=str(tmp_path / "missing"),
    )
    with pytest.raises(pytest.skip.Exception, match="not a directory"):
        config.load_config()


def test_config_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
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
    assert not cfg.wipe and not cfg.disposable and not cfg.insecure
    assert cfg.ingest_timeout_s == 900

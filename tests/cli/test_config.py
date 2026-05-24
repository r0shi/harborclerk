import pytest

from harbor_clerk.cli.config import resolve_config


def test_url_defaults_to_localhost(monkeypatch):
    monkeypatch.delenv("HARBOR_CLERK_URL", raising=False)
    cfg = resolve_config(url=None, api_key="hc_test", insecure=False)
    assert cfg.url == "https://localhost"


def test_url_from_env(monkeypatch):
    monkeypatch.setenv("HARBOR_CLERK_URL", "https://example.test")
    cfg = resolve_config(url=None, api_key="hc_test", insecure=False)
    assert cfg.url == "https://example.test"


def test_flag_overrides_env(monkeypatch):
    monkeypatch.setenv("HARBOR_CLERK_URL", "https://env.test")
    cfg = resolve_config(url="https://flag.test", api_key="hc_test", insecure=False)
    assert cfg.url == "https://flag.test"


def test_missing_api_key_raises(monkeypatch):
    monkeypatch.delenv("HARBOR_CLERK_API_KEY", raising=False)
    with pytest.raises(ValueError) as exc:
        resolve_config(url=None, api_key=None, insecure=False)
    assert "HARBOR_CLERK_API_KEY" in str(exc.value)


def test_insecure_from_env(monkeypatch):
    monkeypatch.setenv("HARBOR_CLERK_INSECURE_SKIP_VERIFY", "true")
    monkeypatch.setenv("HARBOR_CLERK_API_KEY", "hc_test")
    cfg = resolve_config(url=None, api_key=None, insecure=False)
    assert cfg.insecure is True


def test_insecure_flag(monkeypatch):
    monkeypatch.delenv("HARBOR_CLERK_INSECURE_SKIP_VERIFY", raising=False)
    monkeypatch.setenv("HARBOR_CLERK_API_KEY", "hc_test")
    cfg = resolve_config(url=None, api_key=None, insecure=True)
    assert cfg.insecure is True

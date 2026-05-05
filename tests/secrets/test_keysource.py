"""Master-key resolution from HARBOR_CLERK_MASTER_KEY env var."""

import base64

import pytest

from harbor_clerk.secrets.keysource import (
    MissingMasterKey,
    get_master_key,
)


def test_env_var_returns_decoded_key(monkeypatch):
    raw = b"\x42" * 32
    monkeypatch.setenv("HARBOR_CLERK_MASTER_KEY", base64.b64encode(raw).decode())
    assert get_master_key() == raw


def test_missing_env_var_raises(monkeypatch):
    monkeypatch.delenv("HARBOR_CLERK_MASTER_KEY", raising=False)
    with pytest.raises(MissingMasterKey, match="HARBOR_CLERK_MASTER_KEY"):
        get_master_key()


def test_empty_env_var_raises(monkeypatch):
    monkeypatch.setenv("HARBOR_CLERK_MASTER_KEY", "")
    with pytest.raises(MissingMasterKey):
        get_master_key()


def test_invalid_base64_raises(monkeypatch):
    monkeypatch.setenv("HARBOR_CLERK_MASTER_KEY", "not-valid-base64!!!")
    with pytest.raises(ValueError, match="not valid base64"):
        get_master_key()


def test_wrong_length_raises(monkeypatch):
    short = base64.b64encode(b"too short").decode()
    monkeypatch.setenv("HARBOR_CLERK_MASTER_KEY", short)
    with pytest.raises(ValueError, match="must decode to exactly 32 bytes"):
        get_master_key()

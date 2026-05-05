"""Process-wide cipher accessor."""

import base64
import os

import pytest


@pytest.fixture(autouse=True)
def reset_cipher_singleton():
    """Each test gets a fresh accessor — clear the module cache."""
    from harbor_clerk.secrets import _accessor

    _accessor.reset()
    yield
    _accessor.reset()


def test_get_cipher_returns_singleton(monkeypatch):
    from harbor_clerk.secrets import get_cipher

    raw = os.urandom(32)
    monkeypatch.setenv("HARBOR_CLERK_MASTER_KEY", base64.b64encode(raw).decode())
    c1 = get_cipher()
    c2 = get_cipher()
    assert c1 is c2  # same instance
    assert c1.fingerprint == c2.fingerprint


def test_get_cipher_raises_when_key_missing(monkeypatch):
    from harbor_clerk.secrets import MissingMasterKey, get_cipher

    monkeypatch.delenv("HARBOR_CLERK_MASTER_KEY", raising=False)
    with pytest.raises(MissingMasterKey):
        get_cipher()


def test_get_cipher_round_trip(monkeypatch):
    from harbor_clerk.secrets import get_cipher

    monkeypatch.setenv(
        "HARBOR_CLERK_MASTER_KEY",
        base64.b64encode(os.urandom(32)).decode(),
    )
    cipher = get_cipher()
    ciphertext, fp = cipher.encrypt(b"top secret")
    assert cipher.decrypt(ciphertext, fp) == b"top secret"

"""Cipher round-trip tests."""

import os

import pytest

from harbor_clerk.secrets.cipher import Cipher


@pytest.fixture
def key() -> bytes:
    return os.urandom(32)


def test_round_trip(key):
    cipher = Cipher(key)
    ciphertext, fingerprint = cipher.encrypt(b"super-secret-app-password")
    assert ciphertext != b"super-secret-app-password"
    assert len(fingerprint) == 8
    plaintext = cipher.decrypt(ciphertext, fingerprint)
    assert plaintext == b"super-secret-app-password"


def test_unicode_round_trip(key):
    cipher = Cipher(key)
    ciphertext, fingerprint = cipher.encrypt("日本語パスワード".encode())
    plaintext = cipher.decrypt(ciphertext, fingerprint)
    assert plaintext.decode() == "日本語パスワード"


def test_each_encryption_produces_different_ciphertext(key):
    """Fernet uses a random IV; identical plaintext encrypts to different ciphertext."""
    cipher = Cipher(key)
    ct1, _ = cipher.encrypt(b"hello")
    ct2, _ = cipher.encrypt(b"hello")
    assert ct1 != ct2

def test_invalid_key_length_raises():
    with pytest.raises(ValueError, match="must be 32 bytes"):
        Cipher(b"too short")

    with pytest.raises(ValueError, match="must be 32 bytes"):
        Cipher(b"\x00" * 33)

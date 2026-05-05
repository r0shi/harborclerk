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


def test_fingerprint_is_deterministic():
    key = b"\x42" * 32
    c1 = Cipher(key)
    c2 = Cipher(key)
    assert c1.fingerprint == c2.fingerprint
    assert len(c1.fingerprint) == 8


def test_fingerprint_differs_per_key():
    c1 = Cipher(b"\x01" * 32)
    c2 = Cipher(b"\x02" * 32)
    assert c1.fingerprint != c2.fingerprint


def test_fingerprint_does_not_leak_key():
    """Fingerprint shouldn't trivially reveal key bits."""
    key = b"\x42" * 32
    cipher = Cipher(key)
    # The fingerprint isn't derived by truncation; assert it's not a prefix or substring
    assert key[:8] != cipher.fingerprint
    assert cipher.fingerprint not in key


def test_decrypt_with_wrong_fingerprint_raises():
    from harbor_clerk.secrets.cipher import KeyMismatch

    key1 = b"\x01" * 32
    key2 = b"\x02" * 32
    cipher1 = Cipher(key1)
    cipher2 = Cipher(key2)

    ciphertext, fp1 = cipher1.encrypt(b"secret")

    # Try to decrypt with cipher2 but pass cipher1's fingerprint —
    # KeyMismatch fires before Fernet would even try.
    with pytest.raises(KeyMismatch):
        cipher2.decrypt(ciphertext, fp1)


def test_decrypt_with_matching_fingerprint_but_wrong_key_raises():
    """Defense in depth: even if fingerprint check is bypassed somehow,
    Fernet's HMAC catches a wrong-key decrypt attempt."""
    from cryptography.fernet import InvalidToken

    key1 = b"\x01" * 32
    key2 = b"\x02" * 32
    cipher1 = Cipher(key1)
    cipher2 = Cipher(key2)

    ciphertext, _ = cipher1.encrypt(b"secret")

    # Pass cipher2's fingerprint so the KeyMismatch check passes —
    # Fernet then fails on its own HMAC check.
    with pytest.raises(InvalidToken):
        cipher2.decrypt(ciphertext, cipher2.fingerprint)

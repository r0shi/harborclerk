"""Fernet wrapper with key fingerprinting.

Fernet is symmetric-AEAD (AES-128-CBC + HMAC-SHA256); the key is 32 random
bytes URL-safe-base64-encoded. We accept the raw 32 bytes as input and do
the base64 encoding inside this module so callers don't have to care about
Fernet's key format.
"""

from __future__ import annotations

import base64
import hmac
from hashlib import sha256

from cryptography.fernet import Fernet


class KeyMismatch(Exception):
    """Raised when stored fingerprint doesn't match the active master key.

    Indicates the ciphertext was encrypted under a different master key
    than the one currently loaded — typically because a deployment moved
    between hosts (e.g. Docker DB restored to macOS without exporting the
    old master key). Caller should surface this to the user as a "reconnect
    mail accounts" prompt rather than treating it as data corruption.
    """


_FINGERPRINT_DOMAIN = b"harbor-clerk-master-key-fingerprint"
_FINGERPRINT_LEN = 8


class Cipher:
    """Encrypt/decrypt with a master key and key-fingerprint check."""

    def __init__(self, master_key: bytes):
        if len(master_key) != 32:
            raise ValueError(f"master_key must be 32 bytes, got {len(master_key)}")
        self._fernet = Fernet(base64.urlsafe_b64encode(master_key))
        self._fingerprint = _compute_fingerprint(master_key)

    @property
    def fingerprint(self) -> bytes:
        return self._fingerprint

    def encrypt(self, plaintext: bytes) -> tuple[bytes, bytes]:
        """Return (ciphertext, fingerprint) for storage."""
        return self._fernet.encrypt(plaintext), self._fingerprint

    def decrypt(self, ciphertext: bytes, stored_fingerprint: bytes) -> bytes:
        if not hmac.compare_digest(stored_fingerprint, self._fingerprint):
            raise KeyMismatch(
                "stored fingerprint does not match active master key; secret was encrypted under a different key"
            )
        return self._fernet.decrypt(ciphertext)


def _compute_fingerprint(master_key: bytes) -> bytes:
    return hmac.new(master_key, _FINGERPRINT_DOMAIN, sha256).digest()[:_FINGERPRINT_LEN]

# src/harbor_clerk/secrets/_accessor.py
"""Process-wide singleton accessor for the Cipher.

Lazy-initialized on first call to keep import-time side-effects to zero
(useful for Alembic migrations, doc generation, etc. that import models
without needing to encrypt anything).
"""

from __future__ import annotations

from harbor_clerk.secrets.cipher import Cipher
from harbor_clerk.secrets.keysource import get_master_key

_cipher: Cipher | None = None


def get() -> Cipher:
    global _cipher
    if _cipher is None:
        _cipher = Cipher(get_master_key())
    return _cipher


def reset() -> None:
    """Drop the cached instance. For tests only."""
    global _cipher
    _cipher = None

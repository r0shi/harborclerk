"""Envelope-encryption primitives for secret storage.

Postgres holds Fernet ciphertext for sensitive values (mail-account app
passwords today; potentially OAuth refresh tokens, off-host MinIO keys, etc.
later). The master key lives in the HARBOR_CLERK_MASTER_KEY env var on
every platform — operator sets it on Docker; macOS Swift reads from
Keychain and exports it on the Python subprocess environment.

Every ciphertext is paired with an 8-byte key fingerprint so cross-deployment
moves degrade gracefully (raise KeyMismatch instead of producing garbage).
"""

from harbor_clerk.secrets.cipher import Cipher, KeyMismatch
from harbor_clerk.secrets.keysource import MissingMasterKey, get_master_key

__all__ = ["Cipher", "KeyMismatch", "MissingMasterKey", "get_master_key"]

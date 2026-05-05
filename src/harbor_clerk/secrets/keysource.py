"""Resolve the master key from environment.

Single env var (HARBOR_CLERK_MASTER_KEY) on every platform:

- Docker: operator sets it directly in compose / env.
- macOS native: Swift Harbor Clerk Server reads from Keychain on startup
  and sets it on the environment of every Python subprocess (api, worker-io,
  worker-cpu, watcher) before launching them. See the Swift MasterKeyManager
  for the Keychain side.

The value must be base64-encoded 32-byte key material. Generation:

    python -c 'import base64, os; print(base64.b64encode(os.urandom(32)).decode())'
"""

from __future__ import annotations

import base64
import binascii
import os

_ENV_VAR = "HARBOR_CLERK_MASTER_KEY"
_KEY_LEN = 32


class MissingMasterKey(Exception):
    """Raised when the master key env var is unset or empty."""


def get_master_key() -> bytes:
    """Return the raw 32-byte master key from HARBOR_CLERK_MASTER_KEY.

    Raises MissingMasterKey if the env var is unset or empty.
    Raises ValueError if the value is not valid base64 or doesn't decode
    to exactly 32 bytes.
    """
    raw = os.environ.get(_ENV_VAR, "").strip()
    if not raw:
        raise MissingMasterKey(
            f"{_ENV_VAR} is not set. On Docker, set it in your compose env. "
            f"On macOS native, this is set automatically by the Harbor Clerk "
            f"Server menubar app — if you're seeing this error in development, "
            f"set it manually with `export {_ENV_VAR}=$(python -c 'import base64, os; "
            f"print(base64.b64encode(os.urandom(32)).decode())')`."
        )
    try:
        decoded = base64.b64decode(raw, validate=True)
    except binascii.Error as exc:
        raise ValueError(f"{_ENV_VAR} is not valid base64: {exc}") from exc
    if len(decoded) != _KEY_LEN:
        raise ValueError(
            f"{_ENV_VAR} must decode to exactly {_KEY_LEN} bytes, got {len(decoded)}"
        )
    return decoded

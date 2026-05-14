# src/harbor_clerk/mail/imap_client.py
"""Thin async wrapper around aioimaplib.IMAP4_SSL.

Exposes only the operations the sync engine needs and translates
aioimaplib's untyped errors into typed exceptions from
`harbor_clerk.mail.exceptions`.

The password is held in `self._password` only between construction and
`login()`. `login()` clears it (`self._password = ""`) before raising or
returning, so the connection object never retains credentials past the
login attempt — making accidental logging of the connection object safe.
"""

from __future__ import annotations

import logging
from typing import Any

import aioimaplib

from harbor_clerk.mail.exceptions import AuthError

logger = logging.getLogger(__name__)


class IMAPConnection:
    """Connection to one IMAP account.

    Lifecycle: __init__ → connect() → login() → (work) → logout().
    Re-use a single instance across multiple commands; create a new one
    for each (account, label) pair the sync engine watches concurrently.
    """

    def __init__(self, *, host: str, port: int, username: str, password: str):
        self.host = host
        self.port = port
        self.username = username
        self._password = password  # cleared in login() once consumed
        self._client: Any | None = None
        self._logged_in = False

    async def connect(self) -> None:
        """Open TCP connection and complete the IMAP server greeting."""
        self._client = aioimaplib.IMAP4_SSL(host=self.host, port=self.port)
        await self._client.wait_hello_from_server()

    async def login(self) -> None:
        """Authenticate. Raises AuthError on NO/BAD response.

        After this call the password is cleared from memory.
        """
        if self._client is None:
            raise RuntimeError("login() called before connect()")
        result, lines = await self._client.login(self.username, self._password)
        # Clear password regardless of outcome — never keep failed credentials
        # around on the connection object.
        self._password = ""
        if result != "OK":
            detail = b" ".join(lines).decode(errors="replace")
            raise AuthError(detail or "IMAP LOGIN failed")
        self._logged_in = True

    async def logout(self) -> None:
        """Close the connection. Idempotent — safe to call when not logged in."""
        if self._client is None:
            return
        try:
            if self._logged_in:
                await self._client.logout()
        except Exception as exc:
            logger.debug("logout() raised: %s — ignoring", exc)
        finally:
            self._logged_in = False
            self._client = None

    async def examine(self, mailbox: str) -> tuple[str, list[bytes]]:
        """Open `mailbox` in read-only mode (IMAP EXAMINE command).

        Server-enforced: STORE/EXPUNGE/COPY/MOVE/APPEND on the selection
        will be rejected by the server. Callers should always prefer this
        over `select()` — `select()` is intentionally not exposed.
        """
        if self._client is None or not self._logged_in:
            raise RuntimeError("examine() called before login()")
        return await self._client.examine(mailbox)

    @property
    def client(self) -> Any:
        """The underlying aioimaplib client. For internal use by the
        sync engine only — exposed so callers can issue UID FETCH etc."""
        if self._client is None:
            raise RuntimeError("connection not established")
        return self._client

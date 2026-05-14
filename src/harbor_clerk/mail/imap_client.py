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

from harbor_clerk.mail.exceptions import AuthError, ReadOnlyViolation

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
        self._require_logged_in("examine")
        return await self._client.examine(mailbox)

    _BLOCKED_UID_VERBS = frozenset({"STORE", "COPY", "MOVE", "EXPUNGE"})

    async def fetch(self, message_set: str, message_parts: str) -> tuple[str, list[bytes]]:
        self._require_logged_in("fetch")
        return await self._client.fetch(message_set, message_parts)

    async def uid(self, command: str, *args: str) -> tuple[str, list[bytes]]:
        """Issue a UID-prefixed command (FETCH, SEARCH only).

        STORE / COPY / MOVE / EXPUNGE are mutating and raise ReadOnlyViolation.
        """
        self._require_logged_in("uid")
        if command.upper() in self._BLOCKED_UID_VERBS:
            raise ReadOnlyViolation(f"uid({command.upper()!r}) is a mutating IMAP command and is blocked")
        return await self._client.uid(command, *args)

    async def uid_search(self, criteria: str) -> tuple[str, list[bytes]]:
        self._require_logged_in("uid_search")
        return await self._client.uid_search(criteria)

    async def list_mailboxes(self, reference: str, mailbox: str) -> tuple[str, list[bytes]]:
        """IMAP LIST. Renamed from `list` to avoid shadowing the builtin."""
        self._require_logged_in("list_mailboxes")
        return await self._client.list(reference, mailbox)

    async def capability(self) -> tuple[str, list[bytes]]:
        self._require_logged_in("capability")
        return await self._client.capability()

    async def idle_start(self, timeout: float = 0) -> tuple[str, list[bytes]]:
        self._require_logged_in("idle_start")
        return await self._client.idle_start(timeout=timeout)

    async def idle_done(self) -> tuple[str, list[bytes]]:
        self._require_logged_in("idle_done")
        return await self._client.idle_done()

    async def wait_server_push(self, timeout: float | None = None) -> list[bytes]:
        self._require_logged_in("wait_server_push")
        return await self._client.wait_server_push(timeout=timeout)

    def _require_logged_in(self, op: str) -> None:
        if self._client is None or not self._logged_in:
            raise RuntimeError(f"{op}() called before login()")

    @property
    def client(self) -> Any:
        """The underlying aioimaplib client. For internal use by the
        sync engine only — exposed so callers can issue UID FETCH etc."""
        if self._client is None:
            raise RuntimeError("connection not established")
        return self._client

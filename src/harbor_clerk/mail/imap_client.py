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

import asyncio
import logging
import time
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.mail.audit import log_imap_command
from harbor_clerk.mail.exceptions import AuthError, ReadOnlyViolation
from harbor_clerk.mail.readonly_imap import ReadOnlyIMAP4_SSL

logger = logging.getLogger(__name__)


class IMAPConnection:
    """Connection to one IMAP account.

    Lifecycle: __init__ → connect() → login() → (work) → logout().
    Re-use a single instance across multiple commands; create a new one
    for each (account, label) pair the sync engine watches concurrently.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        audit_session: AsyncSession | None = None,
        account_id: UUID | None = None,
    ):
        self.host = host
        self.port = port
        self.username = username
        self._password = password  # cleared in login() once consumed
        self._client: Any | None = None
        self._logged_in = False
        self._audit_session = audit_session
        self._account_id = account_id
        self._current_label: str | None = None

    async def _audit(
        self,
        command: str,
        args: tuple,
        result: tuple[str, list[bytes]] | None,
        duration_ms: int,
        error: str | None,
    ) -> None:
        """Write one audit row. Failure must not break the IMAP call."""
        if self._audit_session is None or self._account_id is None:
            return
        if result is None:
            status, lines = "ERROR", []
        else:
            status, lines = result
        response_bytes = sum(len(line) for line in lines) if lines else 0
        try:
            await log_imap_command(
                self._audit_session,
                account_id=self._account_id,
                label_path=self._current_label,
                command=command,
                args=args,
                response_status=status,
                response_bytes=response_bytes,
                duration_ms=duration_ms,
                error=error,
            )
            # Commit per-command so the audit trail is durable even if the
            # surrounding label task is later killed by SIGKILL / OOM /
            # supervisor cancel before audit_session_scope.__aexit__ runs.
            # The audit session is dedicated to writes — short transactions
            # are cheap and bound the loss window to the in-flight call.
            await self._audit_session.commit()
        except Exception as exc:  # never let audit break the IMAP op
            logger.warning("audit log failed for %s: %s", command, exc)
            try:
                await self._audit_session.rollback()
            except Exception:
                pass

    async def connect(self) -> None:
        """Open TCP connection and complete the IMAP server greeting."""
        self._client = ReadOnlyIMAP4_SSL(host=self.host, port=self.port)
        await self._client.wait_hello_from_server()

    async def login(self) -> None:
        """Authenticate. Raises AuthError on NO/BAD response.

        After this call the password is cleared from memory.
        """
        if self._client is None:
            raise RuntimeError("login() called before connect()")
        start = time.perf_counter()
        err: str | None = None
        # Capture password now — it's cleared below regardless of outcome
        _args = (self.username, self._password)
        try:
            result, lines = await self._client.login(self.username, self._password)
        except Exception as exc:
            err = repr(exc)
            self._password = ""
            duration_ms = int((time.perf_counter() - start) * 1000)
            await self._audit("LOGIN", _args, None, duration_ms, err)
            raise
        # Clear password regardless of outcome — never keep failed credentials
        # around on the connection object.
        self._password = ""
        duration_ms = int((time.perf_counter() - start) * 1000)
        if result != "OK":
            detail = b" ".join(lines).decode(errors="replace")
            await self._audit("LOGIN", _args, (result, lines), duration_ms, None)
            raise AuthError(detail or "IMAP LOGIN failed")
        self._logged_in = True
        await self._audit("LOGIN", _args, (result, lines), duration_ms, None)

    async def logout(self) -> None:
        """Close the connection. Idempotent — safe to call when not logged in.

        Dropping `self._client` is not enough to release the socket. aioimaplib
        schedules `loop.create_connection(...)` in `IMAP4.__init__`, so the
        transport is owned by the event loop; losing the Python reference leaves
        it open until the server reaps it — roughly 30 minutes on Gmail. That
        was survivable while a failed label task was never retried, because it
        leaked one socket per watcher lifetime. With respawn-on-failure it
        repeats: at the backoff cap, about twelve attempts an hour with roughly
        six alive at once, against Gmail's limit of fifteen simultaneous IMAP
        connections per account — enough for one sick label to lock out the
        healthy ones.

        The LOGOUT itself is still conditional (there is nothing to log out of
        if login never succeeded), but the transport close is not.
        """
        client, self._client = self._client, None
        logged_in, self._logged_in = self._logged_in, False
        if client is None:
            return
        try:
            if logged_in:
                await client.logout()
        except Exception as exc:
            logger.debug("logout() raised: %s — ignoring", exc)
        except BaseException:
            # CancelledError is not an Exception, so it used to leave before the
            # close below — reintroducing the leak on the one path where the
            # docstring promises it cannot happen. Close, then re-raise.
            self._close_transport(client)
            raise

        # Reach past the wrapper for the socket. These are aioimaplib internals,
        # like the EXAMINE state fix in readonly_imap — pinned by tests so an
        # upstream rename fails loudly rather than silently leaking again.
        self._close_transport(client)

        # `create_connection` runs as a task nobody awaits. If it failed — the
        # common case when we are here after a connect error — its exception is
        # never retrieved and asyncio logs "Task exception was never retrieved"
        # at shutdown, which is how this class of bug stayed invisible.
        task = getattr(client, "_client_task", None)
        if task is not None:
            if not task.done():
                task.cancel()
            # gather(return_exceptions=True) retrieves it without swallowing a
            # cancellation aimed at *us* — `suppress(CancelledError)` around a
            # bare await would absorb the caller's own cancel.
            await asyncio.gather(task, return_exceptions=True)

    @staticmethod
    def _close_transport(client: Any) -> None:
        protocol = getattr(client, "protocol", None)
        transport = getattr(protocol, "transport", None)
        if transport is None:
            return
        try:
            transport.close()
        except Exception as exc:
            logger.debug("transport.close() raised: %s — ignoring", exc)

    async def examine(self, mailbox: str) -> tuple[str, list[bytes]]:
        """Open `mailbox` in read-only mode (IMAP EXAMINE command).

        Server-enforced: STORE/EXPUNGE/COPY/MOVE/APPEND on the selection
        will be rejected by the server. Callers should always prefer this
        over `select()` — `select()` is intentionally not exposed.
        """
        self._require_logged_in("examine")
        start = time.perf_counter()
        err: str | None = None
        try:
            result = await self._client.examine(mailbox)
        except Exception as exc:
            err = repr(exc)
            duration_ms = int((time.perf_counter() - start) * 1000)
            await self._audit("EXAMINE", (mailbox,), None, duration_ms, err)
            raise
        duration_ms = int((time.perf_counter() - start) * 1000)
        self._current_label = mailbox  # subsequent fetch/uid calls inherit this
        await self._audit("EXAMINE", (mailbox,), result, duration_ms, None)
        return result

    _BLOCKED_UID_VERBS = frozenset({"STORE", "COPY", "MOVE", "EXPUNGE"})

    async def fetch(self, message_set: str, message_parts: str) -> tuple[str, list[bytes]]:
        self._require_logged_in("fetch")
        start = time.perf_counter()
        err: str | None = None
        try:
            result = await self._client.fetch(message_set, message_parts)
        except Exception as exc:
            err = repr(exc)
            duration_ms = int((time.perf_counter() - start) * 1000)
            await self._audit("FETCH", (message_set, message_parts), None, duration_ms, err)
            raise
        duration_ms = int((time.perf_counter() - start) * 1000)
        await self._audit("FETCH", (message_set, message_parts), result, duration_ms, None)
        return result

    async def uid(self, command: str, *args: str) -> tuple[str, list[bytes]]:
        """Issue a UID-prefixed command (FETCH, SEARCH only).

        STORE / COPY / MOVE / EXPUNGE are mutating and raise ReadOnlyViolation.
        Blocked attempts are audited with status="ERROR" before the exception
        propagates.
        """
        self._require_logged_in("uid")
        start = time.perf_counter()
        if command.upper() in self._BLOCKED_UID_VERBS:
            exc = ReadOnlyViolation(f"uid({command.upper()!r}) is a mutating IMAP command and is blocked")
            duration_ms = int((time.perf_counter() - start) * 1000)
            await self._audit(command.upper(), args, None, duration_ms, repr(exc))
            raise exc
        err: str | None = None
        try:
            result = await self._client.uid(command, *args)
        except Exception as exc:
            err = repr(exc)
            duration_ms = int((time.perf_counter() - start) * 1000)
            await self._audit(command.upper(), args, None, duration_ms, err)
            raise
        duration_ms = int((time.perf_counter() - start) * 1000)
        await self._audit(command.upper(), args, result, duration_ms, None)
        return result

    async def uid_search(self, *criteria: str, charset: str | None = "utf-8") -> tuple[str, list[bytes]]:
        """IMAP UID SEARCH. Accepts variadic criteria (e.g., uid_search('SINCE', '1-Jan-2024'))."""
        self._require_logged_in("uid_search")
        start = time.perf_counter()
        err: str | None = None
        try:
            result = await self._client.uid_search(*criteria, charset=charset)
        except Exception as exc:
            err = repr(exc)
            duration_ms = int((time.perf_counter() - start) * 1000)
            await self._audit("UID_SEARCH", criteria, None, duration_ms, err)
            raise
        duration_ms = int((time.perf_counter() - start) * 1000)
        await self._audit("UID_SEARCH", criteria, result, duration_ms, None)
        return result

    async def list_mailboxes(self, reference: str, mailbox: str) -> tuple[str, list[bytes]]:
        """IMAP LIST. Renamed from `list` to avoid shadowing the builtin."""
        self._require_logged_in("list_mailboxes")
        start = time.perf_counter()
        err: str | None = None
        try:
            result = await self._client.list(reference, mailbox)
        except Exception as exc:
            err = repr(exc)
            duration_ms = int((time.perf_counter() - start) * 1000)
            await self._audit("LIST", (reference, mailbox), None, duration_ms, err)
            raise
        duration_ms = int((time.perf_counter() - start) * 1000)
        await self._audit("LIST", (reference, mailbox), result, duration_ms, None)
        return result

    def has_capability(self, name: str) -> bool:
        """Check whether the server announced a capability during the
        initial CAPABILITY response (aioimaplib caches this at connect time).
        Use this instead of issuing a separate CAPABILITY command.

        NOT AUDITED — synchronous local-cache lookup with no IMAP round trip.
        """
        self._require_logged_in("has_capability")
        return self._client.has_capability(name)

    async def idle_start(self, timeout: float = 0) -> None:
        """Send IDLE and return when the server has accepted it. The
        underlying aioimaplib method returns an asyncio.Future tracking the
        completion of IDLE; we don't expose it — callers should treat IDLE
        as a fire-and-await operation paired with idle_done()."""
        self._require_logged_in("idle_start")
        start = time.perf_counter()
        err: str | None = None
        try:
            await self._client.idle_start(timeout=timeout)
        except Exception as exc:
            err = repr(exc)
            duration_ms = int((time.perf_counter() - start) * 1000)
            await self._audit("IDLE_START", (), None, duration_ms, err)
            raise
        duration_ms = int((time.perf_counter() - start) * 1000)
        # idle_start returns a Future/coroutine, not the usual (status, lines) tuple.
        # Record it as a synthetic OK row — the IDLE session is bracketed by
        # idle_start (this) and idle_done (not audited — sync, no return shape).
        await self._audit("IDLE_START", (), ("OK", []), duration_ms, None)

    def idle_done(self) -> None:
        """Stop an in-progress IDLE. aioimaplib's idle_done is synchronous and
        cancels the internal IDLE waiter; it has no async result.

        NOT AUDITED — synchronous wrapper with no return shape to record.
        The surrounding idle_start is audited to bracket the IDLE session.
        """
        self._require_logged_in("idle_done")
        self._client.idle_done()

    async def wait_server_push(self, timeout: float | None = None) -> list[bytes]:
        """NOT AUDITED — IDLE pushes fire continuously and would flood the log.
        The preceding idle_start is audited to bracket each IDLE session;
        idle_done is also skipped (synchronous, no return shape).
        """
        self._require_logged_in("wait_server_push")
        return await self._client.wait_server_push(timeout=timeout)

    def _require_logged_in(self, op: str) -> None:
        if self._client is None or not self._logged_in:
            raise RuntimeError(f"{op}() called before login()")

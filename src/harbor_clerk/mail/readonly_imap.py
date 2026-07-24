"""Read-only subclass of aioimaplib.IMAP4_SSL.

Layer-2 defense beneath IMAPConnection. The wrapper exposes a curated
read-only surface and removes the .client escape hatch; this subclass
provides a second, lower-level guarantee: every mutating IMAP command
raises ReadOnlyViolation before any bytes go on the wire.

If both layers fail, layer 1 (the IMAP protocol's EXAMINE selection)
still rejects mutations at the server.
"""

from __future__ import annotations

import aioimaplib

from harbor_clerk.mail.exceptions import ReadOnlyViolation

_BLOCKED_UID_VERBS = frozenset({"STORE", "COPY", "MOVE", "EXPUNGE"})


class ReadOnlyIMAP4_SSL(aioimaplib.IMAP4_SSL):
    """aioimaplib.IMAP4_SSL with all mutating methods replaced by raisers."""

    async def select(self, *_args, **_kwargs):
        raise ReadOnlyViolation("select() opens the mailbox read-write; use examine() instead")

    async def examine(self, mailbox: str = "INBOX"):
        """EXAMINE, and record that the connection is now in the selected state.

        Works around an aioimaplib bug (2.0.1). Per RFC 3501 §6.3.2 EXAMINE is
        identical to SELECT except that the mailbox is read-only, and both enter
        the *selected* state. aioimaplib only tracks that for SELECT — which is
        `@change_state`-decorated and assigns `state = SELECTED` — while EXAMINE
        goes through the generic `simple_command` path and leaves `state` at
        AUTH.

        Because this class deliberately blocks `select()`, nothing else ever
        moved the state, so every following UID SEARCH / UID FETCH / IDLE was
        rejected *client-side* with `Abort: command SEARCH illegal in state
        AUTH` before a byte reached the server. That made IMAP ingest
        impossible: labels stayed at `last_synced_at = NULL` forever (#557).

        Assigning under `state_condition` and notifying mirrors what
        `change_state` does for SELECT, so callers blocked in
        `protocol.wait(...)` are woken rather than hanging.

        This narrows rather than widens the read-only guarantee's surface: the
        selection really is read-only — the server enforces it — we are only
        correcting the client's bookkeeping to match.
        """
        response = await super().examine(mailbox)
        if response.result == "OK":
            async with self.protocol.state_condition:
                self.protocol.state = aioimaplib.SELECTED
                self.protocol.state_condition.notify_all()
        return response

    async def store(self, *_args, **_kwargs):
        raise ReadOnlyViolation("store() mutates message flags")

    async def copy(self, *_args, **_kwargs):
        raise ReadOnlyViolation("copy() writes to the target mailbox")

    async def move(self, *_args, **_kwargs):
        raise ReadOnlyViolation("move() removes from the source mailbox")

    async def expunge(self, *_args, **_kwargs):
        raise ReadOnlyViolation("expunge() permanently removes messages")

    async def append(self, *_args, **_kwargs):
        raise ReadOnlyViolation("append() adds a message to a mailbox")

    async def create(self, *_args, **_kwargs):
        raise ReadOnlyViolation("create() creates a new mailbox")

    async def delete(self, *_args, **_kwargs):
        raise ReadOnlyViolation("delete() removes a mailbox")

    async def rename(self, *_args, **_kwargs):
        raise ReadOnlyViolation("rename() renames a mailbox")

    async def subscribe(self, *_args, **_kwargs):
        raise ReadOnlyViolation("subscribe() mutates the user's subscription list")

    async def unsubscribe(self, *_args, **_kwargs):
        raise ReadOnlyViolation("unsubscribe() mutates the user's subscription list")

    async def uid(self, command, *args, **kwargs):
        if command.upper() in _BLOCKED_UID_VERBS:
            raise ReadOnlyViolation(f"uid({command.upper()!r}) is a mutating IMAP command")
        return await super().uid(command, *args, **kwargs)

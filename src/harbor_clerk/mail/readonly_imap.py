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

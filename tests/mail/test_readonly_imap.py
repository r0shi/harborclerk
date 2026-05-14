"""ReadOnlyIMAP4_SSL must refuse every mutating IMAP command.

This is the layer-2 defense beneath IMAPConnection. If IMAPConnection
itself ever drifts (a new method is added that mutates), or someone
constructs a raw aioimaplib client by mistake, this subclass still
refuses to put mutating bytes on the wire.
"""

from __future__ import annotations

import pytest

from harbor_clerk.mail.exceptions import ReadOnlyViolation
from harbor_clerk.mail.readonly_imap import ReadOnlyIMAP4_SSL


@pytest.mark.parametrize(
    "method,args",
    [
        ("select", ("INBOX",)),
        ("store", ("1", r"+FLAGS", r"(\Seen)")),
        ("copy", ("1", "Archive")),
        ("move", ("1", "Archive")),
        ("expunge", ()),
        ("append", ("INBOX", None, None, b"From: x\r\n\r\nbody")),
        ("create", ("NewBox",)),
        ("delete", ("OldBox",)),
        ("rename", ("Old", "New")),
        ("subscribe", ("INBOX",)),
        ("unsubscribe", ("INBOX",)),
    ],
)
async def test_mutating_method_raises(method, args):
    client = ReadOnlyIMAP4_SSL.__new__(ReadOnlyIMAP4_SSL)  # bypass network init
    with pytest.raises(ReadOnlyViolation, match=method):
        await getattr(client, method)(*args)


@pytest.mark.parametrize("verb", ["STORE", "store", "COPY", "MOVE", "EXPUNGE"])
async def test_uid_blocks_mutating_verbs(verb):
    client = ReadOnlyIMAP4_SSL.__new__(ReadOnlyIMAP4_SSL)
    with pytest.raises(ReadOnlyViolation, match=verb.upper()):
        await client.uid(verb, "1:*")


def test_block_list_matches_aioimaplib_surface():
    """Catches the case where aioimaplib adds a new mutating method.

    If this test fails, aioimaplib has grown a method that we haven't
    classified as read or mutating — review the new method and either
    expose it via IMAPConnection (read) or add an override here (mutating).
    """
    import aioimaplib

    known_read = {
        "examine",
        "fetch",
        "search",
        "uid",
        "uid_search",
        "list",
        "lsub",
        "status",
        "noop",
        "has_capability",
        "id",
        "namespace",
        "get_state",
        "getquotaroot",
        "idle",
        "idle_start",
        "idle_done",
        "wait_server_push",
        "has_pending_idle",
        "stop_wait_server_push",
        "wait_hello_from_server",
        "login",
        "logout",
        "xoauth2",
        "enable",
        "check",
        "close",
        "create_client",
    }
    known_mutating = {
        "select",
        "store",
        "copy",
        "move",
        "expunge",
        "append",
        "create",
        "delete",
        "rename",
        "subscribe",
        "unsubscribe",
    }
    expected = known_read | known_mutating
    actual = {m for m in dir(aioimaplib.IMAP4_SSL) if not m.startswith("_")}
    actual.discard("TIMEOUT_SECONDS")  # class attribute, not a method

    new_methods = actual - expected
    assert not new_methods, (
        f"aioimaplib has new methods that need classification: {new_methods}. "
        f"Add each to known_read or known_mutating (and an override in "
        f"ReadOnlyIMAP4_SSL if mutating)."
    )

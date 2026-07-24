"""ReadOnlyIMAP4_SSL must refuse every mutating IMAP command.

This is the layer-2 defense beneath IMAPConnection. If IMAPConnection
itself ever drifts (a new method is added that mutates), or someone
constructs a raw aioimaplib client by mistake, this subclass still
refuses to put mutating bytes on the wire.
"""

from __future__ import annotations

import aioimaplib
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


async def test_imap_connection_uses_readonly_subclass(monkeypatch):
    """IMAPConnection.connect() must instantiate ReadOnlyIMAP4_SSL.

    If a future refactor drops back to bare aioimaplib.IMAP4_SSL the
    layer-2 defense disappears silently — this test pins it.
    """
    from harbor_clerk.mail import imap_client
    from harbor_clerk.mail.imap_client import IMAPConnection
    from harbor_clerk.mail.readonly_imap import ReadOnlyIMAP4_SSL

    captured: list[type] = []
    original = imap_client.ReadOnlyIMAP4_SSL

    class _Probe(original):
        def __init__(self, *a, **kw):
            captured.append(type(self))

        async def wait_hello_from_server(self):
            pass

    monkeypatch.setattr(imap_client, "ReadOnlyIMAP4_SSL", _Probe)
    conn = IMAPConnection(host="h", port=993, username="u", password="p")
    await conn.connect()

    assert captured, "ReadOnlyIMAP4_SSL was not instantiated"
    assert issubclass(captured[0], ReadOnlyIMAP4_SSL)


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


# --------------------------------------------------------------------------
# #557 — EXAMINE must leave the connection in the SELECTED state
# --------------------------------------------------------------------------


class _StubProtocol:
    """Minimal stand-in for IMAP4ClientProtocol's state machinery."""

    def __init__(self, state=aioimaplib.AUTH):
        import asyncio

        self.state = state
        self.state_condition = asyncio.Condition()


def _client_with_stub_examine(result: str, *, starting_state=aioimaplib.AUTH):
    """A ReadOnlyIMAP4_SSL whose super().examine() returns `result`."""
    client = ReadOnlyIMAP4_SSL.__new__(ReadOnlyIMAP4_SSL)  # bypass network init
    client.protocol = _StubProtocol(starting_state)

    async def fake_examine(self, mailbox="INBOX"):
        return aioimaplib.Response(result, [b"* 12 EXISTS", b"* OK [UIDVALIDITY 42]"])

    return client, fake_examine


async def test_examine_moves_connection_into_selected_state(monkeypatch):
    """The #557 regression.

    aioimaplib 2.0.1 routes EXAMINE through simple_command, which never sets
    SELECTED — only the @change_state-decorated select() does, and this class
    blocks select() by design. Without this override the client stayed at AUTH
    and every UID SEARCH/FETCH/IDLE was rejected before reaching the server.
    """
    client, fake_examine = _client_with_stub_examine("OK")
    monkeypatch.setattr(aioimaplib.IMAP4_SSL, "examine", fake_examine, raising=True)

    assert client.protocol.state == aioimaplib.AUTH
    resp = await client.examine("kickstarter")

    assert resp.result == "OK"
    assert client.protocol.state == aioimaplib.SELECTED, (
        "EXAMINE must enter the selected state (RFC 3501 §6.3.2); otherwise "
        "UID SEARCH aborts client-side with 'illegal in state AUTH'"
    )


async def test_examine_deselects_when_server_refuses(monkeypatch):
    """A failed EXAMINE deselects — RFC 3501: "no mailbox is selected".

    The stub starts at SELECTED deliberately. Starting from AUTH, "leave the
    state alone" and "reset to AUTH" are indistinguishable, and the earlier
    version of this test passed for both the correct and the broken
    implementation.

    Live shape: MailObserver keeps one connection per label across ticks. Tick N
    examines OK, tick N+1 gets a NO (renamed label, transient refusal). If the
    client still believed it was SELECTED, `ingest.fetch_eml_bytes` — which does
    not re-examine — would put a UID FETCH on the wire against no selection.
    """
    client, fake_examine = _client_with_stub_examine("NO", starting_state=aioimaplib.SELECTED)
    monkeypatch.setattr(aioimaplib.IMAP4_SSL, "examine", fake_examine, raising=True)

    resp = await client.examine("nonexistent-label")

    assert resp.result == "NO"
    assert client.protocol.state == aioimaplib.AUTH, (
        "a refused EXAMINE must deselect; a stale SELECTED lets UID FETCH/IDLE "
        "past the client-side gate and fail on the wire instead"
    )


async def test_examine_satisfies_the_command_state_gate(monkeypatch):
    """The state left by examine() must satisfy aioimaplib's own command table.

    This reads `Commands[...].valid_states` rather than issuing anything — the
    real UID SEARCH over a socket is `test_imap_state_e2e.py`.
    """
    client, fake_examine = _client_with_stub_examine("OK")
    monkeypatch.setattr(aioimaplib.IMAP4_SSL, "examine", fake_examine, raising=True)

    await client.examine("kickstarter")

    allowed_states = aioimaplib.Commands["SEARCH"].valid_states
    assert client.protocol.state in allowed_states
    assert client.protocol.state in aioimaplib.Commands["UID"].valid_states
    assert client.protocol.state in aioimaplib.Commands["IDLE"].valid_states


async def test_examine_wakes_waiters_on_the_state_condition(monkeypatch):
    """Taking the lock is only worth it if the notify actually wakes someone.

    aioimaplib's `protocol.wait(...)` blocks on `state_condition` — that is why
    `change_state` notifies, and why this override mirrors it. Without the
    notify, a waiter would sleep past a state change that already happened.
    """
    import asyncio

    client, fake_examine = _client_with_stub_examine("OK")
    monkeypatch.setattr(aioimaplib.IMAP4_SSL, "examine", fake_examine, raising=True)

    woke = asyncio.Event()

    async def _waiter():
        async with client.protocol.state_condition:
            await client.protocol.state_condition.wait_for(lambda: client.protocol.state == aioimaplib.SELECTED)
            woke.set()

    task = asyncio.create_task(_waiter())
    await asyncio.sleep(0)  # let the waiter reach wait_for

    await client.examine("kickstarter")
    await asyncio.wait_for(woke.wait(), timeout=2)

    assert woke.is_set()
    task.cancel()

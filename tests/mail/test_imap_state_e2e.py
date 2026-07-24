"""EXAMINE -> UID SEARCH against a real socket and the real aioimaplib client.

Every other mail test drives `FakeIMAP`, which replaces `aioimaplib.IMAP4_SSL`
wholesale. That is fast and right for testing sync logic — but it also mocks
away the client state machine, which is exactly where #557 lived: EXAMINE left
the connection in AUTH, so `UID SEARCH` aborted client-side and no label ever
synced. A fake that never had the bug could never catch it.

So this module speaks real IMAP over a loopback socket. It is deliberately the
only test here that does, and it stays minimal: enough of RFC 3501 to get from
greeting to UID SEARCH, nothing more.

TLS is skipped (no cert to manage) by overriding `create_client`; the client
state machine is transport-independent, and it is the state machine under test.
"""

from __future__ import annotations

import asyncio

import aioimaplib
import pytest

from harbor_clerk.mail.readonly_imap import ReadOnlyIMAP4_SSL


class _MiniIMAPServer:
    """Just enough IMAP to answer CAPABILITY, LOGIN, EXAMINE, UID SEARCH, LOGOUT."""

    def __init__(self) -> None:
        self.server: asyncio.Server | None = None
        self.commands: list[str] = []
        self._writers: list[asyncio.StreamWriter] = []

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        # Since 3.12 `wait_closed()` also waits for open connections to finish,
        # so a client that never logged out would hang teardown forever. Drop
        # the sockets first, then wait.
        for writer in self._writers:
            if not writer.is_closing():
                writer.close()
        self._writers.clear()
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.append(writer)
        writer.write(b"* OK [CAPABILITY IMAP4rev1 IDLE] mini ready\r\n")
        await writer.drain()
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                parts = line.decode(errors="replace").strip().split()
                if len(parts) < 2:
                    continue
                tag, verb = parts[0], parts[1].upper()
                # Records the bare verb, so UID SEARCH lands here as 'UID'.
                self.commands.append(verb)

                if verb == "CAPABILITY":
                    writer.write(b"* CAPABILITY IMAP4rev1 IDLE\r\n" + f"{tag} OK done\r\n".encode())
                elif verb == "LOGIN":
                    writer.write(f"{tag} OK logged in\r\n".encode())
                elif verb == "EXAMINE":
                    writer.write(
                        b"* 3 EXISTS\r\n"
                        b"* OK [UIDVALIDITY 99] uidvalidity\r\n"
                        b"* OK [UIDNEXT 4] next\r\n" + f"{tag} OK [READ-ONLY] EXAMINE completed\r\n".encode()
                    )
                elif verb == "UID" and len(parts) > 2 and parts[2].upper() == "SEARCH":
                    writer.write(b"* SEARCH 1 2 3\r\n" + f"{tag} OK UID SEARCH completed\r\n".encode())
                elif verb == "LOGOUT":
                    writer.write(b"* BYE\r\n" + f"{tag} OK logout\r\n".encode())
                    await writer.drain()
                    return
                else:
                    writer.write(f"{tag} OK ok\r\n".encode())
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            return


class _PlaintextReadOnlyIMAP(ReadOnlyIMAP4_SSL):
    """The production class, minus TLS. `examine` resolves exactly as it does live."""

    def create_client(self, host, port, loop, conn_lost_cb=None, ssl_context=None):
        aioimaplib.IMAP4.create_client(self, host, port, loop, conn_lost_cb, None)


@pytest.fixture
async def imap_server():
    server = _MiniIMAPServer()
    port = await server.start()
    try:
        yield server, port
    finally:
        await server.stop()


async def test_examine_then_uid_search_succeeds(imap_server):
    """The #557 end-to-end path: after EXAMINE, UID SEARCH must reach the server.

    Without the `examine` override this fails as
    `Abort: command SEARCH illegal in state AUTH` — the exact error the live
    watcher logged on every tick, forever, for the one watched label.
    """
    _server, port = imap_server
    client = _PlaintextReadOnlyIMAP(host="127.0.0.1", port=port, timeout=10)
    await client.wait_hello_from_server()
    await client.login("user", "password")

    examine = await client.examine("kickstarter")
    assert examine.result == "OK"
    assert client.protocol.state == aioimaplib.SELECTED

    search = await client.uid_search("ALL")
    assert search.result == "OK"
    assert b"1 2 3" in search.lines[0]

    await client.logout()


async def test_baseline_aioimaplib_still_has_the_bug(imap_server):
    """Pin the upstream behaviour the override compensates for.

    If a future aioimaplib fixes EXAMINE's state transition, this test fails —
    which is the signal to delete the override rather than carry it forever.
    """
    _server, port = imap_server
    client = aioimaplib.IMAP4(host="127.0.0.1", port=port, timeout=10)
    await client.wait_hello_from_server()
    await client.login("user", "password")

    examine = await client.examine("kickstarter")
    assert examine.result == "OK"

    try:
        assert client.protocol.state == aioimaplib.AUTH, (
            "aioimaplib now tracks EXAMINE's state transition — remove the "
            "ReadOnlyIMAP4_SSL.examine override, it is no longer needed"
        )
        with pytest.raises(aioimaplib.Abort, match="illegal in state AUTH"):
            await client.uid_search("ALL")
    finally:
        await client.logout()

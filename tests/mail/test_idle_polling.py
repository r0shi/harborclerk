"""IDLE supervisor + 2-minute polling fallback."""

import asyncio

import pytest

from harbor_clerk.mail.idle import poll_or_idle_loop, server_supports_idle
from harbor_clerk.mail.imap_client import IMAPConnection


@pytest.fixture
def mock_aioimap(monkeypatch):
    from tests.mail.conftest import FakeIMAP

    monkeypatch.setattr("harbor_clerk.mail.imap_client.ReadOnlyIMAP4_SSL", FakeIMAP)
    return FakeIMAP


async def test_server_supports_idle_true(mock_aioimap):
    mock_aioimap.set_login_response("OK", b"OK")
    mock_aioimap.set_capability_response("OK", [b"CAPABILITY IMAP4rev1 IDLE LITERAL+"])
    conn = IMAPConnection(host="h", port=993, username="u", password="p")
    await conn.connect()
    await conn.login()
    assert await server_supports_idle(conn) is True


async def test_server_supports_idle_false(mock_aioimap):
    mock_aioimap.set_login_response("OK", b"OK")
    mock_aioimap.set_capability_response("OK", [b"CAPABILITY IMAP4rev1 LITERAL+"])  # no IDLE
    conn = IMAPConnection(host="h", port=993, username="u", password="p")
    await conn.connect()
    await conn.login()
    assert await server_supports_idle(conn) is False


async def test_poll_loop_invokes_callback_on_each_tick(mock_aioimap):
    """When IDLE is unsupported, poll_or_idle_loop falls back to a 2-minute
    poll. We test with a tight interval so the test completes quickly."""
    mock_aioimap.set_login_response("OK", b"OK")
    mock_aioimap.set_capability_response("OK", [b"CAPABILITY IMAP4rev1"])  # no IDLE

    conn = IMAPConnection(host="h", port=993, username="u", password="p")
    await conn.connect()
    await conn.login()

    tick_count = 0

    async def on_tick(c: IMAPConnection) -> None:
        nonlocal tick_count
        tick_count += 1
        if tick_count >= 3:
            raise asyncio.CancelledError("done")

    with pytest.raises(asyncio.CancelledError):
        await poll_or_idle_loop(conn, on_tick=on_tick, poll_interval=0.05)

    assert tick_count == 3


async def test_idle_loop_responds_to_exists_event(mock_aioimap):
    """When server supports IDLE, the loop fires the callback on EXISTS push."""
    mock_aioimap.set_login_response("OK", b"OK")
    mock_aioimap.set_capability_response("OK", [b"CAPABILITY IMAP4rev1 IDLE"])
    mock_aioimap.set_idle_events(
        [
            b"* 5 EXISTS",
            b"* 6 EXISTS",
        ]
    )

    conn = IMAPConnection(host="h", port=993, username="u", password="p")
    await conn.connect()
    await conn.login()

    tick_count = 0

    async def on_tick(c: IMAPConnection) -> None:
        nonlocal tick_count
        tick_count += 1
        if tick_count >= 2:
            raise asyncio.CancelledError("done")

    with pytest.raises(asyncio.CancelledError):
        await poll_or_idle_loop(conn, on_tick=on_tick, poll_interval=120)

    assert tick_count == 2

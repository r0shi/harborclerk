"""End-to-end test against a real Dovecot IMAP server.

Skipped by default. To run: ensure Dovecot is reachable on
DOVECOT_TEST_HOST:DOVECOT_TEST_PORT (default localhost:1143) with a known
test account, then `uv run pytest -m integration tests/integration/`.

The dovecot setup is documented in docker/dovecot.test.Dockerfile (a
follow-up task can ship a docker-compose.test.yml that brings it up).

This test does NOT use `FakeIMAP` — it goes through real aioimaplib to
the real server.
"""

from __future__ import annotations

import os
import socket

import pytest

from harbor_clerk.mail import IMAPConnection, discover_folders

pytestmark = pytest.mark.integration

DOVECOT_HOST = os.environ.get("DOVECOT_TEST_HOST", "localhost")
DOVECOT_PORT = int(os.environ.get("DOVECOT_TEST_PORT", "1143"))
DOVECOT_USER = os.environ.get("DOVECOT_TEST_USER", "testuser")
DOVECOT_PASSWORD = os.environ.get("DOVECOT_TEST_PASSWORD", "testpass")


def _dovecot_reachable() -> bool:
    try:
        with socket.create_connection((DOVECOT_HOST, DOVECOT_PORT), timeout=1):
            return True
    except OSError:
        return False


pytestmark_skip = pytest.mark.skipif(
    not _dovecot_reachable(),
    reason=f"Dovecot not reachable at {DOVECOT_HOST}:{DOVECOT_PORT}",
)


@pytestmark_skip
async def test_e2e_connect_login_list_folders():
    conn = IMAPConnection(
        host=DOVECOT_HOST,
        port=DOVECOT_PORT,
        username=DOVECOT_USER,
        password=DOVECOT_PASSWORD,
    )
    await conn.connect()
    await conn.login()
    folders = await discover_folders(conn)
    await conn.logout()
    paths = [f.path for f in folders]
    assert "INBOX" in paths


# Note: a full sync_label_initial e2e test requires injecting test mail
# into the dovecot container first. That setup belongs in a separate
# integration suite and is deferred — the unit tests cover the sync
# logic; this file just verifies aioimaplib + our wrapper actually talk
# to a real server.


@pytestmark_skip
async def test_no_mutation_invariant_against_dovecot():
    """End-to-end invariant: a full mail-ingest cycle must not mutate
    server state.

    Strategy:
      1. Snapshot the mailbox via an INDEPENDENT raw IMAP client (NOT
         our wrapper) — capture: message count, per-UID flags, folder
         list, UIDVALIDITY, UIDNEXT.
      2. Run Harbor Clerk's full ingestion path: connect → examine →
         uid_search → uid("FETCH", ...) → idle for 3s → logout.
      3. Snapshot again the same way.
      4. Diff: assert equality on every snapshotted field.

    If anything differs, the suite has detected a mutation that
    layers 1–3 missed.
    """
    import asyncio

    import aioimaplib

    from harbor_clerk.mail.imap_client import IMAPConnection

    async def snapshot() -> dict:
        raw = aioimaplib.IMAP4_SSL(host=DOVECOT_HOST, port=DOVECOT_PORT)
        await raw.wait_hello_from_server()
        await raw.login(DOVECOT_USER, DOVECOT_PASSWORD)
        _list_status, list_lines = await raw.list("", "*")
        _exam_status, exam_lines = await raw.examine("INBOX")
        uidvalidity = next((line for line in exam_lines if b"UIDVALIDITY" in line), None)
        uidnext = next((line for line in exam_lines if b"UIDNEXT" in line), None)
        _search_status, search_lines = await raw.uid_search("ALL")
        _fetch_status, fetch_lines = await raw.uid("FETCH", "1:*", "(FLAGS)")
        await raw.logout()
        return {
            "list": list_lines,
            "uidvalidity": uidvalidity,
            "uidnext": uidnext,
            "search": search_lines,
            "fetch_flags": fetch_lines,
        }

    before = await snapshot()

    # Run the full ingestion path under test.
    conn = IMAPConnection(
        host=DOVECOT_HOST,
        port=DOVECOT_PORT,
        username=DOVECOT_USER,
        password=DOVECOT_PASSWORD,
    )
    await conn.connect()
    await conn.login()
    await conn.examine("INBOX")
    await conn.uid_search("ALL")
    await conn.uid("FETCH", "1:*", "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
    # Brief IDLE to exercise the push path.
    await conn.idle_start(timeout=3)
    try:
        await asyncio.wait_for(conn.wait_server_push(), timeout=3)
    except (TimeoutError, asyncio.TimeoutError):
        pass
    conn.idle_done()  # sync — no await
    await conn.logout()

    after = await snapshot()

    assert before == after, f"IMAP server state changed during ingestion:\n  before: {before}\n  after:  {after}"

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

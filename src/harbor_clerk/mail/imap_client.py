"""Thin async wrapper around aioimaplib.IMAP4_SSL.

Exposes only the operations the sync engine needs (LOGIN, LIST, SELECT,
UID FETCH/SEARCH, IDLE, LOGOUT) and translates aioimaplib's untyped
errors into typed exceptions from `harbor_clerk.mail.exceptions`.

The password is accepted in the constructor but never stored as an
attribute — it lives in a closure during `login()` and goes out of scope
afterwards. This makes accidental logging of the connection object safe.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class IMAPConnection:
    """Connection to one IMAP account.

    Lifecycle: __init__ → connect() → login() → (work) → logout().
    Re-use a single instance across multiple commands; create a new one
    for each (account, label) pair the sync engine watches.
    """

    def __init__(self, *, host: str, port: int, username: str, password: str):
        self.host = host
        self.port = port
        self.username = username
        # Password held in a closure, not on `self`, so it doesn't survive
        # past login() and can't leak via repr/logging.
        self._login_callback = lambda c: c.login(username, password)
        self._client: Any | None = None

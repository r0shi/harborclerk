"""IMAP sync subsystem.

Headless engine that connects to user-owned IMAP accounts (Gmail, iCloud,
Fastmail, Yahoo, generic IMAP-with-app-password), watches one or more
labels per account, and populates `watched_messages` rows as mail arrives
or changes.

See spec docs/superpowers/specs/2026-05-04-email-ingestion-design.md.
"""

from harbor_clerk.mail.exceptions import (
    AuthError,
    IdleNotSupported,
    UidValidityChanged,
)
from harbor_clerk.mail.imap_client import IMAPConnection
from harbor_clerk.mail.labels import Folder, discover_folders

__all__ = [
    "AuthError",
    "Folder",
    "IMAPConnection",
    "IdleNotSupported",
    "UidValidityChanged",
    "discover_folders",
]

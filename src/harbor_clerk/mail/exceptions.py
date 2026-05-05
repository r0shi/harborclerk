"""Typed exceptions for the mail subsystem.

Distinct types let the sync state machine and the API layer make policy
decisions without parsing error strings.
"""

from __future__ import annotations


class MailError(Exception):
    """Base for all mail subsystem errors."""


class AuthError(MailError):
    """IMAP login failed — wrong password, account locked, or app passwords
    disabled. Sync engine marks the account `status='auth_error'` and stops
    polling until the operator reconnects."""


class IdleNotSupported(MailError):
    """The IMAP server doesn't advertise the IDLE capability. Sync falls
    back to 2-minute polling."""


class UidValidityChanged(MailError):
    """Server-side UIDVALIDITY changed since the last sync. Cursor is
    invalid; trigger a full rescan of the label."""

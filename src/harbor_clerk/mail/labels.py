# src/harbor_clerk/mail/labels.py
"""IMAP LIST parsing and folder/label classification.

A 'label' in Gmail's UI is exposed as an IMAP folder. We use the term
'folder' here to match the IMAP protocol; UI/spec language uses 'label'.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from harbor_clerk.mail.imap_client import IMAPConnection

# Set of IMAP flags that mark a folder as system-managed (not a user label).
# These come from RFC 6154 (SPECIAL-USE) and Gmail's XLIST extension.
_SYSTEM_FLAGS = frozenset(
    {
        r"\All",
        r"\Sent",
        r"\Drafts",
        r"\Trash",
        r"\Junk",
        r"\Spam",
        r"\Flagged",
        r"\Starred",
        r"\Important",
        r"\Archive",
        r"\Noselect",  # structural-only parents (e.g. "[Gmail]"); also filtered out below
    }
)

# IMAP LIST response line shape: `(flag1 flag2) "delim" "name"`
# The delimiter and name are double-quoted; flags are space-separated.
_LIST_LINE_RE = re.compile(
    rb'^\s*\(([^)]*)\)\s+"([^"]*)"\s+"?([^"]+)"?\s*$',
)


@dataclass(frozen=True)
class Folder:
    path: str
    flags: frozenset[str]
    delimiter: str

    @property
    def is_system(self) -> bool:
        """True if this folder is a system mailbox (INBOX, [Gmail]/*, SPECIAL-USE).

        User labels return False. The wizard greys these with a warning so
        the user has to deliberately pick (e.g.) [Gmail]/All Mail.
        """
        if self.path == "INBOX":
            return True
        if self.flags & (_SYSTEM_FLAGS - {r"\Noselect"}):
            return True
        # [Gmail]/Foo style system folders that don't carry SPECIAL-USE flags
        return self.path.startswith("[Gmail]") or self.path.startswith("[Google Mail]")


def _parse_list_line(line: bytes) -> Folder | None:
    """Parse one IMAP LIST response line into a Folder.

    Returns None if the line is the trailing `OK LIST completed` status,
    or doesn't match the LIST shape. Skips \\Noselect entries (structural
    parents like "[Gmail]" that can't be SELECTed and don't hold mail).
    """
    m = _LIST_LINE_RE.match(line)
    if m is None:
        return None  # status line or unparseable
    flags_blob, delim, name = m.groups()
    flags = frozenset(f.decode("utf-8") for f in flags_blob.split() if f)
    if r"\Noselect" in flags:
        return None
    return Folder(
        path=name.decode("utf-8"),
        flags=flags,
        delimiter=delim.decode("utf-8"),
    )


async def discover_folders(conn: IMAPConnection) -> list[Folder]:
    """Issue `LIST "" "*"` and return the parsed folder list.

    Caller must have already called `conn.connect()` and `conn.login()`.
    """
    result, lines = await conn.client.list('""', '"*"')
    if result != "OK":
        return []
    folders: list[Folder] = []
    for line in lines:
        f = _parse_list_line(line if isinstance(line, bytes) else str(line).encode())
        if f is not None:
            folders.append(f)
    return folders

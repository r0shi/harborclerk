"""Per-label sync state machine.

Stage 2 implements:
  - sync_label_initial: empty cursor → fetch all UIDs → populate watched_messages
  - sync_label_incremental: fetch UIDs > last_uid_seen → append to watched_messages
  - check_uidvalidity: detect server-side UIDVALIDITY change → trigger rescan

Documents are NOT yet created from these messages — that's Stage 3, which
will read freshly-inserted watched_messages rows and produce email +
attachment Documents via the parser.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.mail.cursor import LabelCursor, write_cursor
from harbor_clerk.mail.imap_client import IMAPConnection
from harbor_clerk.models import WatchedLabel, WatchedMessage

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SyncSummary:
    """What a sync invocation did."""

    fetched_count: int
    new_count: int
    duplicate_count: int


_MESSAGE_ID_RE = re.compile(rb"Message-I[Dd]:\s*(<[^>]+>)", re.IGNORECASE)


def _parse_uidvalidity(select_lines: list[bytes]) -> int | None:
    """Extract UIDVALIDITY from a SELECT response. Format:
    `* OK [UIDVALIDITY 12345] UIDs valid`."""
    for line in select_lines:
        m = re.search(rb"UIDVALIDITY\s+(\d+)", line)
        if m:
            return int(m.group(1))
    return None


def _parse_uid_list(search_lines: list[bytes]) -> list[int]:
    """Parse `* SEARCH 1 2 3` style output. The aioimaplib `uid_search`
    helper returns just the UID list as bytes, possibly empty."""
    uids: list[int] = []
    for line in search_lines:
        for tok in line.split():
            try:
                uids.append(int(tok))
            except ValueError:
                continue
    return uids


def _parse_fetch_response(fetch_lines: list[bytes]) -> dict[int, str]:
    """From the FETCH response, build {uid: message_id}.

    The response is multi-line with literal headers. We look for `UID N`
    in the structural lines and `Message-ID: <...>` in the header lines.
    Falls back to a synthesized hash when the Message-ID header is absent.
    """
    result: dict[int, str] = {}
    current_uid: int | None = None
    for line in fetch_lines:
        # Structural line: `1 (UID 1 BODY[HEADER...]`
        m = re.match(rb"^\s*\d+\s+\(UID\s+(\d+)", line)
        if m:
            current_uid = int(m.group(1))
            continue
        # Message-ID header line
        if current_uid is not None:
            mid_match = _MESSAGE_ID_RE.search(line)
            if mid_match:
                result[current_uid] = mid_match.group(1).decode("utf-8", errors="replace")
                # Don't reset current_uid — multi-line literals may continue
    return result


def _synthesize_message_id(uid: int, label_id) -> str:
    """Fallback Message-ID for messages with no header. Stable per-(label, uid)."""
    h = hashlib.sha256(f"{label_id}:{uid}".encode()).hexdigest()[:16]
    return f"<synthetic-{h}@harborclerk.local>"


async def sync_label_initial(
    session: AsyncSession,
    conn: IMAPConnection,
    label: WatchedLabel,
) -> SyncSummary:
    """Fetch all messages currently in the label and populate watched_messages.

    Caller must have already opened and authenticated `conn`. This function
    only reads from IMAP and writes to Postgres — no commit. Caller commits.
    """
    select_result, select_lines = await conn.client.select(label.label_path)
    if select_result != "OK":
        logger.warning("SELECT %r failed: %r", label.label_path, select_lines)
        return SyncSummary(fetched_count=0, new_count=0, duplicate_count=0)

    uidvalidity = _parse_uidvalidity(select_lines)

    # Find all UIDs in the label
    search_result, search_lines = await conn.client.uid_search("ALL")
    if search_result != "OK":
        return SyncSummary(0, 0, 0)
    uids = _parse_uid_list(search_lines)
    if not uids:
        await write_cursor(session, label.label_id, LabelCursor(last_uid_seen=0, uidvalidity=uidvalidity))
        return SyncSummary(0, 0, 0)

    # Fetch Message-ID for each
    uid_set = ",".join(str(u) for u in uids)
    fetch_result, fetch_lines = await conn.client.uid("FETCH", uid_set, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
    if fetch_result != "OK":
        return SyncSummary(0, 0, 0)
    uid_to_mid = _parse_fetch_response(fetch_lines)

    # Insert watched_messages rows. eml_sha256 is a placeholder here
    # (Stage 3 fills it when the .eml is fetched and parsed). Status is 'active'.
    new_count = 0
    duplicate_count = 0
    for uid in uids:
        message_id = uid_to_mid.get(uid) or _synthesize_message_id(uid, label.label_id)
        # Dedup check: same (label_id, message_id) means we've seen this before.
        existing = (
            await session.execute(
                select(WatchedMessage).where(
                    WatchedMessage.label_id == label.label_id,
                    WatchedMessage.message_id == message_id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            # Relabel: user removed then re-applied the label. The row sat
            # at status='unlabeled' (set by detect_unlabeled_messages); flip
            # it back to 'active' so Stage 3's restore_documents_for_relabeled
            # can revive the associated Documents. Without this flip, those
            # Documents stay in status='deleted' forever — re-labeling never
            # restores them.
            if existing.status == "unlabeled":
                existing.status = "active"
                existing.unlabeled_at = None
            duplicate_count += 1
            continue
        # Placeholder eml_sha256 — Stage 3 fills with the actual SHA when fetching the .eml.
        placeholder_sha = hashlib.sha256(f"placeholder:{label.label_id}:{uid}".encode()).digest()
        msg = WatchedMessage(
            label_id=label.label_id,
            message_id=message_id,
            imap_uid=uid,
            eml_sha256=placeholder_sha,
            status="active",
        )
        session.add(msg)
        new_count += 1

    await session.flush()

    # Advance cursor to highest UID seen
    highest_uid = max(uids)
    await write_cursor(session, label.label_id, LabelCursor(last_uid_seen=highest_uid, uidvalidity=uidvalidity))

    return SyncSummary(fetched_count=len(uids), new_count=new_count, duplicate_count=duplicate_count)


async def sync_label_incremental(
    session: AsyncSession,
    conn: IMAPConnection,
    label: WatchedLabel,
) -> SyncSummary:
    """Fetch messages with UID > last_uid_seen and append to watched_messages.

    Caller must have already authenticated. Caller commits.
    """
    select_result, select_lines = await conn.client.select(label.label_path)
    if select_result != "OK":
        return SyncSummary(0, 0, 0)

    uidvalidity = _parse_uidvalidity(select_lines)
    if label.uidvalidity is not None and uidvalidity != label.uidvalidity:
        # UIDVALIDITY changed — caller must trigger a full rescan instead.
        # We refuse to advance the cursor in this case.
        from harbor_clerk.mail.exceptions import UidValidityChanged

        raise UidValidityChanged(f"label {label.label_path}: uidvalidity {label.uidvalidity} → {uidvalidity}")

    # Search for UIDs strictly greater than last_uid_seen.
    next_uid = label.last_uid_seen + 1
    search_query = f"UID {next_uid}:*"
    search_result, search_lines = await conn.client.uid_search(search_query)
    if search_result != "OK":
        return SyncSummary(0, 0, 0)

    uids = _parse_uid_list(search_lines)
    # IMAP `UID N:*` always returns at least UIDNEXT-1 even if no messages
    # match; filter those out.
    uids = [u for u in uids if u >= next_uid]
    if not uids:
        return SyncSummary(0, 0, 0)

    uid_set = ",".join(str(u) for u in uids)
    fetch_result, fetch_lines = await conn.client.uid("FETCH", uid_set, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
    if fetch_result != "OK":
        return SyncSummary(0, 0, 0)
    uid_to_mid = _parse_fetch_response(fetch_lines)

    new_count = 0
    duplicate_count = 0
    for uid in uids:
        message_id = uid_to_mid.get(uid) or _synthesize_message_id(uid, label.label_id)
        existing = (
            await session.execute(
                select(WatchedMessage).where(
                    WatchedMessage.label_id == label.label_id,
                    WatchedMessage.message_id == message_id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            # Relabel: see sync_label_initial for the rationale. Flip
            # status='unlabeled' rows back to 'active' so Stage 3's
            # restore_documents_for_relabeled can revive their Documents.
            if existing.status == "unlabeled":
                existing.status = "active"
                existing.unlabeled_at = None
            duplicate_count += 1
            continue
        placeholder_sha = hashlib.sha256(f"placeholder:{label.label_id}:{uid}".encode()).digest()
        msg = WatchedMessage(
            label_id=label.label_id,
            message_id=message_id,
            imap_uid=uid,
            eml_sha256=placeholder_sha,
            status="active",
        )
        session.add(msg)
        new_count += 1

    await session.flush()

    highest_uid = max(uids)
    await write_cursor(session, label.label_id, LabelCursor(last_uid_seen=highest_uid, uidvalidity=uidvalidity))

    return SyncSummary(fetched_count=len(uids), new_count=new_count, duplicate_count=duplicate_count)


async def handle_uidvalidity_change(
    session: AsyncSession,
    conn: IMAPConnection,
    label: WatchedLabel,
) -> SyncSummary:
    """Drop all watched_messages for the label, reset cursor, run initial sync.

    Called when `sync_label_incremental` raises UidValidityChanged. Stage 3
    will react to the deleted watched_messages by soft-deleting their
    associated email Documents (via the existing 30-day reaper code path).

    Caller must have already authenticated. Caller commits.
    """
    logger.warning(
        "label %s (%s): UIDVALIDITY changed; dropping %d watched_messages and rescanning",
        label.label_id,
        label.label_path,
        label.last_uid_seen,
    )
    await session.execute(delete(WatchedMessage).where(WatchedMessage.label_id == label.label_id))
    label.uidvalidity = None
    label.last_uid_seen = 0
    await session.flush()

    return await sync_label_initial(session, conn, label)

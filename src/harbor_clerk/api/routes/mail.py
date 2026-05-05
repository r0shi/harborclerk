"""REST endpoints for /api/mail/* — admin-only.

Stage 2 surface: account CRUD + connection-test (Task 6), watched-label
CRUD (Task 7) + manual rescan (Task 16). Used by the API layer (humans
via auth) and by Stage 4's UI.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.api.deps import Principal, require_admin
from harbor_clerk.api.schemas.mail import (
    FolderInfo,
    MailAccountCreate,
    MailAccountResponse,
    TestConnectionResponse,
)
from harbor_clerk.db import get_session
from harbor_clerk.mail import AuthError, IMAPConnection, discover_folders
from harbor_clerk.models import MailAccount
from harbor_clerk.secrets import get_cipher

logger = logging.getLogger(__name__)
router = APIRouter(tags=["mail"])


def _account_to_response(a: MailAccount) -> MailAccountResponse:
    return MailAccountResponse(
        account_id=a.account_id,
        display_name=a.display_name,
        provider=a.provider,  # type: ignore[arg-type]
        imap_host=a.imap_host,
        imap_port=a.imap_port,
        imap_username=a.imap_username,
        status=a.status,  # type: ignore[arg-type]
        last_error=a.last_error,
        last_connected_at=a.last_connected_at,
        created_at=a.created_at,
    )


@router.post(
    "/mail/accounts",
    response_model=MailAccountResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_mail_account(
    body: MailAccountCreate,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> MailAccountResponse:
    """Create a mail account. Encrypts the app password via the
    process-wide Cipher before persisting. Does NOT test the connection
    here — call `POST /api/mail/accounts/{id}/test` separately."""
    cipher = get_cipher()
    ciphertext, fingerprint = cipher.encrypt(body.app_password.get_secret_value().encode())

    account = MailAccount(
        display_name=body.display_name,
        provider=body.provider,
        imap_host=body.imap_host,
        imap_port=body.imap_port,
        imap_username=body.imap_username,
        app_password_ciphertext=ciphertext,
        key_fingerprint=fingerprint,
    )
    session.add(account)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"mail account ({body.imap_host}, {body.imap_username}) already exists",
        )
    await session.commit()
    return _account_to_response(account)


@router.get("/mail/accounts", response_model=list[MailAccountResponse])
async def list_mail_accounts(
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> list[MailAccountResponse]:
    """List all mail accounts. Never returns secrets."""
    rows = (await session.execute(select(MailAccount).order_by(MailAccount.created_at))).scalars().all()
    return [_account_to_response(a) for a in rows]


@router.delete("/mail/accounts/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_mail_account(
    account_id: UUID,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Delete a mail account. Cascades to watched_labels and watched_messages
    (Documents created from those messages are NOT deleted — they remain in
    the corpus). The Stage 2 sync engine will react to the LISTEN/NOTIFY
    and stop polling immediately."""
    account = (
        await session.execute(select(MailAccount).where(MailAccount.account_id == account_id))
    ).scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="mail account not found")
    await session.delete(account)
    await session.commit()


@router.post(
    "/mail/accounts/{account_id}/test",
    response_model=TestConnectionResponse,
)
async def test_mail_account(
    account_id: UUID,
    principal: Principal = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> TestConnectionResponse:
    """Open an IMAP connection with the stored credentials and run LIST.

    Returns the discovered folder list on success. On auth failure,
    updates the account row to status='auth_error' so the UI knows
    re-entry is needed.
    """
    account = (
        await session.execute(select(MailAccount).where(MailAccount.account_id == account_id))
    ).scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="mail account not found")

    cipher = get_cipher()
    try:
        password = cipher.decrypt(account.app_password_ciphertext, account.key_fingerprint).decode()
    except Exception as exc:
        logger.warning("decrypt failed for account %s: %s", account_id, exc)
        account.status = "key_mismatch"
        account.last_error = "key fingerprint does not match active master key"
        await session.commit()
        return TestConnectionResponse(success=False, error=account.last_error)

    conn = IMAPConnection(
        host=account.imap_host,
        port=account.imap_port,
        username=account.imap_username,
        password=password,
    )
    try:
        await conn.connect()
        await conn.login()
        folders = await discover_folders(conn)
        await conn.logout()
    except AuthError as exc:
        account.status = "auth_error"
        account.last_error = str(exc)
        await session.commit()
        return TestConnectionResponse(success=False, error=str(exc))
    except Exception as exc:
        # Network errors, timeouts, etc. — don't change account status
        # (the account isn't broken, just unreachable right now).
        try:
            await conn.logout()
        except Exception:
            pass
        return TestConnectionResponse(success=False, error=f"connection failed: {exc}")

    account.status = "active"
    account.last_error = None
    account.last_connected_at = datetime.now(UTC)
    await session.commit()

    return TestConnectionResponse(
        success=True,
        folders=[
            FolderInfo(
                path=f.path,
                display_name=f.path,
                is_system=f.is_system,
                has_children=r"\HasChildren" in f.flags,
            )
            for f in folders
        ],
    )

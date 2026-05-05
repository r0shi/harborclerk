"""REST endpoints for /api/mail/* — admin-only.

Stage 2 surface: account CRUD + connection-test (Task 6), watched-label
CRUD (Task 7) + manual rescan (Task 16). Used by the API layer (humans
via auth) and by Stage 4's UI.
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.api.deps import Principal, require_admin
from harbor_clerk.api.schemas.mail import (
    MailAccountCreate,
    MailAccountResponse,
)
from harbor_clerk.db import get_session
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

"""OAuth 2.1 endpoints — registration, authorize, token, revoke, well-known metadata."""

import logging
import uuid
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from harbor_clerk.auth import verify_password
from harbor_clerk.config import get_settings
from harbor_clerk.db import get_session
from harbor_clerk.models.user import User
from harbor_clerk.oauth import (
    create_authorization_code,
    get_client,
    issue_tokens,
    refresh_access_token,
    register_client,
    revoke_token,
    verify_client_secret,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["oauth"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_public_url() -> str:
    """Return settings.public_url or raise 503 if not configured."""
    settings = get_settings()
    if not settings.public_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="public_url is not configured",
        )
    return settings.public_url.rstrip("/")


def _consent_html(
    client_name: str,
    scope: str,
    client_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
    code_challenge_method: str,
    error: str | None = None,
) -> str:
    """Render the server-side consent / login page."""
    error_block = ""
    if error:
        error_block = f'<div class="error">{error}</div>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Authorize — Harbor Clerk</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         background: #f5f5f5; margin: 0; display: flex; justify-content: center; align-items: center;
         min-height: 100vh; }}
  .card {{ background: #fff; border-radius: 12px; box-shadow: 0 2px 12px rgba(0,0,0,.1);
           padding: 2rem; max-width: 420px; width: 100%; }}
  h1 {{ font-size: 1.25rem; margin: 0 0 .25rem; color: #111; }}
  .subtitle {{ color: #666; font-size: .875rem; margin-bottom: 1.5rem; }}
  .client {{ background: #f0f4ff; border-radius: 8px; padding: .75rem 1rem; margin-bottom: 1.25rem; }}
  .client strong {{ color: #333; }}
  .scope {{ color: #555; font-size: .85rem; }}
  label {{ display: block; font-size: .875rem; color: #333; margin-bottom: .25rem; font-weight: 500; }}
  input[type=email], input[type=password] {{ width: 100%; padding: .5rem .75rem; border: 1px solid #ccc;
         border-radius: 6px; font-size: .95rem; margin-bottom: .75rem; box-sizing: border-box; }}
  .actions {{ display: flex; gap: .75rem; margin-top: 1rem; }}
  button {{ flex: 1; padding: .6rem; border: none; border-radius: 6px; font-size: .95rem;
           cursor: pointer; font-weight: 500; }}
  .btn-allow {{ background: #2563eb; color: #fff; }}
  .btn-allow:hover {{ background: #1d4ed8; }}
  .btn-deny {{ background: #e5e7eb; color: #333; }}
  .btn-deny:hover {{ background: #d1d5db; }}
  .error {{ background: #fef2f2; color: #b91c1c; border: 1px solid #fecaca; border-radius: 6px;
           padding: .5rem .75rem; margin-bottom: 1rem; font-size: .875rem; }}
</style>
</head>
<body>
<div class="card">
  <h1>Harbor Clerk</h1>
  <p class="subtitle">An application is requesting access to your account.</p>
  <div class="client">
    <strong>{client_name or "Unknown application"}</strong><br>
    <span class="scope">Permissions: {scope}</span>
  </div>
  {error_block}
  <form method="post" action="/oauth/authorize">
    <input type="hidden" name="client_id" value="{client_id}">
    <input type="hidden" name="redirect_uri" value="{redirect_uri}">
    <input type="hidden" name="state" value="{state}">
    <input type="hidden" name="scope" value="{scope}">
    <input type="hidden" name="code_challenge" value="{code_challenge}">
    <input type="hidden" name="code_challenge_method" value="{code_challenge_method}">
    <label for="email">Email</label>
    <input type="email" id="email" name="email" required autocomplete="email">
    <label for="password">Password</label>
    <input type="password" id="password" name="password" required autocomplete="current-password">
    <div class="actions">
      <button type="submit" name="action" value="deny" class="btn-deny">Deny</button>
      <button type="submit" name="action" value="authorize" class="btn-allow">Authorize</button>
    </div>
  </form>
</div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Well-known metadata
# ---------------------------------------------------------------------------


@router.get("/.well-known/oauth-protected-resource")
async def oauth_protected_resource():
    public_url = _require_public_url()
    return {"resource": public_url, "authorization_servers": [public_url]}


@router.get("/.well-known/oauth-authorization-server")
async def oauth_authorization_server():
    public_url = _require_public_url()
    return {
        "issuer": public_url,
        "authorization_endpoint": f"{public_url}/oauth/authorize",
        "token_endpoint": f"{public_url}/oauth/token",
        "registration_endpoint": f"{public_url}/oauth/register",
        "revocation_endpoint": f"{public_url}/oauth/revoke",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_post"],
        "scopes_supported": ["mcp"],
        "service_documentation": f"{public_url}/docs",
    }


# ---------------------------------------------------------------------------
# Dynamic client registration
# ---------------------------------------------------------------------------


@router.post("/oauth/register", status_code=status.HTTP_201_CREATED)
async def oauth_register(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    body = await request.json()
    redirect_uris = body.get("redirect_uris")
    if not redirect_uris or not isinstance(redirect_uris, list):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="redirect_uris is required and must be a non-empty list",
        )

    client, raw_secret = await register_client(
        session,
        redirect_uris=redirect_uris,
        client_name=body.get("client_name"),
        client_uri=body.get("client_uri"),
        grant_types=body.get("grant_types"),
        response_types=body.get("response_types"),
        scope=body.get("scope", "mcp"),
    )
    await session.commit()

    return JSONResponse(
        status_code=201,
        content={
            "client_id": str(client.client_id),
            "client_secret": raw_secret,
            "client_name": client.client_name,
            "redirect_uris": client.redirect_uris,
            "grant_types": client.grant_types,
            "response_types": client.response_types,
            "scope": client.scope,
            "client_id_issued_at": int(client.created_at.timestamp()) if client.created_at else 0,
        },
    )


# ---------------------------------------------------------------------------
# Authorization endpoint
# ---------------------------------------------------------------------------


@router.get("/oauth/authorize", response_class=HTMLResponse)
async def oauth_authorize_get(
    response_type: str = Query(...),
    client_id: str = Query(...),
    redirect_uri: str = Query(...),
    state: str = Query(""),
    scope: str = Query("mcp"),
    code_challenge: str = Query(...),
    code_challenge_method: str = Query("S256"),
    session: AsyncSession = Depends(get_session),
):
    # Validate client
    try:
        cid = uuid.UUID(client_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid client_id")

    client = await get_client(session, cid)
    if client is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unknown client_id")
    if redirect_uri not in client.redirect_uris:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="redirect_uri not registered")
    if response_type != "code":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported response_type")

    return HTMLResponse(
        _consent_html(
            client_name=client.client_name or "",
            scope=scope,
            client_id=client_id,
            redirect_uri=redirect_uri,
            state=state,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
        )
    )


@router.post("/oauth/authorize")
async def oauth_authorize_post(
    action: str = Form(...),
    client_id: str = Form(...),
    redirect_uri: str = Form(...),
    state: str = Form(""),
    scope: str = Form("mcp"),
    code_challenge: str = Form(...),
    code_challenge_method: str = Form("S256"),
    email: str = Form(""),
    password: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    # Validate client
    try:
        cid = uuid.UUID(client_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid client_id")

    client = await get_client(session, cid)
    if client is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unknown client_id")
    if redirect_uri not in client.redirect_uris:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="redirect_uri not registered")

    # Deny → redirect with error
    if action == "deny":
        params = urlencode({"error": "access_denied", "state": state})
        return RedirectResponse(url=f"{redirect_uri}?{params}", status_code=302)

    # Authorize → authenticate user
    result = await session.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if user is None or not verify_password(password, user.password_hash):
        return HTMLResponse(
            _consent_html(
                client_name=client.client_name or "",
                scope=scope,
                client_id=client_id,
                redirect_uri=redirect_uri,
                state=state,
                code_challenge=code_challenge,
                code_challenge_method=code_challenge_method,
                error="Invalid email or password",
            ),
            status_code=200,
        )

    if user.role.value != "admin":
        return HTMLResponse(
            _consent_html(
                client_name=client.client_name or "",
                scope=scope,
                client_id=client_id,
                redirect_uri=redirect_uri,
                state=state,
                code_challenge=code_challenge,
                code_challenge_method=code_challenge_method,
                error="Only admin users can authorize OAuth clients",
            ),
            status_code=200,
        )

    # Create authorization code
    raw_code = await create_authorization_code(
        session,
        client_id=cid,
        user_id=user.user_id,
        redirect_uri=redirect_uri,
        scope=scope,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
    )
    await session.commit()

    params = urlencode({"code": raw_code, "state": state})
    return RedirectResponse(url=f"{redirect_uri}?{params}", status_code=302)


# ---------------------------------------------------------------------------
# Token endpoint
# ---------------------------------------------------------------------------


@router.post("/oauth/token")
async def oauth_token(
    grant_type: str = Form(...),
    code: str | None = Form(None),
    redirect_uri: str | None = Form(None),
    code_verifier: str | None = Form(None),
    refresh_token: str | None = Form(None),
    client_id: str = Form(...),
    client_secret: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    # Authenticate client
    try:
        cid = uuid.UUID(client_id)
    except ValueError:
        return JSONResponse(status_code=400, content={"error": "invalid_client"})

    client = await get_client(session, cid)
    if client is None or not verify_client_secret(client_secret, client):
        return JSONResponse(status_code=401, content={"error": "invalid_client"})

    if grant_type == "authorization_code":
        if not code or not redirect_uri or not code_verifier:
            return JSONResponse(status_code=400, content={"error": "invalid_request"})

        from harbor_clerk.oauth import validate_authorization_code

        auth_code = await validate_authorization_code(
            session,
            code=code,
            client_id=cid,
            redirect_uri=redirect_uri,
            code_verifier=code_verifier,
        )
        if auth_code is None:
            return JSONResponse(status_code=400, content={"error": "invalid_grant"})

        tokens = await issue_tokens(
            session,
            client_id=cid,
            user_id=auth_code.user_id,
            scope=auth_code.scope,
        )
        await session.commit()
        return tokens

    elif grant_type == "refresh_token":
        if not refresh_token:
            return JSONResponse(status_code=400, content={"error": "invalid_request"})

        tokens = await refresh_access_token(
            session,
            refresh_token=refresh_token,
            client_id=cid,
        )
        if tokens is None:
            return JSONResponse(status_code=400, content={"error": "invalid_grant"})

        await session.commit()
        return tokens

    else:
        return JSONResponse(status_code=400, content={"error": "unsupported_grant_type"})


# ---------------------------------------------------------------------------
# Revocation (RFC 7009)
# ---------------------------------------------------------------------------


@router.post("/oauth/revoke")
async def oauth_revoke(
    token: str = Form(...),
    token_type_hint: str | None = Form(None),
    session: AsyncSession = Depends(get_session),
):
    await revoke_token(session, token=token, token_type_hint=token_type_hint)
    await session.commit()
    # Always 200 per RFC 7009
    return JSONResponse(status_code=200, content={})

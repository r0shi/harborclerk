# Cloud LLM Integration Design

**Date:** 2026-03-12
**Status:** Approved

## Goal

Enable ChatGPT (and other MCP clients like claude.ai web) to connect to Harbor Clerk's knowledge base via OAuth 2.1, and provide an Integrations page with connection guides for all supported clients.

## Context

Harbor Clerk already has an MCP server with 16 knowledge base tools and API key auth. Claude Desktop/Code and Gemini CLI can connect today using bearer token auth. However, ChatGPT and claude.ai web require OAuth 2.1 with PKCE and Dynamic Client Registration per the MCP authorization spec.

The big 3 consumer plans ($20/mo Claude Pro, ChatGPT Plus, Gemini Advanced) do not include API access — they are separate products with separate billing. This means "MCP in" (cloud apps connect to Harbor Clerk) is the primary path for the target audience, not "API out" (Harbor Clerk calls cloud APIs).

## Architecture

### OAuth 2.1 Authorization Server

Built into FastAPI using **Authlib >= 1.6.0** (Apache 2.0 license). Authlib provides the core OAuth grants, PKCE, and token handling. MCP-specific metadata endpoints are custom (simple JSON responses).

### Flow: Connecting ChatGPT

1. User pastes Harbor Clerk's public URL into ChatGPT's MCP connector settings
2. ChatGPT discovers `/.well-known/oauth-protected-resource` -> authorization server metadata URL
3. ChatGPT fetches `/.well-known/oauth-authorization-server` -> registration, authorization, token endpoints
4. ChatGPT calls dynamic client registration endpoint -> gets `client_id`
5. ChatGPT redirects user to Harbor Clerk's authorization page (browser popup)
6. User logs in with Harbor Clerk email + password, clicks "Authorize"
7. Harbor Clerk redirects back to ChatGPT with an authorization code
8. ChatGPT exchanges code for access + refresh tokens (PKCE validated)
9. ChatGPT uses access token on `POST /mcp` for tool calls
10. Access token expires (1hr) -> ChatGPT refreshes automatically
11. Refresh token expires (configurable, default 90 days) -> user re-authorizes

### Existing Auth Unchanged

- Human users: email + password -> JWT access token + refresh cookie (PyJWT, unchanged)
- API keys: admin-created, read-only, bearer token (unchanged)
- MCP middleware: updated to accept OAuth access tokens as a third auth path

## Database Schema

Three new tables (Alembic migration):

### oauth_clients

| Column | Type | Notes |
|---|---|---|
| client_id | UUID | PK |
| client_secret_hash | text | SHA-256 hashed |
| client_name | text | From registration metadata |
| redirect_uris | JSONB | Array of allowed redirect URIs |
| grant_types | JSONB | e.g., `["authorization_code"]` |
| response_types | JSONB | e.g., `["code"]` |
| scope | text | e.g., `"mcp"` |
| client_uri | text | Client homepage link |
| created_at | timestamp | |

### oauth_codes

| Column | Type | Notes |
|---|---|---|
| code_id | UUID | PK |
| code_hash | text | SHA-256 hashed |
| client_id | UUID | FK -> oauth_clients |
| user_id | UUID | FK -> users |
| redirect_uri | text | |
| scope | text | |
| code_challenge | text | PKCE |
| code_challenge_method | text | `S256` |
| expires_at | timestamp | ~10 minutes |
| used | boolean | Default false |
| created_at | timestamp | |

### oauth_tokens

| Column | Type | Notes |
|---|---|---|
| token_id | UUID | PK |
| access_token_hash | text | SHA-256 hashed |
| refresh_token_hash | text | SHA-256 hashed |
| client_id | UUID | FK -> oauth_clients |
| user_id | UUID | FK -> users |
| scope | text | |
| access_token_expires_at | timestamp | |
| refresh_token_expires_at | timestamp | |
| revoked | boolean | Default false |
| created_at | timestamp | |
| last_used_at | timestamp | |

All secrets stored as SHA-256 hashes (same pattern as existing API keys).

## Endpoints

### Well-Known Metadata (no auth)

- `GET /.well-known/oauth-protected-resource` — resource descriptor
- `GET /.well-known/oauth-authorization-server` — server metadata (issuer, endpoints, supported grants/PKCE methods)

### OAuth (no auth on register; user login on authorize)

- `POST /oauth/register` — Dynamic Client Registration (rate-limited: 10/hr per IP)
- `GET /oauth/authorize` — Authorization endpoint (server-rendered HTML consent page)
- `POST /oauth/token` — Token endpoint (authorization_code + refresh_token grants)
- `POST /oauth/revoke` — Token revocation

### Consent Page

Server-rendered HTML with inline CSS (not React SPA). Branded with Harbor Clerk logo. Shows login form if no active session, then consent prompt: "ChatGPT wants to access your knowledge base. Allow?" with Authorize/Deny buttons. CSRF-protected.

## Integrations Page

React page at `/integrations`, admin-only, top nav next to System Settings.

### Layout

**1. Active Connections** (top)
- Table: client name, first authorized, last used, status (active/expired)
- Revoke button per client
- Empty state: "No external AI tools connected yet"

**2. Connect ChatGPT** (card)
- Step-by-step: make HC publicly accessible -> paste URL in ChatGPT MCP settings -> authorize
- Status indicator if already connected

**3. Connect Claude Desktop/Code** (card)
- Create API key -> copyable config JSON snippet

**4. Connect Gemini CLI** (card)
- Create API key -> copyable command

## Configuration

- **Public URL** (text): admin enters their public HTTPS URL. Required for OAuth metadata. OAuth endpoints return 503 if not set.
- **OAuth refresh token lifetime** (dropdown): 30 / 60 / 90 / 120 / 365 days, default 90. Stored in config.json (macOS) or `OAUTH_REFRESH_TOKEN_DAYS` env var (Docker).
- **Access token lifetime**: 1 hour, hardcoded.

## Security

- All secrets hashed (SHA-256): client_secret, auth codes, access tokens, refresh tokens
- PKCE required on all authorization code grants (OAuth 2.1 mandate)
- Refresh token rotation on each refresh (old token invalidated)
- OAuth scope limited to `mcp` (read-only knowledge base access)
- Rate limiting on dynamic client registration (10/hr per IP)
- CSRF protection on consent page
- Public URL required — OAuth endpoints return 503 if unconfigured
- Expired code/token cleanup (background or on-demand)
- Admin-only: only admin users can authorize OAuth clients

## Dependencies

- `authlib>=1.6.0` — OAuth 2.1 authorization server (Apache 2.0 license)

## Files

### New

- `src/harbor_clerk/oauth.py` — Authlib server setup, grant types, token/client models
- `src/harbor_clerk/api/routes/oauth.py` — FastAPI routes for `/oauth/*` and `/.well-known/*`
- `src/harbor_clerk/api/templates/authorize.html` — server-rendered consent page
- `frontend/src/pages/IntegrationsPage.tsx` — Integrations UI
- Alembic migration for oauth tables

### Modified

- `src/harbor_clerk/mcp_server.py` — MCPAuthMiddleware accepts OAuth tokens
- `src/harbor_clerk/config.py` — new settings (public_url, oauth_refresh_token_days)
- `src/harbor_clerk/api/app.py` — mount OAuth routes
- `frontend/src/App.tsx` — add Integrations route + nav tab
- `pyproject.toml` — add authlib dependency

### Unchanged

- Existing JWT auth flow (PyJWT)
- API key management
- Chat/research/search functionality
- MCP tool implementations
- macOS native apps

## Future Work (not in this scope)

- "API out" — calling cloud LLM APIs from Harbor Clerk's chat UI (Gemini free tier is the easiest first target)
- Tunnel management UI — integrated Cloudflare Tunnel setup in Preferences
- Additional OAuth clients beyond ChatGPT/claude.ai

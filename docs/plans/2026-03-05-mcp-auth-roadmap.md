# MCP Auth Roadmap

## Current: URL-Embedded Token (v0.5.x)

PR #66 adds `/t/<api_key>` path-based auth for MCP clients that cannot send custom `Authorization` headers (Claude.ai, ChatGPT, Gemini web UIs). Users combine their API key with a tunnel URL and paste it into the connector settings.

**Tunnel options for exposing Harbor Clerk to cloud AI:**
- Cloudflare Quick Tunnel: `cloudflared tunnel --url http://localhost:8000` (no account needed)
- Tailscale Funnel: `tailscale funnel 8000` (requires Tailscale)
- Cloudflare Named Tunnel: persistent subdomain, requires free Cloudflare account

## Planned: OAuth 2.1 (Authlib)

MCP spec defines OAuth 2.1 as the standard auth mechanism. Full support requires:

1. **Protected Resource Metadata** (RFC 9728) — `GET /.well-known/oauth-protected-resource`
2. **Authorization Server Metadata** — `GET /.well-known/oauth-authorization-metadata`
3. **Dynamic Client Registration** (RFC 7591) — `POST /oauth/register`
4. **Authorization endpoint** — `GET /oauth/authorize` (consent screen)
5. **Token endpoint** — `POST /oauth/token` (PKCE required)

**Recommended approach:** Use [Authlib](https://authlib.org/) to implement the OAuth 2.1 server. Authlib provides Flask/Django/Starlette integrations and handles the RFC complexity. Our existing user/password auth stays; OAuth adds a new grant flow for MCP clients.

**Scope:** ~2-3 days of work. The authorization page is a simple consent screen ("Allow <client> to search your knowledge base?"). Token storage reuses our existing `api_keys` table pattern.

**Trigger:** Implement when a major MCP client (Claude, ChatGPT) adds native OAuth support for custom MCP servers, making the URL-token approach unnecessary.

## Possible Future: Passkeys / WebAuthn

Passkeys would improve the human login experience (no password to remember). Not currently planned — the target audience (small offices) is well-served by email/password, and passkeys add significant implementation complexity (WebAuthn ceremony, credential storage, fallback flows).

Could revisit if Apple pushes passkey-only auth patterns or if users request it.

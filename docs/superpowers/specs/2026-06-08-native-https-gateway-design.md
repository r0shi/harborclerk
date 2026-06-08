# Native HTTPS Gateway - Design Spec

**Date:** 2026-06-08
**Status:** Implemented in the native HTTPS gateway PR; manual certificate-file support is included; ACME/Let's Encrypt and app-over-HTTPS remain deferred
**Scope:** macOS native HTTPS parity for MCP, CLI, and local agent harness setup. Includes configurable hostname, bind addresses, self-signed/default certificates, custom certificate files, and Tailscale bind detection. Excludes ACME/Let's Encrypt automation and loading Harbor Clerk.app itself over HTTPS.

## Overview

Docker already presents Harbor Clerk through a Caddy HTTPS gateway at
`https://localhost`, while the macOS native app currently serves the API and
SPA directly over `http://localhost:<apiPort>`. That mismatch leaks into the
Integrations page, CLI defaults, OpenClaw setup, and cloud/local connector
messaging.

Native HTTPS should be a release gate because Harbor Clerk is explicitly
positioned as a tool surface for agentic frameworks and cloud LLM connectors.
Operators should not have to reason about one local URL shape for Docker, a
different one for macOS, and a third one for public cloud connectors.

## Goals

- Add a managed macOS HTTPS gateway that mirrors the Docker Caddy gateway.
- Keep the FastAPI app bound to an internal loopback HTTP port.
- Give local MCP clients a stable HTTPS URL on macOS.
- Make CLI/OpenClaw/Codex/Claude Code setup copy consistent across Docker and
  macOS.
- Keep cloud connector guidance explicit: cloud clients still require a public
  URL with a certificate trusted by the provider.
- Prepare the architecture for manual real hostname/certificate configuration.
- Surface gateway health, cert mode, and externally reachable URL clearly in
  Preferences, Status, and Integrations.

## Non-goals

- No ACME or Let's Encrypt automation in the initial release gate.
- No privileged port 443 default on macOS.
- No silent system-trust modification without user-visible consent.
- No public exposure by default.
- No redesign of MCP auth, API keys, or OAuth.
- No replacing FastAPI/uvicorn for this scope.
- No external exposure of the web app, setup/status API, or OAuth endpoints
  through the native gateway in this release.

## Current behavior

Docker:

- Caddy listens on port 443.
- Caddy uses an internal/self-signed local cert for `localhost`.
- Caddy proxies `/`, `/api/*`, `/mcp/*`, and `/t/*` to the app container.

macOS native before this PR:

- Harbor Clerk Server starts the API on `http://127.0.0.1:<apiPort>`.
- Harbor Clerk.app loads `http://localhost:<apiPort>` in WKWebView.
- The CLI shim bakes in `http://localhost:<apiPort>`.
- The generic CLI default is `https://localhost`, which currently matches
  Docker better than macOS.
- Public URL remains an operator-configured integration setting used by OAuth
  and cloud-facing MCP snippets.

## Recommended architecture

Add a new managed native gateway service, likely backed by a bundled Caddy
binary:

```text
Local clients
  -> https://localhost:<gatewayPort>
  -> Caddy native gateway
  -> http://127.0.0.1:<apiPort>
  -> FastAPI / React SPA / MCP
```

Default ports:

- Keep `apiPort` at `8100` for the internal app listener.
- Add `gatewayPort`, defaulting to a non-privileged port such as `8443`.
- Do not use `443` by default because it needs privilege, may conflict with
  other local services, and complicates install/uninstall.

Default bind behavior:

- Gateway binds to loopback only by default.
- FastAPI remains bound to `127.0.0.1`.
- Preferences let the operator choose loopback, all interfaces, or a detected
  Tailscale address for the gateway.
- When the gateway binds beyond loopback, Caddy proxies only `/mcp*` and `/t*`
  to FastAPI and returns 404 for everything else.
- This preserves remote/local agent MCP while keeping the SPA, setup/status
  endpoints, and OAuth off the external native listener.

Authentication/exposure audit:

- `/mcp` and `/t` are the intended remote-capable surfaces and require API key
  auth through the MCP middleware/token path.
- Static SPA routes, setup-status, ping/health, login/logout/refresh, and OAuth
  discovery/authorization/token routes are not appropriate to expose via the
  quick native gateway remote-bind control.
- Public OAuth/cloud connector hosting should remain a separate trusted-public
  URL design, not an accidental consequence of binding the local gateway to
  `0.0.0.0`.

Why Caddy:

- Already used in Docker, so config and behavior converge.
- Handles TLS and reverse proxying cleanly.
- Makes later manual cert/hostname support mostly a config problem.
- Avoids baking TLS ownership into FastAPI or the Swift app.

Rejected alternatives:

- Uvicorn TLS directly: lower initial code, but worse long-term parity and
  real-certificate support.
- A custom Swift reverse proxy: unnecessary complexity and more security
  surface.
- Requiring users to install their own reverse proxy: not acceptable for the
  Mac-native product promise.

## Certificate modes

### Mode 1: Local self-signed or local CA certificate

Release-gate default for macOS parity.

Expected URL:

```text
https://localhost:8443/t/YOUR_API_KEY
```

Requirements:

- Generate or let Caddy generate a localhost certificate.
- Store gateway config and cert material under Application Support, with
  private material protected by normal user-only file permissions.
- Provide an explicit trust workflow if the cert needs to be trusted by system
  clients.
- Verify WKWebView, CLI, OpenClaw, Claude Code, Codex, and curl behavior.
- Document any client that still requires an insecure flag or manual trust
  import.

Open issue:

- The main unknown is not whether Caddy can serve HTTPS. It can. The unknown is
  how cleanly local agent clients trust the certificate. That must be tested
  against OpenClaw before considering the release gate satisfied.

### Mode 2: Manual cert files and hostname

Included as a release-gate add-on for operators who already have certificate
material or a Tailscale/MagicDNS name.

Inputs:

- Hostname.
- Certificate chain path.
- Private key path.
- Bind address list, including loopback, all interfaces, or detected Tailscale
  interface shortcuts.

Requirements:

- Require both certificate and private key paths before restart.
- Let Caddy reject invalid or mismatched files at gateway startup.
- Future hardening: validate certificate/private-key match, hostname coverage,
  and expiry before restart.
- Warn if the gateway is configured for remote access.
- Store paths, not key material.
- Reload/restart the gateway when settings change.
- Reflect the effective local MCP URL on the Integrations page.

### Mode 3: ACME/Let's Encrypt

Deferred.

Reason:

- Requires DNS or HTTP challenge UX, renewal, firewall/router/tunnel guidance,
  and more failure modes.
- It is valuable, but it should not block the first public release.

## User-facing surfaces

### Harbor Clerk Server Preferences

Add a "Local HTTPS Gateway" section near Network Access:

- Gateway URL: `https://localhost:8443`.
- Gateway port.
- Hostname.
- Bind addresses, with loopback, all interfaces, and detected Tailscale
  shortcuts.
- Certificate mode: generated/self-signed local certificate or manual
  certificate files.
- Certificate and private-key file pickers when manual mode is enabled.
- Copy/read local MCP URL.
- Warning copy that external binds expose only `/mcp` and `/t`.

Changing gateway port or certificate mode should mark that a restart/reload is
needed, then perform a targeted gateway restart when applied.

### Status page

Add gateway state alongside API/search/local AI:

- Gateway running.
- Certificate valid.
- Certificate trusted by this Mac, if detectable.
- Public URL configured or not configured.
- Remote access enabled or disabled.

The Status page should distinguish:

- "Local HTTPS unavailable" for local gateway failures.
- "Public HTTPS not configured" for cloud connectors. This is informational
  unless the user is trying to set up cloud MCP/OAuth.

### Integrations page

Split URLs by use case:

- Local URL: effective same-machine URL, preferably `https://localhost:8443`
  after this work lands.
- Public URL: operator-configured trusted HTTPS URL for cloud connectors.

Copy should be explicit:

- ChatGPT/cloud connector examples use Public URL only.
- OpenClaw/Codex/Claude Code local examples use Local URL by default.
- If Public URL is unset, cloud connector examples should show a placeholder
  and say setup is incomplete.

### CLI shim

After local HTTPS is stable:

- Bake `HARBOR_CLERK_URL=https://localhost:<gatewayPort>` into the macOS shim.
- Mark old HTTP shims stale so users reinstall.
- Keep an environment-variable override for unusual deployments.
- Consider whether the CLI should default to HTTPS localhost everywhere after
  macOS parity lands.

## Implementation plan

### PR 1: Native localhost HTTPS gateway and URL split

Status: implemented in this branch, then expanded to cover hostname, bind,
Tailscale, and manual certificate-file settings.

- Make Integrations copy distinguish local clients from cloud clients.
- Use the local gateway URL for local-capable MCP examples.
- Add `gateway_port` and gateway enable/cert-mode settings.
- Bundle or locate a Caddy binary in the macOS Server resources.
- Add `GatewayService` as a managed service.
- Render a Caddyfile from settings into Application Support.
- Start gateway after API is healthy.
- Health-check the gateway over HTTPS.
- Add tests for settings persistence, config generation, and stale-port
  detection.
- Keep FastAPI bound to loopback even when the gateway binds to external
  interfaces.
- Restrict external native gateway binds to `/mcp` and `/t`.
- Mark non-loopback gateway health as "not probed" from the unauthenticated
  health endpoint, avoiding a remote-probe/SSRF-shaped diagnostic.

### PR 2: Native app routing

- Decide whether Harbor Clerk.app loads the SPA through HTTPS by default.
- Update `BackendDetector`, `AuthManager`, and WKWebView certificate handling
  if the app moves to HTTPS.
- Add fallback behavior if the gateway is disabled or unhealthy.

### PR 3: Certificate trust and smoke tests

- Implement or document the trust workflow.
- Verify curl, `harbor-clerk` CLI, OpenClaw MCP, Claude Code MCP, and browser
  behavior.
- Add a release smoke section covering local HTTPS setup.
- Decide whether any client-specific insecure flag is acceptable for release.

### PR 4: Manual hostname/cert hardening

- Validate cert/key pair, hostname coverage, and expiry.
- Add certificate trust/status display.
- Decide whether to copy cert/key material into Application Support or continue
  storing operator-managed paths only.
- Add public URL/OAuth guidance only after the public trusted HTTPS flow is
  designed.

## Release gates

Minimum gate:

- macOS native exposes a working same-machine HTTPS URL for MCP.
- macOS native exposes a configurable remote/tailnet MCP URL when explicitly
  requested, with only `/mcp` and `/t` reachable externally.
- OpenClaw can complete a documented MCP smoke task against that URL, or the
  docs clearly route OpenClaw through CLI until trust is solved.
- Docker and macOS snippets no longer contradict actual listener behavior.
- Cloud connector docs continue to require public trusted HTTPS.
- Status exposes whether the gateway is healthy.
- CLI still works when the gateway is disabled or unavailable.

Preferred gate:

- Harbor Clerk.app, CLI shim, and local MCP snippets all use the same local
  HTTPS gateway URL by default.
- Certificate trust is first-class enough that local agent users do not need to
  hand-edit insecure flags.
- Public/manual cert configuration is at least designed and not blocked by the
  local gateway architecture.

## Risks

- Local certificate trust may be client-specific and messy.
- WKWebView may need explicit certificate challenge handling if the app loads
  through the gateway before system trust is installed.
- Bundling Caddy may affect notarization/signing expectations.
- Port conflicts on `8443` need a clear recovery path.
- Remote-access toggles may need to move from "bind uvicorn broadly" to "bind
  gateway broadly and keep uvicorn loopback."
- Self-signed local HTTPS could give users a false sense that cloud connectors
  are configured; copy must keep the boundary explicit.

## Open questions

- Should Harbor Clerk.app switch to HTTPS in the same PR as the gateway, or
  should the gateway initially exist only for CLI/MCP?
- What default gateway port is least surprising: `8443`, `9443`, or another?
- Can OpenClaw trust the local cert through normal system trust, or does it
  require an explicit client setting?
- Should manual cert mode store private key paths or copy key material into
  Harbor Clerk's Application Support directory?
- Should public URL settings be web-only, native Preferences-only, or shared
  across both surfaces?

## Estimated effort

- Copy and docs correction: less than 1 day.
- Native self-signed HTTPS gateway: 3 to 5 engineering days.
- Release-quality local HTTPS with status, trust handling, CLI/app snippet
  updates, and OpenClaw smoke: about 1 week.
- Manual real cert and hostname support: 1 to 2 additional weeks.
- ACME/Let's Encrypt automation: defer; likely 2 to 4 weeks depending on UX
  scope.

# Native HTTPS Gateway - Design Spec

**Date:** 2026-06-08
**Status:** Draft for release-gate planning
**Scope:** macOS native HTTPS parity with Docker for local UI, MCP, CLI, and local agent harness setup. Includes manual real-certificate support planning. Excludes ACME/Let's Encrypt automation.

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

## Current behavior

Docker:

- Caddy listens on port 443.
- Caddy uses an internal/self-signed local cert for `localhost`.
- Caddy proxies `/`, `/api/*`, `/mcp/*`, and `/t/*` to the app container.

macOS native:

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
- Existing "Allow remote browser connections" and "Allow remote model
  connections (MCP)" should control whether the gateway can bind beyond
  loopback.
- Direct API binding to `0.0.0.0` should be reconsidered once the gateway
  exists; the safer release posture is public traffic through the gateway, not
  straight to uvicorn.

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

### Mode 2: Manual real cert and hostname

Fast follow or same release only if the local gateway lands cleanly.

Inputs:

- Hostname.
- Certificate chain path or uploaded certificate.
- Private key path or uploaded key.
- Optional bind address.
- Optional public URL override.

Requirements:

- Validate certificate and private key match.
- Validate hostname coverage and expiry.
- Warn if the gateway is configured for remote access.
- Store paths or key material safely.
- Reload/restart the gateway when settings change.
- Reflect the effective URL on the Integrations page.

### Mode 3: ACME/Let's Encrypt

Deferred.

Reason:

- Requires DNS or HTTP challenge UX, renewal, firewall/router/tunnel guidance,
  and more failure modes.
- It is valuable, but it should not block the first public release.

## User-facing surfaces

### Harbor Clerk Server Preferences

Add a "Local HTTPS Gateway" section near Network Access:

- Enable local HTTPS gateway.
- Gateway URL: `https://localhost:8443`.
- Gateway port.
- Certificate mode: local certificate or manual certificate.
- Certificate trust status.
- Real hostname/cert controls when manual mode is enabled.
- Copy local MCP URL.
- Link to Integrations.

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

### PR 1: Copy and URL split

Status: in progress in this planning branch.

- Make Integrations copy distinguish local clients from cloud clients.
- Use current app origin for local-capable MCP examples.
- Document that macOS native is currently HTTP and Docker is HTTPS.

### PR 2: Native gateway service

- Add `gateway_port` and gateway enable/cert-mode settings.
- Bundle or locate a Caddy binary in the macOS Server resources.
- Add `GatewayService` as a managed service.
- Render a Caddyfile from settings into Application Support.
- Start gateway after API is healthy.
- Health-check the gateway over HTTPS.
- Add tests for settings persistence, config generation, and stale-port
  detection.

### PR 3: Native app and CLI routing

- Decide whether Harbor Clerk.app loads the SPA through HTTPS by default.
- Update `BackendDetector`, `AuthManager`, and WKWebView certificate handling
  if the app moves to HTTPS.
- Update CLI shim defaults to the gateway URL.
- Update Integrations snippets to prefer the gateway URL.
- Add fallback behavior if the gateway is disabled or unhealthy.

### PR 4: Certificate trust and smoke tests

- Implement or document the trust workflow.
- Verify curl, `harbor-clerk` CLI, OpenClaw MCP, Claude Code MCP, and browser
  behavior.
- Add a release smoke section covering local HTTPS setup.
- Decide whether any client-specific insecure flag is acceptable for release.

### PR 5: Manual hostname/cert support

- Add Preferences UI for hostname, cert chain, private key, bind address, and
  effective public URL.
- Validate cert/key pair, hostname coverage, and expiry.
- Store paths or key material safely.
- Reload gateway on changes.
- Update Status and Integrations with the effective public URL and warnings.

## Release gates

Minimum gate:

- macOS native exposes a working same-machine HTTPS URL for MCP.
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

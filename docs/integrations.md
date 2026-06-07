# Harbor Clerk Integrations

Harbor Clerk can expose a local document archive to external models and local
agent harnesses through two surfaces:

- **MCP** for cloud models and connector-style clients that can call structured
  tools over HTTPS.
- **CLI** for shell-first local agents such as Codex, Claude Code, OpenClaw,
  and Aider.

Both surfaces use the same scoped API key model, audit logging, citations, and
tool contracts. Use MCP when a client speaks MCP natively. Use the CLI when an
agent is better at composing shell commands than remote tool calls.

## Data Boundary

Harbor Clerk does not upload the full corpus to connected tools. External MCP
clients receive only tool responses: retrieved snippets, citations, document
titles, folder labels, relative paths, and structured metadata needed for the
request. Cloud model providers may see that response content and the tool
arguments they are asked to run.

CLI usage stays on the local machine unless the agent harness itself sends
command output to a cloud model. Treat local agent harnesses as capable
operators: give them scoped keys, rate limits, and expiry dates just as you
would for a cloud connector.

By default, agent-visible paths should avoid absolute local filesystem paths.
Use relative paths and folder aliases unless a local-only workflow explicitly
needs more detail.

Cloud access through MCP is the supported release path for external models.
Direct provider-backed Ask/Research inside the Harbor Clerk app is a separate
fast-follow surface and should ship only after citation preservation, provider
key storage, disclosure, path policy, and failure-mode tests are stable. See
[Cloud model boundaries](cloud-model-boundaries.md) for the release gate.

## API Key Guidance

Create a dedicated API key for each external tool or harness.

- **Search**: search and enumerate documents.
- **Read**: search plus passage reads, document metadata, outlines, identifier
  verification, and date lookup.
- **Full**: all read-only tools, including full document reads, entity tools,
  related-document tools, and ingest-status checks.

Recommended defaults:

- Cloud connector: **Read**, folder-scoped when possible, snippet caps enabled.
- Local agent harness: **Read** for normal citation workflows; **Full** only
  when the agent needs full-document reads, entity tools, or ingest-status
  checks.
- Autonomous agent: rate limit and expire the key. Review usage in the audit
  dashboard.

The admin-only tools `kb_system_health` and `kb_reprocess` are never available
to API keys.

If a skill or harness tries a tool outside its key scope, treat that as a
recoverable scope mismatch. The agent should continue with search and passage
reads, then tell the operator which broader capability was denied.

## MCP Setup

Use MCP for ChatGPT, Claude Desktop, Gemini CLI, and any other client that can
call remote MCP servers.

1. Set a public HTTPS URL in **Settings -> Integrations** if the client is not
   running on the same machine.
2. Create a scoped API key in **Settings -> API Keys**.
3. Use the MCP URL shown in the Integrations page.

Use `/mcp` for OAuth or clients that can send `Authorization: Bearer
YOUR_API_KEY`. For clients that cannot attach bearer headers, Harbor Clerk
supports the URL-token path form:

```text
https://your-server.example.com/t/YOUR_API_KEY
```

Do not reuse a broad admin key for autonomous tools. Create a separate scoped
key per connector.

## CLI Setup

Use the CLI for shell-first agent harnesses.

1. Enable CLI access in **Harbor Clerk Server -> Preferences -> Network
   Access**. Docker/Linux operators can set `ENABLE_CLI_ACCESS=true` and
   restart the API service.
2. Install the CLI shim from the same preferences panel if running the macOS
   native app.
3. Export connection settings in the shell where the agent runs:

```bash
export HARBOR_CLERK_URL="http://localhost:8100"
export HARBOR_CLERK_API_KEY="YOUR_API_KEY"
export PATH="$HOME/.local/bin:$PATH"
```

4. Smoke test:

```bash
harbor-clerk search "contract renewal" --json
harbor-clerk find-all "renewal" --presentation full --json
```

The CLI is audit-logged as `request_type="cli_tool"`, distinct from MCP
traffic. Exit code 3 means CLI access is disabled by an admin.

## OpenClaw

Release-safe claim:

> Harbor Clerk can act as a private, cited memory layer for OpenClaw through
> MCP or CLI.

Prefer CLI for local OpenClaw runs because it matches OpenClaw's shell-first
workflow and lets the agent use the checked-in Harbor Clerk skill markdown.
Use MCP when your OpenClaw setup is already configured for MCP servers.

Minimum setup:

1. Create a dedicated, scoped API key.
2. Enable CLI access and install the CLI shim.
3. Copy `skills/harbor-clerk/SKILL.md` into the OpenClaw skill location used by
   your local setup.
4. Run a smoke task that requires a cited source from the local archive.

Suggested first task:

```text
Using Harbor Clerk, find the documents that mention renewal terms, cite the
best passages, and tell me which source each passage came from.
```

Do not claim Harbor Clerk improves OpenClaw outcomes until the
[agent-memory eval](agent-memory-eval.md) has been run and the results support
that stronger claim.

## Codex

Codex is best treated as a CLI-orchestrating local agent.

1. Create a scoped API key.
2. Enable CLI access and install the CLI shim.
3. Export `HARBOR_CLERK_URL` and `HARBOR_CLERK_API_KEY` in the shell where
   Codex runs.
4. Copy the Harbor Clerk skill markdown into your local Codex skill location.

Useful first commands:

```bash
harbor-clerk search "topic or phrase" --json
harbor-clerk expand-context CHUNK_ID -n 3 --json
harbor-clerk find-all "literal or semantic query" --presentation full --json
```

## Claude Desktop and Claude Code

Claude Desktop usually uses MCP:

```json
{
  "mcpServers": {
    "harbor-clerk": {
      "url": "https://your-server.example.com/t/YOUR_API_KEY"
    }
  }
}
```

Claude Code can use MCP if configured for it:

```bash
claude mcp add harbor-clerk "https://your-server.example.com/t/YOUR_API_KEY"
```

For shell-first coding sessions, the CLI path is often simpler. Enable CLI
access, export the same environment variables shown above, and copy the Harbor
Clerk skill markdown into the local skill directory.

## ChatGPT and Other Cloud Connectors

ChatGPT-style OAuth connectors require a public HTTPS URL and will authorize
through the browser. Once connected, the cloud model calls Harbor Clerk tools
and receives retrieved snippets with citations.

Make the data boundary explicit when helping users set this up:

- The corpus stays local.
- Retrieved snippets and citation metadata can be sent to the provider.
- API keys should be scoped.
- Audit logs should be reviewed if an autonomous agent may call tools in loops.

This is different from future in-app cloud Ask/Research. In the current release,
cloud models use Harbor Clerk as a cited retrieval tool through MCP; the app's
own Ask and Research surfaces remain local-AI-first unless an explicit cloud
mode is later added.

## Troubleshooting

- **401 Unauthorized**: regenerate or re-copy the API key.
- **403 CLI access disabled**: enable CLI access in Preferences or set
  `ENABLE_CLI_ACCESS=true` for Docker/Linux.
- **MCP client cannot attach headers**: use the URL-token form.
- **Agent cannot find expected documents**: check ingestion status and key
  document scope before changing prompts.
- **Too many tool calls**: lower per-key rate limits or narrow the document
  scope.

# `harbor-clerk` CLI for agentic harnesses — Design

**Status:** Draft for review
**Date:** 2026-05-23
**Author:** brainstorm with Claude

## Goal

Expose Harbor Clerk's retrieval and ingest-status surface as a command-line tool that local agentic harnesses (OpenClaw, Claude Code, Codex, Aider, Goose, etc.) can invoke directly to extend their memory. Additive — the existing MCP server stays primary and unchanged.

## Non-goals

- **Not a migration off MCP.** MCP remains the primary agent interface. The CLI is a second front door for harnesses that prefer the CLI shape.
- **Not a separate distribution.** Same Python package, same install. No slim `harbor-clerk-cli` PyPI sibling.
- **No per-tool scoping.** Single global on/off toggle is enough, per the brainstorm.
- **No new auth model.** Uses existing API keys with no additions.

## Context: why this is worth building (skeptical pass)

The late-May-2026 "CLI > MCP" buzz in the OpenClaw / Claude-Code-alternative community is grounded in real evidence (Scalekit benchmark: CLI 1,365 tokens vs MCP 44,026 tokens on the same task; Apideck: 55k tokens of MCP definitions can sit in context before the first prompt). But the critique targets bloated remote MCP servers with 50–100 tools (GitHub MCP is the recurring villain). Harbor Clerk has 16 focused tools — the token-overhead argument applies weakly.

The retrieval-specific signal (qmd, knowledge-base-MCP repos) is MCP-first with CLI for human/scripting use. The defensible position is "ship both," and our specific motivation is OpenClaw integration where users want a CLI-shaped tool to wire into their CLI-orchestrating runtime.

## Architecture

**The CLI is an MCP client.** It speaks JSON-RPC over HTTP to the existing `POST /mcp` endpoint, identifying itself with `User-Agent: harbor-clerk-cli/<version>`. No new server endpoints, no duplicated tool logic.

```
OpenClaw agent ──spawn──▶ harbor-clerk search "..."
                                   │
                                   ▼
                            POST /mcp   (JSON-RPC)
                            Authorization: Bearer <api_key>
                            User-Agent: harbor-clerk-cli/0.1.0
                                   │
                                   ▼
                            Harbor Clerk daemon
                            ├─ auth middleware
                            │   • verifies API key (existing path)
                            │   • if UA matches CLI AND enable_cli_access=false → 403
                            ├─ MCP server (existing 16 tools, unchanged)
                            └─ api_request_log: request_type="cli_tool"
```

## Server delta (small)

1. **New setting `enable_cli_access: bool`** in `model_settings` (default `false`). Surfaced in **System Settings → Integrations**.
2. **Auth middleware sniff:** if `User-Agent` starts with `harbor-clerk-cli/` and `enable_cli_access=false` → respond `403` with body `{"error":"cli_access_disabled","hint":"Enable in System Settings → Integrations"}`. Audit row written with `status="error"`, `status_detail="cli_access_disabled"`.
3. **Audit marker:** when `User-Agent` matches, write `request_type="cli_tool"` (existing TEXT column in `api_request_log`, new literal value — no schema migration). Existing audit UI surfaces it; the Integrations page should add a filter chip.

That's the entire server delta. ~50 lines of code, zero migrations.

## CLI command surface

Flat, 16 subcommands mirroring MCP tool names with kebab-case. The `kb_` prefix is dropped — redundant in `harbor-clerk` context.

| MCP tool | CLI subcommand |
|---|---|
| `kb_search` | `harbor-clerk search` |
| `kb_batch_search` | `harbor-clerk batch-search` |
| `kb_read_passages` | `harbor-clerk read-passages` |
| `kb_expand_context` | `harbor-clerk expand-context` |
| `kb_get_document` | `harbor-clerk get-document` |
| `kb_list_recent` | `harbor-clerk list-recent` |
| `kb_corpus_overview` | `harbor-clerk corpus-overview` |
| `kb_document_outline` | `harbor-clerk document-outline` |
| `kb_find_related` | `harbor-clerk find-related` |
| `kb_entity_search` | `harbor-clerk entity-search` |
| `kb_entity_overview` | `harbor-clerk entity-overview` |
| `kb_entity_cooccurrence` | `harbor-clerk entity-cooccurrence` |
| `kb_read_document` | `harbor-clerk read-document` |
| `kb_ingest_status` | `harbor-clerk ingest-status` |
| `kb_reprocess` | `harbor-clerk reprocess` |
| `kb_system_health` | `harbor-clerk system-health` |

## Configuration (env-var only)

| Variable | Default | Purpose |
|---|---|---|
| `HARBOR_CLERK_URL` | `https://localhost` | Daemon endpoint |
| `HARBOR_CLERK_API_KEY` | (required) | API key from System Settings → API Keys |
| `HARBOR_CLERK_INSECURE_SKIP_VERIFY` | `false` | Allow self-signed cert (must opt in explicitly) |

CLI flags `--url`, `--api-key`, `--insecure` override env vars. No config file, no `harbor-clerk login` — keeps the surface predictable for agents.

## Output conventions

- **Default:** JSON to stdout if stdout is **not** a TTY (invoked by an agent / piped); pretty-printed text if TTY (human user).
- **Override:** `--json` forces JSON; `--format text` forces text.
- **JSON shape:** the exact JSON the MCP tool returns. No new serialization.
- **Errors:** machine-readable JSON on stderr when `--json`; human-readable line on stderr otherwise. stdout stays clean.

## Help (the showpiece)

Per-subcommand man-page-class help. Structure (every subcommand):

1. **DESCRIPTION** — what it does, what guarantees it provides (e.g., "results include citations," "possible_conflict flag when top hits disagree").
2. **USAGE** — one-line signature.
3. **OPTIONS** — every flag with type, default, constraints, and any sub-flag dependencies.
4. **RETURNS (JSON)** — full JSON schema with inline field annotations (`// pass to read-passages`, `// 1-indexed page`, etc.).
5. **EXAMPLES** — 3–5: a golden path, an edge case, a shell-composition example (`| jq ...`), and any two-stage idioms.
6. **COMMON MISTAKES** — known footguns (e.g., "chunk_id is not doc_id," "default `--detail=full` returns 1–2KB per result").
7. **SEE ALSO** — related subcommands.

**Implementation:** each subcommand's help text lives in `src/harbor_clerk/cli/help/<subcommand>.txt`. The argparse setup loads from disk. Keeps Python code clean; lets the doc text be reviewed and revised independently.

## Failure modes (explicit, distinguishable)

| Exit code | Meaning |
|---|---|
| `0` | Success |
| `1` | Bad usage (unknown flag, missing required arg) — argparse default |
| `2` | Cannot reach daemon (connection refused, DNS, timeout) |
| `3` | CLI access disabled at server (403 with `cli_access_disabled` body) |
| `4` | Auth failure (missing / invalid / revoked API key) |
| `5` | API returned non-success (4xx other than 401/403, or 5xx) |

stderr always carries a short human-readable explanation. stdout stays clean JSON in `--json` mode (so agents can pipe it).

## Distribution

- **macOS app:** on first launch with CLI enabled, prompt to install `/usr/local/bin/harbor-clerk` via `AuthorizationServices` (one-time admin auth). Fallback `~/.local/bin/harbor-clerk` if the user declines admin, with a copyable PATH snippet. Install status visible in System Settings → Integrations.
- **Docker:** Python entry point already exists; users run `docker exec harbor-clerk-app harbor-clerk ...`.
- **Pip:** `pip install harbor-clerk` ships the CLI alongside the server. Point `HARBOR_CLERK_URL` at a remote daemon when installing on a different machine.

## OpenClaw integration: in-repo skill + Integrations page exposure

**The skill lives in this repo at `skills/harbor-clerk/SKILL.md`** for now. We do **not** publish it to the OpenClaw skill repo yet — that's deferred to the HC 1.0 launch (see "Deferred work" below).

**System Settings → Integrations** gets a new "Agentic CLI" card:

- Toggle for `enable_cli_access`.
- Link out to **API Keys** for minting a CLI-suitable key.
- macOS install status (installed at `/usr/local/bin/harbor-clerk`, or "not installed — click to install").
- A code block with the full skill markdown, plus a **Copy** button. The copy text is exactly what an OpenClaw / Claude Code user would drop into their `~/.openclaw/skills/harbor-clerk/SKILL.md` (or equivalent).
- A short "How to use" blurb explaining the copy-paste flow.

The skill markdown itself is intentionally thin — the CLI's `--help` carries the heavy doc weight:

```markdown
---
name: harbor-clerk
description: Search and read documents from a local Harbor Clerk knowledge base. Use when the user references their personal documents, contracts, notes, emails, or asks "what did I store about X?"
---

# Harbor Clerk — extended memory for agents

Harbor Clerk is a local document corpus with hybrid FTS+vector search and citation-ready reads. This skill exposes it via the `harbor-clerk` CLI.

## First step: discover the surface
Run `harbor-clerk --help` for the full command list, then `harbor-clerk <cmd> --help` for any specific command. The help is comprehensive — JSON return shapes, examples, common mistakes are all there.

## Three patterns you'll use most

1. **Search → expand**: `harbor-clerk search "..."` then `harbor-clerk expand-context <chunk_id> -n 3` on the best hit.
2. **Read a known document**: `harbor-clerk read-document <doc_id>` for full text with pagination.
3. **Check ingest status before searching for new content**: `harbor-clerk ingest-status <doc_id>` returns per-stage state.

## What you can trust
- Search returns top-level `.hits[]`; each hit includes `doc_title`, `pages` when available, and `chunk_id`. Use those fields as the citation, then call `read-passages` when you need exact surrounding text.
- `possible_conflict: true` means top hits span multiple similarly scored documents; inspect the relevant sources before answering.
- The CLI exits non-zero on failure. Exit code 3 specifically means an admin has disabled CLI access — tell the user.
```

## Build sequence

1. **Server gate** — `enable_cli_access` setting in `model_settings`, auth-middleware UA sniff, `request_type="cli_tool"` literal, Integrations UI control. (~½ day)
2. **CLI scaffold** — `src/harbor_clerk/cli/__init__.py`, argparse skeleton, `pyproject.toml` entry point, env-var + flag plumbing, TTY auto-detection, exit-code conventions. (~½ day)
3. **MCP client** — HTTPX wrapper that does JSON-RPC over `POST /mcp`, unwraps results, maps errors to exit codes. (~½ day)
4. **16 subcommand implementations** — each ~20 lines: parse args, call MCP client, format output. (~1 day)
5. **Help text files** — the deliberate, careful part. Per-subcommand `.txt` with the full 7-section structure. (~1–2 days)
6. **Integrations page card** — toggle, install status, skill copy block. (~½ day)
7. **macOS shim installer** — Swift code in `HarborClerkServer` that drops the binary via `AuthorizationServices` and reports status to the UI. (~½ day)
8. **In-repo skill markdown** — committed at `skills/harbor-clerk/SKILL.md`. (~15 min)
9. **Tests** — end-to-end against a test daemon: each subcommand exercised, all five exit codes triggered, UA-gating verified. (~½ day)
10. **Docs** — README section, settings copy. (~1 hour)

**Total: ~5–6 days of focused work.**

## Explicitly NOT doing (YAGNI)

- No `harbor-clerk login`. Env vars are enough.
- No config file. Env vars are enough.
- No per-tool permission scoping. User explicitly said global toggle suffices.
- No standalone slim CLI PyPI package. Same package, same install.
- No streaming output for long-running commands. MCP tools don't stream either.
- No `harbor-clerk call <tool>` escape hatch for unknown tools. The 16 subcommands are the contract.
- No publication to the OpenClaw skill repo at this milestone. **Deferred to HC 1.0** (see below).

## Deferred work (post-merge follow-ups)

- **Publish skill to OpenClaw skill repo at HC 1.0 launch.** The in-repo skill markdown is the canonical source until then. When HC 1.0 ships, copy to the OpenClaw skill repo and add a discoverability link from the Integrations page.
- **Audit-log filter chip in Integrations UI** for `request_type="cli_tool"` so admins can see CLI traffic separately.
- **Streaming output** for `reprocess` and `ingest-status --watch` if there's demand. Would require SSE support in the CLI client.
- **Standalone slim PyPI package** if installing the full Harbor Clerk package just for the CLI becomes a friction point for off-machine users.

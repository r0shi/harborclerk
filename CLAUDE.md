# CLAUDE.md

Operating rules for any agent or human working in this repository. Rules here
are binding. Reference material — architecture, API surface, schema — lives
elsewhere; see [Map](#map). On matters of **fact**, the code is authoritative and
this file may be stale: trust the source and fix this file. On matters of
**rule**, this file is authoritative.

## What this is

Harbor Clerk is a single-tenant, local-first document intelligence system: local
extraction, OCR (eng/fra), hybrid retrieval with reranking, cited local-AI
answers, and Research. It runs as a Mac-native app or via Docker Compose, and
exposes the corpus to external agents through an MCP server and a mirrored CLI.

## Non-negotiables

- **Single-tenant.** No `tenant_id` in schema, API, or code. Ever.
- **Read-only external access.** API keys are read-only and stored hashed.
- **Snippets, not corpus.** MCP/CLI callers receive retrieved passages and
  citations — never a bulk corpus export.
- **Never modify a source.** Watched-folder files are never altered, copied, or
  moved. Mailboxes are read-only (IMAP `EXAMINE`; read-only OAuth scopes when
  OAuth lands) — never marked read, never deleted.
- **Copy only where the source requires it.** Watched-folder documents are read
  in place from `source_path`. IMAP is the deliberate exception: messages and
  attachments must be fetched and stored, because there is no file to reference.
- **All results carry citations.** Every retrieval surface returns sources.
- **Tika is required** for PDF/Office/eBook/HTML/email extraction.
- **Never commit credentials.** Passwords, API keys, JWTs, or tokens supplied in
  conversation never enter a file, regardless of stated intent.
- **Strategy stays private.** Positioning, competitive analysis, claim posture,
  and candid assessment live in `docs/strategy/` (a separate private repo),
  never here.

## Working agreement

- **Branch first.** Create a feature branch before any change, including docs.
  Never commit to `main`. Do this from the start, not retroactively.
- **PR always.** Every change lands via PR, even docs-only. Branch protection is
  enforced.
- **Never bypass the gate.** No `gh pr merge --admin`, no direct push to main,
  without explicit human permission in the current conversation.
- **Verify before claiming done.** Run the checks and read the output before
  saying anything passes. Evidence, then assertion.
- **Don't stack PRs.** Land the base on `main` and branch again. A stacked
  branch once lost ~3,000 lines to a squash-merge of its base.
- **Regenerate docs** after changing tools, routes, models, stages, or compose
  services: `uv run python -m scripts.gen_docs`. CI fails otherwise.
- **`dependency-audit` can fail on an unrelated PR.** It audits the whole
  environment against live advisory databases, so a newly-published CVE turns it
  red on a change that touched nothing relevant.

## Review policy

A PR requires a fresh-eyes review before merge if it touches more than three
files, changes behavior, modifies public API or tool contracts, adjusts
auth/security, affects ingest/retrieval/LLM flows, **or adds a new subsystem,
client surface, or storage location**. Trivial doc edits and one-line mechanical
fixes do not.

That last trigger exists because a new subsystem is exactly when a stated
non-negotiable quietly stops being true — "originals are never copied" was
correct until IMAP ingest shipped.

The reviewer must be **unprimed**: hand it the diff and nothing else. No focus
areas, no "don't flag X", no restatement of the design. A primed reviewer
confirms the author's mental model instead of finding defects. Self-review "as
if someone else wrote it" is not a substitute — it is the failure mode.

Review output leads with bugs, regressions, missing tests, and contract risk.

## Where knowledge goes

- Project-wide rule or constraint → this file
- Subsystem gotcha → the nearest scoped `CLAUDE.md`
- Decision + rationale + rejected alternatives → `docs/adr/`
- Deferred work → a GitHub issue
- Strategy, positioning, claims → `docs/strategy/` (private repo)
- Personal workflow preference → user memory

**A project rule never goes in personal memory.** Test: could someone with a
fresh clone and no memory work on this repo safely? If not, a rule is in the
wrong place.

## Map

| What | Where |
|---|---|
| Architecture, data model, deployment | `docs/architecture.md` |
| Generated reference (tools, routes, tables, stages, services) | `docs/architecture.md#generated-reference` |
| Design specs and implementation plans | `docs/superpowers/{specs,plans}/` |
| Eval harness and methodology | `scripts/test_corpora/`, `docs/evaluation.md` |
| Integration setup (MCP, CLI, connectors) | `docs/integrations.md` |
| Backend gotchas and pipeline invariants | `src/harbor_clerk/CLAUDE.md` |

Entry points are declared in `pyproject.toml`. The MCP tool surface is defined
in `src/harbor_clerk/mcp_server.py` and published as a generated table — read
those rather than a transcription.

## Linting & formatting

- Python: `uv run ruff check .` / `uv run ruff format .` (line length 120)
- Frontend: `npm run lint` / `format:check` / `type-check` in `frontend/`
- Everything at once: the `verify` skill

## CI

Five required checks on PRs to `main`: `python`, `frontend`, `codeql`,
`dependency-audit`, `container-scan`. The `python` job also verifies generated
docs are current.

---

## Pending restructure (batches 3–5)

The sections below were **not** part of the batches 1–2 restructure and are kept
here awaiting a placement decision. Factual errors in them have been corrected —
leaving a known-wrong statement in place to preserve a batch boundary would be
the wrong trade — but where they should ultimately live is still open.

### Python Package

Package name: `harbor_clerk` (under `src/harbor_clerk/`). Entry points are
declared in `pyproject.toml`:

- `harbor-clerk-api` — FastAPI server
- `harbor-clerk-worker` — PostgreSQL-polling background worker
- `harbor-clerk-watcher` — folder + IMAP watcher (one process per deployment)
- `harbor-clerk-seed` — database seeder
- `harbor-clerk` — CLI for agent harnesses; off by default, gated by
  `ENABLE_CLI_ACCESS`. Talks to `/mcp` over JSON-RPC using `HARBOR_CLERK_API_KEY`

### Key API surface

REST, MCP and CLI surfaces are **generated** from source — see
[`docs/architecture.md#generated-reference`](docs/architecture.md#generated-reference).
Do not transcribe them here; the previous hand-written copy drifted.

### Database

PostgreSQL 18 with `vector`, `pg_trgm`, and `citext`. No tenant table, no
`tenant_id` columns.

`chunks` has dual FTS columns (`fts_en`, `fts_fr`) as generated stored columns
with GIN indexes, plus `embedding vector(768)` (Granite-R2) with an HNSW index.
`schema_metadata` records the embedding model and dimension; startup refuses a
mismatch. The full table list is generated; current schema lives in
`alembic/versions/`.

Storage (legacy uploads only): bucket `originals`, key pattern
`originals/<doc_id>/<original_filename>`. Watched-folder documents are read in
place from `source_path` and never stored in MinIO.

### Worker presets (C = logical cores)

- **Quiet**: io=1, cpu=1 · **Balanced**: io=max(2, C//4), cpu=2 · **Fast**:
  io=max(2, C//2), cpu=3
- Hard caps: io ≤ 8, cpu ≤ 4. The `llm` queue runs a single worker in every
  preset.

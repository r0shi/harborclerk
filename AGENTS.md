# AGENTS.md

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

Before requesting review, the author runs two mechanical checks:

- **Sweep for sibling call sites.** A change that fixes a pattern, migrates a
  contract, or adds a cleanup path is not done until the old idiom has been
  grepped for and every hit accounted for in the PR. Three migrations each
  shipped N−1 of N sites; the missed site was always the one outside the
  module being edited — exactly the site self-review never visits.
- **Mutation-verify tests guarding error handling, resource cleanup, or a
  `finally`.** Commit first, then revert the guarded line and confirm a test
  fails; restore, and state the result in the PR. A green suite on a change
  you just made is not evidence — five consecutive review rounds found vacuous
  tests, concentrated in exactly these three places, and revert-the-line
  caught every one.

## Where knowledge goes

- Project-wide rule or constraint → this file
- Subsystem gotcha → the nearest scoped `AGENTS.md`
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
| Pipeline stages, queues, `mark_stage_done`, extraction paths, storage, retrieval, async traps | `src/harbor_clerk/AGENTS.md` |
| Tailwind v4 (no config file), ESLint hooks rules, React Router state loss, npm peer deps, WKWebView dialogs | `frontend/AGENTS.md` |
| Swift process management, `waitUntilExit`, pipe deadlocks, ports 8100/8443, worker presets, bundle IDs | `macos/AGENTS.md` |
| Migrations, enum casing, embedding dimension sentinel, log-table conventions | `alembic/AGENTS.md` |

Console scripts, MCP tools, REST routes, database tables, pipeline stages and
Compose services are all **generated** into
[`docs/architecture.md`](docs/architecture.md#generated-reference) from source.
Read those rather than any transcription, and regenerate with
`uv run python -m scripts.gen_docs` after changing them.

## Linting & formatting

- Python: `uv run ruff check .` / `uv run ruff format .` (line length 120)
- Frontend: `npm run lint` / `format:check` / `type-check` in `frontend/`
- Everything at once: the `verify` skill

## CI

Required checks on PRs to `main`: `python`, `embedder`, `macos`,
`eval-harness`, `frontend`, `codeql`, `dependency-audit`, `container-scan`. The
`python` job also verifies generated docs are current. Counted in prose once — "six required checks" — and
it was wrong by the next PR, so it isn't counted here.

**A new job is not enforced until branch protection requires it.** Adding one to
`ci.yml` makes it run and go green; it does not make it able to block a merge.
Every job added this week (`embedder`, `macos`, `eval-harness`) needed a separate
`gh api -X PATCH repos/:owner/:repo/branches/main/protection/required_status_checks`
call, and until that lands the guard is advisory.

**Add the job to `main` before requiring it.** A required context that never
reports blocks the merge, so requiring `macos` and `eval-harness` while their own
PRs were still open would have deadlocked all four open PRs — including the two
that added those jobs, which then could not merge to supply them. Land the job,
then require it, then rebase everything else.

## Instruction files and your harness

`AGENTS.md` is the real file; `CLAUDE.md` beside it is a **symlink to the same
content**, because Claude Code reads only `CLAUDE.md` while Codex and most other
harnesses read `AGENTS.md`. One source of truth, two names — never edit them as
if they were separate files.

### Scoped files are NOT auto-loaded — read them yourself

Verified empirically (2026-07-21, Claude Code, `/context`): launching from the
repo root loads **this file only**. Scoped `AGENTS.md` files did not appear in
context even after explicitly reading a file in that directory. Do not rely on
on-demand loading; it did not happen.

**Before working in a directory below, read its `AGENTS.md` first. It contains
rules that will not otherwise be in your context.**

The Map above is the discovery mechanism — an agent finds a scoped file because
the Map names its subject. That works when the topic matches obviously and
fails when it does not, which is the second reason to read the file rather than
wait to be reminded.

This is also why anything catastrophic-if-violated lives in this file rather
than a scoped one: **severity decides placement, not topic.**

**If your harness does not support hierarchical instruction files, read the
scoped `AGENTS.md` files manually before working in those directories.**

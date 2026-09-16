# Acceptance suite — API tier — design

**Status:** Proposed, 2026-09-16. Step 2 of the sequence in ADR 0001.
**Scope:** an executable counterpart to `docs/release-smoke-test.md` areas 2–7,
driving a running Harbor Clerk instance over HTTP, MCP and the CLI. UI, Status
recovery and doc review (areas 1, 8, 9) are the native tier and stay manual
until that tier exists. Model quality stays with the eval harness.

## Why

The smoke matrix is run by hand before a release and recorded in the release
notes. v0.9.2's run marked three of nine areas "partial" because there was no
time to finish. One maintainer cannot exercise the product surface manually
each release, and an agent loop cannot exercise it at all until there is
something executable to run. Every claim in the README that a user could
verify with an HTTP client should have a test that verifies it.

## Constraints that shape the design

- **Destructive by nature, so scope it.** The eval harness wipes the instance
  it talks to. This suite must not: it runs on the disposable Mac mini today
  and on the larger machine, next to real data, later. `DELETE
  /api/watch/folders/{id}` cascades to the folder's documents
  (`api/routes/watch.py:451`), so a suite that adds its own watched folder,
  asserts inside it, and deletes it at the end touches nothing else.
  Full-wipe mode exists for clean-instance runs and refuses to start without
  an explicit disposable flag.
- **No new venv.** `mcp`, `httpx` and `pytest` are root-project dependencies,
  so the suite lives in `tests/acceptance/` and runs with `uv run pytest`. The
  eval harness's `SyncMcpSession` and pipeline-wait helpers
  (`scripts/test_corpora/runner/client.py:156-660`) are the reference
  implementation; the suite carries a small client of its own rather than
  importing across uv projects, and says so at the top of the file.
- **Collected by CI, skipped without an instance.** `tests/` is already
  collected, which satisfies `test_ci_collects_every_test_suite.py`. Every
  acceptance test skips with the reason `HC_API_BASE not set` when there is no
  instance. That is the "green without running" shape AGENTS.md warns about,
  so the suite's real gate is the scheduled run on the mini that lands a
  report in `docs/reports/`; a missing or stale report is the alarm, not a
  skipped test. A compose-backed CI job is a follow-up, not part of this spec.
- **Fixtures are generated, not committed as binaries.** Plain-text sources
  under `tests/acceptance/fixtures/` are rendered at run time into the
  document types the pipeline claims to handle. The suite owns the ground
  truth for them.
- **Credentials never enter the tree or a transcript.** Admin credentials and
  the instance URL arrive as environment variables; the skill reads them from
  Keychain.

## What is tested

Each check has an ID; the report maps IDs onto the smoke-matrix areas so the
release template can be filled from the run.

### A. Setup and auth

| ID | Check | Surface |
|---|---|---|
| A1 | `GET /api/system/health` is ok; setup-status is consistent with login working | REST |
| A2 | Login returns a bearer token and sets the refresh cookie; refresh rotates it | REST |
| A3 | An API key is refused on human-only routes (create conversation, create folder) with 403 | REST |

### B. Ingest and status

| ID | Check |
|---|---|
| B1 | Creating the fixture folder starts a scan; `GET /api/watch/folders/{id}/progress` reaches `scan_status=idle`, `ingest_status=idle`, `completed_files == total_files` within the timeout, and `GET /api/jobs/snapshot` shows all queues quiescent |
| B2 | Every fixture reaches `pipeline_status=ready`: text-native PDF, image-only PDF (OCR), Markdown with headings, plain text, French text, `.eml` with attachment, `.docx` |
| B3 | `GET /api/system/status-summary` reports no stranded or failed documents attributable to the fixture folder |
| B4 | `POST /api/docs/{id}/reprocess` on one fixture returns to `ready` with a coherent per-stage `jobs[]` |
| B5 | A file with an unsupported extension in the folder appears in `skipped_extensions`, not as a failed document |

### C. Search and Find All

| ID | Check |
|---|---|
| C1 | A query with a known answer returns hits carrying `source`, `citation`, `pages` or `section`, and `score` |
| C2 | A French query hits the French fixture (bilingual FTS) |
| C3 | Filters each narrow correctly: `scope.folder_ids`, `after`/`before`, `language`, `mime_type`, `text_contains`, `metadata_filter.email.from_address` |
| C4 | Two near-duplicate fixtures produce `possible_conflict=true` with both in `conflict_sources` |
| C5 | Find All with `presentation=full` enumerates every fixture matching a shared phrase, no more, no fewer |
| C6 | `faceted=true` groups hits by document |
| C7 | `POST /api/passages/read` returns the exact fixture text for a hit's `chunk_id` |
| C8 | An unknown `email.*` filter key is a 422, not an empty result |

### D. Documents and citations

| ID | Check |
|---|---|
| D1 | `GET /api/docs/{id}` shows title, canonical filename, mime type, language, pipeline status, per-stage jobs |
| D2 | `GET /api/docs/{id}/content` paginates and matches the source text |
| D3 | `GET /api/docs/{id}/entities` finds the named people and organisations planted in the fixture |
| D4 | The `.eml` fixture's citation is `source_kind=email` with sender and subject in `source_label`; its attachment is `source_kind=attachment` |
| D5 | Every MCP tool payload for fixture documents contains no absolute path: the fixture root string is absent from the serialised JSON |
| D6 | `GET /api/docs/{id}` via a read-tier API key: record whether `source_path` (absolute) is present. Today it is (`api/routes/documents.py:515`); see open question 1 |

### E. Ask (requires a downloaded model; skipped otherwise, reported as partial)

| ID | Check |
|---|---|
| E1 | Activate the smallest curated model; `GET /api/chat/models/status` reaches `ready` on three consecutive polls |
| E2 | One Ask question scoped to the fixture folder streams to a `done` event whose `rag_context.citations` resolve to fixture `doc_id`s |

### F. MCP and CLI parity

| ID | Check |
|---|---|
| F1 | Bearer `POST /mcp/` lists exactly the tier's tool set for each of search, read, full keys; admin-only tools never appear |
| F2 | `/t/<key>` lists the same tools as bearer for the same key |
| F3 | `kb_search` returns hits with `citation` and `source`; `kb_find_all` matches C5 |
| F4 | With CLI access disabled, `harbor-clerk search` exits 3 and the audit row is `cli_tool` / `denied` / `cli_access_disabled` |
| F5 | With CLI access enabled, `harbor-clerk search --json` and `kb_search` with the same key return the same `chunk_id` set and identical `citation` strings |
| F6 | `harbor-clerk find-all --presentation full --json` and `kb_find_all` agree; `harbor-clerk expand-context` returns the passage plus neighbours |
| F7 | `harbor-clerk` with a bad key exits 4; with an unreachable URL exits 2 |

### G. API key scope, limits and audit

| ID | Check |
|---|---|
| G1 | A search-tier key calling a read-tier tool gets `Unknown tool`, and the request log shows `denied` / `tool not in key scope` |
| G2 | A read-tier key can read passages and a document; a search-tier key cannot |
| G3 | A key scoped to a second, empty folder returns no fixture hits and `would_match_unscoped > 0` on the same query |
| G4 | A key with `rate_limit_rpm=2` is refused on the third call with `Rate limit exceeded`, logged `rate_limited`; the limit resets |
| G5 | A key created with an expiry seconds ahead is refused with 401 once it passes |
| G6 | After one MCP call and one CLI call with the same key, `GET /api/api-keys/{id}/usage/requests` contains one `mcp_tool` and one `cli_tool` row |
| G7 | A deleted key is refused with 401 on both `/mcp/` and `/t/` |

### H. Deletion

| ID | Check |
|---|---|
| H1 | Soft-deleting one fixture removes it from search, Find All, `kb_search`, `kb_read_passages` and Ask context (regression for #621/#622) |
| H2 | Deleting the fixture folder removes every fixture document and leaves the request log intact |

Claims deliberately not covered here: onboarding routes, Status UI, Research,
backup docs, OAuth, IMAP. The first two are the native tier; Research is the
eval harness; IMAP needs a mail server and stays in `tests/integration/`.

## Fixtures

`tests/acceptance/fixtures/sources/` holds plain-text sources and one
`groundtruth.yaml`. A `fixtures.py` module renders them into a temporary
folder at run time:

| File | Type | Purpose |
|---|---|---|
| `lease-agreement.txt` → `.pdf` (text layer) | text-native PDF | C1, C7, D1–D3; contains two named parties and a renewal clause |
| `lease-agreement-scan.txt` → image-only `.pdf` at 150 DPI | OCR | B2; same text rendered to an image so OCR output can be compared |
| `vendor-notes.md` | Markdown with headings | B2, `section` in citations |
| `meeting-minutes.txt`, `meeting-minutes-v2.txt` | plain text, near duplicates | C4 |
| `bail-commercial.txt` | French text | C2, `language=fr` |
| `invoice-thread.eml` (built with `tests/mail/fixtures/build_eml.py`) with a small `.txt` attachment | email + attachment | D4, `email.from_address` filter |
| `handbook.docx` (generated with `python-docx` if available, otherwise skipped with reason) | Office | B2 via Tika |
| `notes.xyz` | unsupported extension | B5 |

Ground truth records, per fixture, the expected `pipeline_status`, language,
`source_kind`, planted entities, one query with an exact phrase, and which
fixtures share the Find All phrase. The image PDF is compared to its text twin
by token overlap above a threshold, not by equality; OCR is not exact.

On Docker Compose the folder must be a child of `WATCH_ROOT`; the suite takes
`HC_ACCEPTANCE_FOLDER_ROOT` and falls back to a temporary directory for the
native app.

## Runtime and safety

Environment: `HC_API_BASE` (required, else skip), `HC_USERNAME`, `HC_PASSWORD`
(admin), `HC_ACCEPTANCE_FOLDER_ROOT` (optional), `HC_INSECURE=1` for Caddy's
self-signed certificate, `HC_ACCEPTANCE_DISPOSABLE=1` to unlock wipe mode.

Modes:

- **Folder-scoped (default).** Session fixture creates `hc-acceptance-<run
  id>/` under the folder root, populates it, adds it as a watched folder,
  waits for ingest, yields, then deletes the folder (cascading its documents)
  and every API key it created. Nothing else on the instance is touched. All
  searches carry `scope.folder_ids=[fixture folder]` unless the check is about
  scope itself.
- **Wipe (`--wipe`).** Calls `delete-all-documents` with the literal
  confirmation first, then proceeds as above. Refuses unless
  `HC_ACCEPTANCE_DISPOSABLE=1` is set and `document_count` before the run is
  below a threshold, both, so a mistyped URL cannot wipe the wrong machine.

Never called by the suite: `purge-run`, `clear-queue`, `reprocess-all*`,
`resummarize-all`, `run-migrations`, `recompute-topics`, `DELETE
/api/api-keys/{id}/usage`, model deactivate or delete. The client class has
no methods for them.

Timeouts: ingest wait defaults to 15 minutes with a `status-summary` dump on
failure; Ask to 5 minutes; everything else to 60 seconds.

## Report

`tests/acceptance/report.py` turns the JUnit XML from a run into
`docs/reports/YYYY-MM-DD-acceptance-<host>.md` in the release-smoke template:
the header block from `docs/reports/README.md`, then the nine-area table with
pass, partial or fail derived from the IDs mapped to each area, then the
failing check IDs with their assertion messages. Skipped IDs make an area
"partial" and are listed with their skip reasons.

## Skill

`.claude/skills/acceptance/SKILL.md`: preflight (instance reachable,
credentials present in Keychain under `harbor-clerk-acceptance`, mode chosen,
disposable flag only on the mini), run `uv run pytest tests/acceptance -m
acceptance --junitxml`, generate the report, open a PR as the machine user
with the report and a one-paragraph summary. Failing checks become issues only
when the owner asks; the report is the trace.

## Out of scope, tracked separately

- Native tier (menubar states, SPA, onboarding, Status recovery) with computer
  control: its own spec.
- Isolated data-directory and port override for the Mac app: its own spec;
  required before wipe mode or the eval sweep can run on the larger machine.
- A compose-backed CI job running this suite: follow-up issue once the suite
  is stable on the mini.

## Open questions

1. **`GET /api/docs/{id}` returns the absolute `source_path` to a read-tier
   API key** (`api/routes/documents.py:515`; also `watch_source_path` in
   `GET /api/docs`). MCP payloads strip it and the smoke matrix says
   agent-visible outputs never expose absolute paths, but a key holder can
   reach REST directly. D6 records the current behaviour; deciding whether it
   is a defect is the owner's call.
2. **Ask in tier 1.** E1–E2 need a downloaded model on the target. Proposal:
   include them, skip when no model is present, and let the report show the
   area as partial. The alternative is to drop Ask from this tier entirely.
3. **Schedule.** Run manually until the suite has passed twice on the mini,
   then nightly via a scheduled session that opens the report PR.

## Implementation plan

Each PR lands on `main` before the next branches.

1. `tests/acceptance/`: client, fixtures, session fixture with folder-scoped
   mode, checks A, B, C, D. Marker `acceptance` registered in `pyproject.toml`.
2. Checks F and G (MCP, CLI, keys, audit), which need the CLI access toggle
   and key lifecycle helpers.
3. Checks E and H, wipe mode with its two-part guard, `report.py`, and the
   `acceptance` skill.
4. First real run on the mini; the report PR is the acceptance of this spec.

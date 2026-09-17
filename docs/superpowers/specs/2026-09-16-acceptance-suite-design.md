# Acceptance suite — API tier — design

**Status:** Proposed, 2026-09-16; revised the same day after an unprimed review.
Step 2 of the sequence agreed on 2026-09-16; ADR 0001 records the decisions and
its Consequences are amended alongside this spec.
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
- **No new venv.** `mcp` and `httpx` are root dependencies and `pytest` is in
  the `test` extra CI already installs, so the suite runs with `uv run pytest`.
  The eval harness's `SyncMcpSession` and pipeline-wait helpers
  (`scripts/test_corpora/runner/client.py:156-660`) are the reference
  implementation; the suite carries a small client of its own rather than
  importing across uv projects, and says so at the top of the file.
- **Outside `tests/`, on purpose.** The suite lives in `acceptance/` at the
  repository root. `tests/conftest.py` runs at import: it probes Postgres on
  5433 and 5432, creates a test database, and overrides `DATABASE_URL`,
  storage and `SECRET_KEY` (`tests/conftest.py:107-131`). On the Mac mini,
  5433 is the instance under test. A child conftest cannot stop the parent
  loading, so the only fix is a separate directory.
- **Collected by CI, skipped without an instance.** The `python` job runs
  `uv run pytest acceptance/`, which satisfies
  `test_ci_collects_every_test_suite.py`: the fixture renderer and the safety
  switches run there, and every live check skips with the reason
  `HC_API_BASE not set`. That is the "green without running" shape
  `test_ci_collects_every_test_suite.py` warns about, so the suite's real gate
  is the scheduled run on the mini that lands a report in `docs/reports/`; a
  missing or stale report is the alarm, not a skipped test. A compose-backed
  CI job is a follow-up, not part of this spec.
- **Fixtures are generated, not committed as binaries.** Plain-text sources
  under `acceptance/fixtures/sources/` are rendered at run time into the
  document types the pipeline claims to handle, by hand-rolled writers on the
  standard library and Pillow (a PDF with a text layer, an image-only PDF, a
  minimal DOCX package, an email with an attachment). No new dependency. The
  suite owns the ground truth for them.
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
| B3 | `GET /api/watch/folders/{id}/progress` shows zero errors in every foreground stage and each fixture's `pipeline_status` is `ready`. `status-summary` is instance-wide with no folder attribution, so it is used only in failure diagnostics |
| B4 | `POST /api/docs/{id}/reprocess` on one fixture returns to `ready` with a coherent per-stage `jobs[]` |
| B5 | A file with an unsupported extension in the folder appears in `skipped_extensions`, not as a failed document |
| B6 | Every fixture file is byte-identical after ingest (sha256 before and after): the product reads files in place and never modifies a source |
| B7 | A file written into the folder after registration is picked up by the watcher's live path, becomes `ready`, and is found by a scoped search |

### C. Search and Find All

| ID | Check |
|---|---|
| C1 | A query with a known answer returns hits carrying `source`, `citation`, `pages` or `section`, and `score` |
| C2 | A French query hits the French fixture (bilingual FTS) |
| C3 | Filters each narrow correctly: `scope.folder_ids`, `after`/`before`, `language`, `mime_type`, `text_contains`, and `metadata_filter` with `email.from_address`, `email.to_addresses`, `email.subject_contains`; the response reports `reranker_status` |
| C4 | Two near-duplicate fixtures produce `possible_conflict=true` with both in `conflict_sources` |
| C5 | Find All with `presentation=full` enumerates every fixture matching a shared phrase, no more, no fewer |
| C6 | `faceted=true` groups hits by document |
| C7 | `POST /api/passages/read` returns the exact fixture text for a hit's `chunk_id` |
| C8 | An unknown `email.*` filter key is a 422, not an empty result |

### D. Documents and citations

| ID | Check |
|---|---|
| D1 | `GET /api/docs/{id}` shows title, canonical filename, mime type, pipeline status, per-stage jobs (language is a chunk attribute, checked in C2/C3) |
| D2 | `GET /api/docs/{id}/content` returns page text matching the source; a `pages` range selects pages |
| D3 | `GET /api/docs/{id}/entities` finds the named people and organisations planted in the fixture |
| D4 | The `.eml` fixture's citation is `source_kind=email` with sender or subject in `source_label`. An on-disk `.eml` yields exactly one document: attachments become child documents only through IMAP ingest (`mail/ingest.py:198`), so the attachment's text is checked as part of the email's content, not as its own source |
| D5 | Every MCP tool payload for fixture documents contains no absolute path: the fixture root string is absent from the serialised JSON |
| D6 | `GET /api/docs/{id}` and `/content` via an API key of any tier: record that they succeed and that `source_path` (absolute) is present. Tier is enforced only in MCP (`api/deps.py:187`); see open question 1 |
| D7 | `GET /api/docs` filters narrow the list: `language=fr` returns the French fixture, `mime_type=application/pdf` the two PDFs, `q=` a title match |

### E. Ask (requires a downloaded model; skipped otherwise, reported as partial)

| ID | Check |
|---|---|
| E1 | Only when `GET /api/chat/models` lists a downloaded model and the run is flagged disposable (or `HC_ACCEPTANCE_ALLOW_MODEL_SWAP=1`): record the active model, activate the smallest downloaded one, wait for `state == "ready"` on three consecutive polls, and restore the previous model in `finally`. Activation of a model that is not downloaded is a 404 |
| E2 | One Ask question scoped to the fixture folder streams to a `done` event whose `rag_context.citations` resolve to fixture `doc_id`s |

### F. MCP and CLI parity

| ID | Check |
|---|---|
| F1 | Bearer `POST /mcp/` lists exactly the tier's tool set for each of search, read, full keys; admin-only tools never appear |
| F2 | `/t/<key>` lists the same tools as bearer for the same key |
| F3 | `kb_search` returns hits with `citation` and `source`; `kb_find_all` matches C5 |
| F4 | With CLI access disabled, `harbor-clerk search` exits 3 and the audit row is `cli_tool` / `denied` / `cli_access_disabled`. There is no API to toggle `enable_cli_access`: it is read from the native app's `config.json` or `ENABLE_CLI_ACCESS` on Docker. The suite tests the state it finds; flipping needs `HC_ACCEPTANCE_CONFIG_JSON=<path>` on the server host, is restored in `finally`, and that restore is mutation-verified |
| F5 | With CLI access enabled, `harbor-clerk search --json` and `kb_search` with the same key return the same `chunk_id` set and identical `citation` strings. The CLI subprocess gets `HARBOR_CLERK_URL` from `HC_API_BASE`, `HARBOR_CLERK_API_KEY` from the key under test, and `HARBOR_CLERK_INSECURE_SKIP_VERIFY` from `HC_INSECURE` |
| F6 | `harbor-clerk find-all --presentation full --json` and `kb_find_all` agree; `harbor-clerk expand-context` returns the passage plus neighbours |
| F7 | `harbor-clerk` with a bad key exits 4; with an unreachable URL exits 2 |

### G. API key scope, limits and audit

| ID | Check |
|---|---|
| G1 | A search-tier key calling a read-tier tool gets `Unknown tool`, and the request log shows `denied` / `tool not in key scope` |
| G2 | Over MCP, a read-tier key's `kb_read_passages` and `kb_get_document` succeed and a search-tier key's are refused as `Unknown tool`. REST read routes admit any key regardless of tier (`api/deps.py:187`); D6 records that |
| G3 | A key scoped to a second, empty folder returns no fixture hits from `kb_search` and the response carries `would_match_unscoped > 0` (MCP only; REST responses do not include it) |
| G4 | A key with `rate_limit_rpm=2` is refused on the third call with `Rate limit exceeded`, logged `rate_limited`; the limit resets |
| G5 | A key created with an expiry seconds ahead is refused with 401 once it passes |
| G6 | After one MCP call and one CLI call with the same key, `GET /api/api-keys/{id}/usage/requests` contains one `mcp_tool` and one `cli_tool` row. Runs before session teardown: deleting a key soft-deletes it and nulls the FK on its request-log rows |
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
| `handbook.docx` (minimal OOXML package written by the renderer) | Office | B2 via Tika |
| `notes.xyz` | unsupported extension | B5 |

Ground truth records, per fixture, the expected `pipeline_status`, language,
`source_kind`, planted entities, one query with an exact phrase, and which
fixtures share the Find All phrase. The image PDF is compared to its text twin
by token overlap above a threshold, not by equality; OCR is not exact.

For the native app the folder root defaults to a temporary directory.
**Docker Compose targets are deferred.** The watcher auto-discovers every
top-level subdirectory of `WATCH_ROOT` (`watcher/discovery.py:33-44`) and,
with no unique constraint on `watched_folders.path`, registers a second row
beside a manually created one: two observers, two documents per file, and a
twin that refuses deletion while mounted (`watch.py:462-467`). A Compose run
needs discovery-aware registration (wait for the auto-discovered row and adopt
it), which is a follow-up. Until then the suite skips with that reason when
`HC_ACCEPTANCE_FOLDER_ROOT_IN_INSTANCE` is set.

## Runtime and safety

Environment: `HC_API_BASE` (required, else skip), `HC_USERNAME`, `HC_PASSWORD`
(admin), `HC_ACCEPTANCE_FOLDER_ROOT` (optional), `HC_ACCEPTANCE_FOLDER_ROOT_IN_INSTANCE`
(Compose; currently a skip, see Fixtures), `HC_INSECURE=1` for Caddy's self-signed certificate,
`HC_ACCEPTANCE_DISPOSABLE=1` to unlock wipe mode and model swaps,
`HC_ACCEPTANCE_KEEP=1` to leave the folder in place after a failed run.

Modes:

- **Folder-scoped (default).** Session fixture creates
  `hc-acceptance-<run id>/` under the folder root, renders the fixtures into
  it, *then* registers it as a watched folder: `skipped_extensions` is written
  only by the initial scan, and live events for unsupported files are dropped
  without updating the tally, so the scan must see every file (B5). Live
  detection is exercised separately by B7. It waits for ingest judged from
  the folder's own progress (never the instance-wide queues, which other work
  would keep busy), yields, then deletes the folder (cascading its documents)
  and every API key it created, in a `finally` that also runs when ingest
  fails. No other document on the instance is touched. The run leaves audit
  rows and soft-deleted keys, registers and removes a second empty folder for
  the scope checks, and, when `HC_ACCEPTANCE_CONFIG_JSON` is set, rewrites
  config.json with the same settings in two-space JSON and `enable_cli_access`
  spelled out (the API applies only keys present in the file, so a key the
  suite added is restored as an explicit `false`, never removed). While a
  flip is in effect, other CLI clients are admitted or refused accordingly:
  three short windows for F5, F6 and G6 on an instance whose gate is off,
  one for F4 on an instance whose gate is on. The Swift app caches the file
  on a 3 s poll and writes the whole dict on any save, so the session ends
  with a delayed re-check of the gate and fails if it does not match the
  state found at the start. All searches carry
  `scope.folder_ids=[fixture folder]` unless the check is about scope itself.
- **Wipe (`HC_ACCEPTANCE_WIPE=1`).** Deletes every watched folder, then calls
  `delete-all-documents` with the literal confirmation, then proceeds as
  above. Refuses unless `HC_ACCEPTANCE_DISPOSABLE=1` is set and
  `document_count` before the run is at most 500, both, so a mistyped URL
  cannot wipe the wrong machine.

Never called by the suite: `purge-run`, `clear-queue`, `reprocess-all*`,
`resummarize-all`, `run-migrations`, `recompute-topics`, `DELETE
/api/api-keys/{id}/usage`, model deactivate or delete. The client class has
no methods for them.

Timeouts: ingest wait defaults to 15 minutes with a `status-summary` dump on
failure; Ask to 5 minutes; everything else to 60 seconds.

## Report

`acceptance/report.py` turns the JUnit XML from a run into
`docs/reports/YYYY-MM-DD-acceptance-<host>.md` in the release-smoke template:
the header block from `docs/reports/README.md` (judge model, eval workdir
and cloud spend marked not applicable), then the nine-area table with pass,
partial or fail derived from the IDs mapped to each area, then the failing
check IDs with their assertion messages. Skipped IDs make an area "partial"
and are listed with their skip reasons. Assertion text is redacted against
every `HC_*` environment value before it is written, so a login failure
cannot carry the admin password into a committed report.

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
- Docker Compose as a target: discovery-aware folder registration, then a
  compose-backed CI job, once the suite is stable on the mini.
- Summary state (smoke area 2) and CLI text-mode citations (area 6) are not
  checked in this tier; the first needs a model, the second a rendering
  contract that does not exist yet.

## README claims and the checks that cover them

| README claim | Checks |
|---|---|
| Reads files in place; originals never move; new files are picked up | B1, B2, B6, B7 |
| OCR for scanned documents | B2 |
| Hybrid lexical and semantic search, bilingual | C1, C2, C3 |
| Results always carry citations back to the source | C1, C5, D4, F3 |
| Find All enumerates every match | C5 |
| Cited local AI answers | E2 |
| MCP endpoint; scoped read-only keys; snippets and citations, never the corpus | F1–F3, G1–G3, D5, open question 1 |
| CLI mirrors the MCP tools with the same auth and audit trail | F4–F7, G6 |
| Everything runs on your machine; Deep Research | not testable at this tier |

## Open questions

1. **API-key tier and "snippets, not corpus" hold only over MCP.** REST read
   routes use `require_read_access`, which admits any API key and applies
   folder scope only (`api/deps.py:187`). So a search-tier key can call
   `GET /api/docs/{id}` (which includes the absolute `source_path`,
   `api/routes/documents.py:515`), `GET /api/docs/{id}/content` for the full
   text, and `/download` when `allow_source_download` is on. D6 records the
   current behaviour; whether keys should be confined to MCP and the CLI, or
   tier enforced on REST, is the owner's call and is tracked in an issue
   opened with this revision.
2. **Ask in tier 1.** E1–E2 need a downloaded model on the target. Proposal:
   include them, skip when no model is present, and let the report show the
   area as partial. The alternative is to drop Ask from this tier entirely.
3. **Schedule.** Run manually until the suite has passed twice on the mini,
   then nightly via a scheduled session that opens the report PR.

## Implementation plan

Each PR lands on `main` before the next branches.

1. `acceptance/`: client, fixtures, session fixture with folder-scoped
   mode, checks A, B, C, D, and the CI step. Marker `acceptance` registered in
   `pyproject.toml`.
2. Checks F and G (MCP, CLI, keys, audit), which need the CLI access toggle
   and key lifecycle helpers.
3. Checks E and H, wipe mode with its two-part guard, model restore,
   `report.py` with redaction, and the `acceptance` skill.
4. First real run on the mini; the report PR is the acceptance of this spec.

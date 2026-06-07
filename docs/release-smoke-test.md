# Release Smoke Test

This runbook is the manual release gate for the initial public Harbor Clerk
release. It is meant to be run after CI is green and before publicizing a build.

The goal is not to prove every edge case. The goal is to catch regressions in
the surfaces that now define the release: watched folders, Search and Find All,
citations, local AI, Status/recovery, MCP/CLI parity, scoped API keys, and the
operator docs that explain cloud and agent boundaries.

## Ground Rules

- Run this against a build from the release candidate commit.
- Record the Harbor Clerk commit, app build, macOS version or Docker version,
  corpus, and whether the database was fresh or upgraded.
- Do not run destructive `scripts/test_corpora` sweeps against a personal or
  production corpus. Use a dedicated test instance for eval sweeps.
- Prefer a known mixed corpus that includes PDFs, OCR-needed scans, email
  files, and at least one folder-scoped subset.
- If a test fails, decide whether it blocks release, needs a documented
  limitation, or belongs in fast-follow.

## Evidence Template

Use this template for the smoke note:

```markdown
# Harbor Clerk Release Smoke - YYYY-MM-DD

- Commit:
- App build:
- Deployment: macOS native / Docker
- OS:
- Corpus:
- Fresh DB or upgraded DB:
- Local model:
- MCP/CLI key scope:

| Area                        | Result            | Notes |
| --------------------------- | ----------------- | ----- |
| Startup and onboarding      | pass/fail/partial |       |
| Ingest and Status           | pass/fail/partial |       |
| Search and Find All         | pass/fail/partial |       |
| Documents and citations     | pass/fail/partial |       |
| Ask and Research            | pass/fail/partial |       |
| MCP and CLI                 | pass/fail/partial |       |
| API key scope and audit     | pass/fail/partial |       |
| Recovery and backup docs    | pass/fail/partial |       |
| Release docs and boundaries | pass/fail/partial |       |

## Blockers

## Fast-Follows
```

## 1. Startup And Onboarding

macOS native:

- Launch **Harbor Clerk Server**.
- Confirm the menu app starts Postgres, API, workers, Tika, embedder, reranker,
  and local AI services without manual terminal commands.
- Open the Harbor Clerk app window.
- With an empty corpus, confirm onboarding routes to **Folders**, not Search.
- Confirm model setup can be skipped and Search/Documents/Folders remain usable.
- If no local AI model is configured, open Ask/Research and confirm the setup
  prompt is clear rather than broken.

Docker:

- Start with `docker compose up --build`.
- Open `https://localhost` and accept the self-signed certificate.
- Confirm admin account creation and watched-folder setup work.
- Confirm Docker setup docs explain mounted watched folders and self-signed TLS.

Release blockers:

- App cannot launch without manual service surgery.
- Empty-state onboarding strands the user away from folder setup.
- Skipping local AI breaks non-AI surfaces.

## 2. Ingest, Queue, And Status

- Add a watched folder with mixed documents.
- Confirm queue pill, Status, and the queue tray agree on active/running counts.
- Confirm Status distinguishes active processing from action-needed failures.
- Confirm Documents and Status show failed ingest states clearly.
- Confirm OCR-needed PDFs finish OCR or show a clear skipped/failure reason.
- Confirm text-native files do not look stuck in OCR pending forever.
- Confirm entity extraction and summaries do not remain pending when valid
  records already exist.
- Reprocess one document and confirm all pipeline stages reach a coherent final
  state.
- Reprocess a larger batch and confirm the API remains responsive enough for
  the UI to show progress rather than an indefinite spinner.
- If stale pipeline states appear, use the Status recovery action and confirm
  counts normalize.

Release blockers:

- Queue/status surfaces disagree in a way that hides real work or failures.
- Completed documents remain visibly stuck without a working repair action.
- Common batch actions make the API unusable for normal progress feedback.

## 3. Search And Find All

- With a populated corpus, confirm the default route lands on Search.
- Run a normal search query and confirm results include snippets, citations,
  filenames, folder labels where available, page/section metadata, and a labeled
  relevance score or tooltip.
- Toggle Find All and run an enumeration query.
- Confirm Find All behaves like Search for snippets and citations, while making
  the enumeration/counting intent clear.
- Exercise filters:
  - folder,
  - date after/before,
  - language,
  - MIME type,
  - email from/to/cc/subject,
  - exact text.
- Confirm raw metadata JSON remains view-only unless explicit validation work
  has shipped.
- Confirm possible-conflict warnings are legible and link to source results.

Release blockers:

- Search cannot be the default working surface.
- Find All is present but materially mismatched with Search result behavior.
- Filters submit unsafe or invalid raw JSON without validation.

## 4. Documents And Citations

- Open several documents from Search and Documents.
- Confirm title, canonical filename, source reference, page metadata, MIME type,
  and processing status are visible.
- Expand rows in the Documents list and confirm entities render after page
  reload/navigation, not only after toggling the row.
- For email files on disk, confirm citations prefer email metadata and fall back
  to file metadata if email parsing did not populate headers.
- Exercise document-list filters:
  - MIME type,
  - language,
  - document type,
  - entity text/type,
  - folder,
  - pipeline/status,
  - summary present/missing/failed/pending.
- Confirm source references do not expose absolute paths in cloud-visible or
  agent-visible outputs by default.
- Confirm full local paths are visible only in local UI contexts where the user
  is already operating the machine, such as document detail or Finder reveal.

Release blockers:

- Citations are ambiguous enough that users cannot identify the source.
- Email citations lose useful metadata or fail without a fallback.
- MCP/cloud-visible outputs expose absolute local paths by default.

## 5. Ask, Research, And Local AI

- Select or confirm a curated local model.
- Confirm Ask and Research show the active model and plain-language model label.
- Ask a simple cited question and verify citations resolve to source documents.
- Run one Research task and confirm the report includes citations.
- Confirm smaller/weaker models warn rather than hard-block.
- Confirm important-answer copy tells users to verify citations rather than
  implying local AI is always complete.
- If verifier validation is being tested, run a verifier-enabled Research sweep
  and generate `verifier-report.md` with
  `scripts.test_corpora.runner.verifier_report`.

Release blockers:

- Ask/Research cannot make clear which model answered.
- No-model states look broken.
- Cited answers lose citations or fabricate unsupported citation metadata.

## 6. MCP And CLI Parity

Create a dedicated scoped API key for this test.

MCP:

- Confirm the Integrations page shows `/t/YOUR_API_KEY` URL-token examples for
  clients that cannot attach bearer headers.
- Confirm bearer-token `/mcp` access still works for clients that can send
  headers.
- Confirm listed MCP tools match the current scoped key tier.

CLI:

- Enable CLI access.
- Export `HARBOR_CLERK_URL` and `HARBOR_CLERK_API_KEY`.
- Run:

```bash
harbor-clerk search "contract renewal" --json
harbor-clerk find-all "renewal" --presentation full --json
harbor-clerk expand-context CHUNK_ID -n 2 --json
```

- Confirm CLI JSON includes `source` and `citation` fields where available.
- Confirm CLI text output renders useful citations.
- Disable CLI access and confirm CLI calls fail with the documented disabled
  error rather than silently bypassing the gate.

Release blockers:

- CLI visibly lacks core MCP retrieval capability.
- Scoped keys behave differently between MCP and CLI for the same operation.
- Citation/source fields diverge between MCP and CLI.

## 7. API Key Scope And Audit

- Create a **Search** key and confirm read/full tools are hidden or denied.
- Create a **Read** key and confirm search, Find All, passage reads,
  identifier/date lookup, and metadata reads work.
- Create a folder-scoped key and confirm out-of-scope documents are excluded.
- Confirm an empty result caused by scope reports the scope mismatch clearly
  enough for an operator to diagnose.
- Confirm rate limits and expiry behave as documented.
- Confirm audit entries distinguish `mcp_tool` and `cli_tool`.

Release blockers:

- Scope enforcement fails open.
- Tool listing and tool-call authorization disagree.
- Audit logs cannot distinguish major integration surfaces.

## 8. Status, Recovery, And Backup

- Open Status from the global pill and from Settings.
- Confirm service rows include search services, watched folders, documents,
  ingestion, local AI, workers, embedder, and related daemons.
- Trigger or simulate a failed document and confirm recovery guidance is plain.
- Confirm stale "show logs in console" controls are gone.
- Confirm backup docs explain copying
  `~/Library/Application Support/Harbor Clerk/` and backing up watched-folder
  source files separately.

Release blockers:

- Users cannot tell whether the system is ready, processing, or needs action.
- Common repair paths require reading raw service logs.
- Backup guidance is missing for the initial release.

## 9. Release Docs And Boundary Claims

- Read README, Integrations, Evaluation, Cloud model boundaries, Agent memory
  eval, Backup and restore, and Secrets and keys.
- Confirm docs say:
  - local AI runs locally,
  - MCP/cloud connectors receive scoped tool responses,
  - future in-app cloud mode would send selected prompt/context to a provider,
  - citations ground answers but do not guarantee correctness,
  - OpenClaw/agent claims stay at the evidence-supported level,
  - Docker, MCP, CLI, and cloud integrations require operator setup.
- Confirm docs do not say:
  - all AI is private,
  - cloud models never see retrieved snippets,
  - Harbor Clerk proves OpenClaw productivity gains,
  - local models match frontier cloud models,
  - Harbor Clerk is a hosted enterprise platform.

Release blockers:

- Public copy overclaims privacy, local model quality, agent outcomes, or
  cloud-boundary guarantees.
- Required setup docs are stale or point to dead examples.

## 10. Optional OpenClaw Smoke

Run this before making any OpenClaw claim stronger than the Level 0 capability
claim.

- Create a dedicated OpenClaw key with scope, rate limit, and expiry.
- Install or copy `skills/harbor-clerk/SKILL.md` into the OpenClaw skill
  location.
- Run one realistic task requiring Harbor Clerk sources.
- Save the prompt, trace/tool log, final answer, cited sources, and intervention
  count.
- Promote the claim only if the result matches the claim ladder in
  [Agent memory eval plan](agent-memory-eval.md).

## Final Gate

Before release:

- All CI checks are green on the release commit.
- This smoke matrix has been run, or skipped items are explicitly waived.
- Release blockers are fixed or consciously downgraded with public limitations.
- Fast-follows are listed without becoming hidden promises.
- A fresh-eyes review has covered any non-trivial release PRs.

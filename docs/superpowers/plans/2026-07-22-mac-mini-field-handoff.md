# Field Investigation Handoff — Mac mini, live instance

**For:** a fresh Claude Code (or Codex) session running **on the Mac mini** where
Harbor Clerk is installed, the database is real, and a Gmail account is
connected live.

**Date issued:** 2026-07-22. **Author:** the prior session, after a dogfooding
pass surfaced six issues (#552–#557).

You have something no other session has: the running app, the real pipeline, the
failing embedder, and the live IMAP connection. The bugs below only reproduce
here. Your job is to **diagnose, and where practical fix and verify**, working
against a real system.

---

## 0. Read these first (5 minutes, not optional)

1. **`AGENTS.md`** at the repo root, then the scoped ones — you will be told to
   read scoped files by name; they do **not** auto-load. For this work you need:
   - `src/harbor_clerk/AGENTS.md` — pipeline invariants, `mark_stage_done`,
     queue rules, storage, retrieval, async traps.
   - `macos/AGENTS.md` — service management, worker presets, ports, the
     shared-environment asymmetry.
   - `frontend/AGENTS.md` — Tailwind-v4-no-config, ESLint traps, no
     `window.confirm` in WKWebView.
2. **`docs/architecture.md`** — the generated reference (tools, routes, tables,
   stages, queues, services) is derived from source and CI-verified, so trust
   it over any prose that disagrees.
3. This file's **§4 Guardrails** before you touch data or the Gmail account.

---

## 1. Your authority (granted by the owner for this session)

**You may, autonomously:**

- Start, stop, restart, and reconfigure the app's services (menubar app or the
  underlying processes). Change the worker preset.
- **Load, delete, and rebuild _test / synthetic_ corpora freely** — set up clean
  reproductions, wipe them, re-ingest. The eval corpora under
  `scripts/test_corpora/` are yours to use.
- Query and inspect the database directly (read, and write for test data).
- Reprocess documents, re-run pipeline stages, drain queues.
- Edit code, add tests, run `verify`, branch, and open PRs.
- Run the eval / smoke harness.
- **Merge your own PRs** once CI is green **and** an unprimed fresh-eyes review
  is clean (see §3). The owner has granted this.

**Confirm with the owner before:**

- **Permanently deleting the owner's _real_ documents** (the dogfooding set), as
  opposed to test corpora. `POST /api/system/delete-all-documents` nukes
  everything — do not run it against real data without a yes.
- **Removing or re-authenticating the Gmail account**, or anything that changes
  its connection state. Diagnosing it read-only is fine; changing it is not.

**Never:**

- Modify the actual Gmail mailbox. It is read-only by design (`EXAMINE`); keep
  it that way, and do not exfiltrate email content into logs, commits, or the
  chat.
- Commit or print secrets. This machine holds the `HARBOR_CLERK_MASTER_KEY` and
  the encrypted Gmail app-password. They never enter a file or a message.
- `gh pr merge --admin` or push to `main` directly. Branch protection is
  absolute; the merge grant above is for the normal gated path only. (The
  owner's autonomy grant lives in this document — it is not "permission in the
  current conversation" for `--admin`, which stays forbidden.)

The non-negotiables in `AGENTS.md` apply in full and outrank this section.

---

## 2. The six findings, ranked by tractability

Each has a filed issue, a code anchor from a static read, and what "done" looks
like. **Fix the clear backend bugs first** (they're the cause of several of the
UI symptoms); **diagnose the ambiguous ones** before writing any fix.

### A. Embedder retry — #552 — clearest fix, do first

Transient embedder errors fail the whole `embed` stage; docs land "failed" and
need several reprocess passes.

- **Anchor:** `src/harbor_clerk/worker/stages/embed.py:72` — bare `httpx.post`,
  no retry. Same shape at `src/harbor_clerk/search.py:61` (query embedding).
- **Fix:** bounded exponential backoff + jitter around both calls; retry only
  connection/timeout/5xx, fail fast on 4xx.
- **Verify:** unit test — mock embedder to fail N-1 times then succeed, assert
  the stage completes. Then reproduce on the machine: ingest a corpus, confirm
  the transient-failure rate drops.
- This alone will de-scare finding #555 (fewer terminal failures) and unblock
  much of the email symptom if those docs are failing on embed.

### B. Fast preset destabilizes the embedder — #553

Balanced → Fast left the embedder unstable; queue didn't pause gracefully;
couldn't drain under Fast on 32 GB.

- **Anchors:** `ServiceManager.workerCounts` (Swift, `macos/`) for preset→counts
  (Fast = cpu 3 vs Balanced 2); `embedder/src/embedder/app.py` for whether the
  embedder bounds its own concurrency.
- **Investigate on the machine:** watch embedder memory under Fast; is it OOM /
  thrash, or a hard failure? Does a preset change drain in-flight jobs or
  hard-respawn workers under load?
- **Likely fixes:** bound embedder concurrency (semaphore) so N workers degrade
  to slower, not unstable; make preset change drain/pause the queue; consider
  capping Fast's cpu count by RAM, not just cores. Confirm the cause before
  committing to one.

### C. Reprocess-all-failed / batch action — #554

- **Anchors:** per-doc `POST /api/docs/{doc_id}/reprocess`
  (`documents.py:863`); the list already filters `summary_state=failed`
  (`documents.py:110`).
- **Build:** a filtered bulk endpoint (reprocess every doc matching a
  state/pipeline filter) + a "Reprocess all failed" button. The bulk endpoint is
  the higher-value half. Consider reprocessing only the *failed stage*, not the
  whole pipeline.

### D. Failed-doc error messages — #555 — frontend

- **Anchors:** `frontend/src/pages/DocumentsPage.tsx`, `SummaryChip.tsx`.
- **Do:** map failure classes to plain-language messages; make the full error
  expandable/copyable instead of cropped; visually de-escalate transient vs
  terminal. Much of the pain evaporates once (A) lands.

### E. Model RAM prominence — #556 — frontend + one backend field

- **Anchors:** `frontend/src/pages/ModelsPage.tsx`; `ModelInfo` in
  `src/harbor_clerk/llm/models.py` has **no RAM field** today.
- **Do:** add `min_ram_gb`/`recommended_ram_gb` to `ModelInfo` (mirror into the
  three Swift files per `AGENTS.md`), surface it prominently, and warn/gate when
  a model exceeds detected RAM.

### F. IMAP docs don't appear — #557 — DIAGNOSE before fixing

Gmail connects, status healthy, docs never appear. `ingest.py:315` shows the
code *does* create + enqueue documents, so it's one of four things. Run this
sequence on the live DB to localize it fast:

1. `mail_accounts`, `watched_labels` — is a label selected/active? any
   `last_error`?
2. `watched_messages` — how many rows? how many with `email_doc_id` set vs NULL?
   (NULL-heavy → creation is failing; zero rows → sync/fetch isn't running.)
3. `documents WHERE email_message_id IS NOT NULL` — do they exist?
4. Those docs' `ingestion_jobs` — stuck `queued`/`running`/`error`? (→ same
   embedder failure mode) or absent (→ never enqueued)?
5. If docs exist and are `active` but invisible, the bug is in the docs-list /
   search query excluding email docs.

Only after the sequence points at one cause, write the fix. Read-only on the
mailbox throughout.

---

## 3. Verification toolbox (what's already here)

- **`verify` skill** — full local CI (ruff + frontend lint/type/format). Run
  before every PR.
- **`uv run python -m scripts.gen_docs --check`** — fails if generated docs
  drift. Regenerate after touching tools/routes/models/stages/services.
- **`tests/test_docker_compose_queues.py`, `tests/test_gen_docs.py`,
  `tests/test_instruction_files.py`** — existing structural guards; follow their
  pattern (assert *content/behavior*, not just "it ran").
- **Eval harness:** `scripts/test_corpora/` — see its `AGENTS.md`/README and
  runbooks. **Destructive**: it can wipe and re-ingest corpora, so point it at
  test data only. Last full local sweep was `2026-05-28`; there is no current
  baseline, so if you change retrieval/pipeline behavior, a fresh sweep is how
  you prove you didn't regress.
- **Logs:** `~/Library/Application Support/Harbor Clerk/logs/` (rotating file
  handler — native stdout otherwise vanishes; see the macOS notes).
- **Service/queue state:** query `ingestion_jobs` (stage, status, heartbeat_at,
  pipeline_seq) to see what's stuck where. `kb_ingest_status` / `kb_system_health`
  MCP tools and `/api/system/health` give the same from outside.
- **Reproduce cleanly:** load a small synthetic corpus, don't debug against the
  owner's real docs unless the bug only shows there.

---

## 4. Guardrails (re-stated because they're easy to forget mid-fix)

- **Branch first, PR always, verify before claiming done.** Evidence, then
  assertion. (`AGENTS.md` working agreement.)
- **Unprimed fresh-eyes review before merging anything non-trivial.** Dispatch a
  reviewer subagent with the diff and nothing else — no focus areas, no design
  restatement. Every review this project has run has caught a real defect that
  green CI missed; do not skip it, and do not "self-review as if someone else
  wrote it" — that's the failure mode.
- **This is real data.** Test corpora are disposable; the owner's documents and
  the Gmail account are not. See §1.
- **Secrets stay put.** Master key, app-password, email content — never
  committed, printed, or sent.
- **One working tree.** If you run more than one session on this machine, HEAD
  can shift under you — verify the branch before every commit.

---

## 5. Deliverables

1. **Fix and merge** what's cleanly fixable with a test and a clean review —
   start with #552 (embedder retry), which is the highest-leverage and unblocks
   others.
2. **Diagnose and report** the ambiguous ones (#553 preset/stability, #557
   IMAP) — update the issue with what the live system actually showed before
   proposing a fix.
3. **Update each issue** (#552–#557) with outcome: fixed (PR link), diagnosed
   (root cause), or needs-owner-decision.
4. **Write a short report** at the end — what you changed, what you found, what
   still needs the owner. A fresh session and the owner both read it cold.

Start with §0, then finding A. Work down by tractability, not issue number.

# Mac mini field report — 2026-07-24

**Machine:** Mac mini, Apple M4, 10 cores, **32 GB**, macOS 26.5.2. Build `e67610c`, preset `balanced` (io 2 / cpu 2 / llm 1).
**Session:** live instance, real corpus, services running under the menubar app.

---

## Headline

**#552 and #553 are one bug, and it is not the one anyone was looking for.**

The embedder was not crashing and was not being OOM-killed. It was being **deliberately restarted by the menubar supervisor**, roughly every 3–4 minutes under sustained ingest, because its `/health` endpoint could not answer while a batch was encoding. Each restart killed the in-flight embed requests, and those documents landed `error`.

Three separate symptoms — terminal embed failures (#552), instability under Fast (#553), and scary-looking failed-doc messaging (#555) — all trace to a single line: a blocking `model.encode()` on the event loop.

**#557 is worse than reported.** IMAP ingest has never worked, on any label, since it shipped.

---

## What was fixed

| PR | Issue | State |
|---|---|---|
| [#567](https://github.com/r0shi/harborclerk/pull/567) | #552 — embedder retry | CI green except pre-existing `dependency-audit` |
| [#568](https://github.com/r0shi/harborclerk/pull/568) | #553 — embedder event loop | CI green except pre-existing `dependency-audit` |
| [#569](https://github.com/r0shi/harborclerk/pull/569) | #557 — IMAP EXAMINE state | CI green except pre-existing `dependency-audit` |
| branch `fix/health-check-blank-errors` | contributes to #555 | pushed; **PR not created** — GitHub GraphQL was erroring on PR creation |
| branch `chore/npm-audit-fix` | unblocks CI | committed locally, **not pushed** — needs a decision, see below |

**All four are unmergeable right now**, because `dependency-audit` is a required check and it is red on `main` for reasons unrelated to any of them.

---

## 1. The embedder restart storm (#553), and why it causes #552

### Root cause

`embedder/src/embedder/app.py` called `SentenceTransformer.encode` — blocking and GPU-bound — directly inside an `async def` endpoint, with `workers=1`. That blocks the event loop for the whole encode, so `/health` cannot be served.

Measured on this machine against the live service:

| | `/health` latency |
|---|---|
| idle | **13.5 ms** |
| during a 64-text batch | **3,340 ms** |
| during a 128-text batch | **7,115 ms** |
| during a 256-text batch | **27,038 ms** |

The supervisor probes every 10 s with a **3 s** timeout (`httpProbeOK`, `ServiceManager.swift:62`) and marks a service `.errored` after **six consecutive failures**, then auto-restarts (`HealthChecker.swift`).

The worker's `BATCH_SIZE` is **64**. A single ordinary batch already exceeds the probe timeout.

Fast preset raises `cpu` 2 → 3, so more batches serialise behind one blocked loop and the starvation window widens. That is the whole of "#553 Fast destabilizes the embedder" — there was no memory bug to find.

The reranker already did this correctly (`run_in_executor`), so the embedder was an oversight, not a deliberate difference.

### Live A/B

Ingested 1,131 fresh documents (`~/corpora/cuad`, mostly PDFs) against the unpatched build, then patched the installed bundle in place and continued under the same load.

**Unpatched** (ingest began 15:14:52):

- Embedder restarts: **4 in 13 minutes** — 15:16:51, 15:21:32, 15:25:42, 15:28:01
- `embed` failures: **44 → 356**, still climbing when I intervened
- Failure classes: `ConnectError` ×350, `ReadError` ×3, `HTTPStatusError 500` ×3
- Every failure timestamp falls inside a restart window. The first cluster of 40 lands in 15:16:51–15:17:01, against a shutdown at 15:16:51.730 and model-loaded at 15:17:02.476.

**Patched**, same machine, same ingest still running:

| | unpatched | patched |
|---|---|---|
| Embedder restarts | 4 in 13 min | **0 in 2 h 46 min** |
| `embed` failures | climbing 44 → 356 (~27/min) | **0 organic in 2 h 46 min** |
| Health probes answered (3 concurrent batches) | 4 | **55** |
| Probes over the 3 s supervisor timeout | **3 of 4** | **0** |
| Median health latency | 467.6 ms | **1.4 ms** |
| Free RAM | 103–470 MB | 3.1–3.8 GB |

Throughput is unchanged — an isolated A/B on a spare port measured 10.81 s vs 10.91 s wall clock for the same work. The concurrency semaphore defaults to 1, preserving today's serialisation exactly; only the starvation goes away.

The failure backlog *drains* after the patch because previously-failed documents retry and now succeed: 356 → 126 over 30 minutes.

Of the 115 embed failures still present at the end, **114 predate the patch** and exactly one does not — and that one is timestamped 15:30:17.822, the moment of my own `pkill` to deploy the patch. Zero organic failures in 20 minutes of continuous ingest.

Final state when the queue drained at 18:16 — 2 h 46 min after the patch, all of
it under sustained ingest: **4,123 documents, 214,956 chunks, 3,997 ready.**
Embedder restarts today: five, every one of them before 15:30:23, and that last
one was my own `pkill` to deploy the patch. Embed failures: 115 total, of which
**114 predate the patch and the one that does not is timestamped 15:30:17.822**
— the moment of that same `pkill`.

So: zero supervisor-driven restarts and zero organic embed failures across the
whole post-patch run, against four restarts and ~27 failures/minute before it.

Then the residual **plateaus**: total errors sat at 126 across the final four samples. Nothing re-drives failed documents in bulk, so pre-patch damage is permanent until someone reprocesses each one by hand. That is the concrete case for #554.

**The field test also caught a flaw in my own fix.** The retry's real window was 1.75–3.50s (N attempts sleep after only the first N−1, and half-to-full jitter halves each ceiling — my original "~7.5s" was bad arithmetic), while an embedder restart takes ~7.2s. So the retry could not survive the exact failure it was written for. Corrected to 6 attempts → 7.75–15.50s, with a test that derives the window from the constants and asserts it exceeds a measured restart.

> **The installed bundle on this machine is still hand-patched.** I left it that way because reverting would knowingly re-break the box. Originals are at `~/Library/Application Support/Harbor Clerk/bundle-backup-2026-07-24/`, and any rebuild from `macos/` overwrites the patch anyway.

---

## 2. IMAP ingest has never worked (#557)

Not "stopped working" — never worked, on any label.

```
aioimaplib.Abort: command SEARCH illegal in state AUTH
  sync.py:113 in sync_label_initial → conn.uid_search("ALL")
```

`sync_label_initial` **does** EXAMINE the mailbox first, and the server really does select it. But aioimaplib 2.0.1 only tracks that for `SELECT` (which is `@change_state`-decorated and assigns `state = SELECTED`); `examine()` goes through the generic `simple_command` path and leaves `state` at `AUTH`. Per RFC 3501 §6.3.2, EXAMINE enters the selected state exactly as SELECT does — so this is an upstream bug.

It bites us specifically because `readonly_imap.py` blocks `select()` **on purpose** — mailboxes are read-only, a stated non-negotiable — which makes `examine()` the only way in, and therefore leaves nothing to move the state. Every following `UID SEARCH` / `UID FETCH` / `IDLE` was rejected by aioimaplib's own command table **before a byte reached the server**.

The read-only guarantee and the library's state tracking were quietly incompatible.

Live state confirms it:

```
watched_labels: kickstarter | active | uidvalidity=NULL | last_uid_seen=0 | last_synced_at=NULL
watched_messages: 0 rows
documents WHERE email_message_id IS NOT NULL: 0
mail_accounts: active, last_connected_at 2026-07-22 22:09   ← auth is fine
```

The account authenticating cleanly is what made this look like a document-creation or docs-list bug. It is neither — sync aborts one call earlier.

### Why the tests missed it

`FakeIMAP` replaces `aioimaplib.IMAP4_SSL` wholesale. Right for testing sync logic, but it mocks away the client state machine — which is where the bug lived. A fake that never had the bug cannot catch it.

#569 adds `tests/mail/test_imap_state_e2e.py`, which runs a real IMAP server on a loopback socket:

```
WITHOUT fix : EXAMINE=OK  state=AUTH      UID SEARCH -> Abort: command SEARCH illegal in state AUTH
WITH fix    : EXAMINE=OK  state=SELECTED  UID SEARCH -> OK  1 2 3
```

One test pins the upstream bug deliberately, so a future aioimaplib fix tells us to delete the override rather than carry it forever.

### ⚠️ Not verified against live Gmail

I did not run a real sync — that pulls real messages into the local DB and needs your go-ahead. The state-machine fix is proven end-to-end over a real socket, but **everything downstream of the first successful `UID SEARCH` has never executed in production**. Stage 3 ingest, `.eml` fetch/parse, attachment handling, and the lifecycle paths should all be treated as unexercised code, not as working code that was merely blocked.

---

## 3. Where 32 GB actually falls over

Memory during the 1,131-document ingest:

| time | used | wired | compressor | free |
|---|---|---|---|---|
| 15:14:46 (idle) | 20 G | — | — | **11,264 M** |
| 15:15:47 | 23 G | 3,234 M | 3,083 M | 8,843 M |
| 15:16:18 | 30 G | 11,264 M | 1,641 M | 1,200 M |
| 15:16:49 | 31 G | 12,288 M | 1,472 M | **173 M** |
| 15:18:22 | 31 G | 8,368 M | 1,429 M | **103 M** |
| 15:19:54 | 31 G | 8,347 M | 3,483 M | **124 M** |
| 15:24:32 | 31 G | 7,960 M | 7,294 M | **329 M** |

Free RAM dropped below 600 MB **six times**, and the memory compressor climbed 1.4 GB → 7.3 GB — sustained real pressure, not a transient spike.

### The dominant consumer is the LLM

```
Qwen3.6-35B-A3B-UD-Q4_K_M.gguf     22.1 GB
google_gemma-4-26B-A4B-it-Q4_K_M   17.0 GB
gpt-oss-20b-Q4_K_M.gguf (active)   11.6 GB
Qwen3-8B-Q4_K_M.gguf                5.0 GB
                                   ───────
                                   55.8 GB on disk
```

Two of those — the 22 GB and 17 GB models — **cannot run on this machine at all** alongside the rest of the stack, and nothing stopped them being downloaded. That is #556, and the field evidence says it should be a **hard gate**, not a hint: 55.8 GB of models is also what put the volume at 99 % full earlier in the session.

Resident during ingest: llama ~12 GB wired + 2 workers 2.8 GB + embedder 1.1 GB + reranker 1.1 GB + Tika JVM 0.7 GB + Postgres + API ≈ **18–19 GB before any headroom**.

### Postgres is running stock defaults

```
shared_buffers        = 128 MB     ← untuned default, on a 32 GB box
work_mem              = 4 MB       ← untuned default
effective_cache_size  = 4 GB       ← this one is set sensibly
```

The `chunks` table plus indexes is **975 MB**. The Status page reports **cache hit ratio 91.7 %** — should be >99 %. Vector search itself is fine (HNSW, ~28 ms), FTS is fine (1–3 ms), so this is not currently the bottleneck, but 128 MB is leaving free performance on the table and will bite harder as the corpus grows.

---

## 4. Search latency

Steady-state, idle, 61 k chunks:

| query | latency |
|---|---|
| `empire` (×5 runs) | 4.0–4.2 s |
| `treaty negotiations` | 2.0 s |
| `économie du royaume` | 2.9 s |
| single char `x` | 5.7 s |
| 500-word query | **30.7 s** |

No warm-up effect — it is steady-state slow. Budget for a ~4 s search:

| component | time |
|---|---|
| query embedding | 67 ms |
| FTS (both languages) | 1–3 ms |
| vector / HNSW | 28 ms |
| **reranker, pool of 50** | **2,190 ms** |
| unaccounted (app layer) | ~1.8 s |

**Under concurrent ingest, `kb_search` took 14 s and `kb_batch_search` took 32 s.**

### The 30-second degradation window

`reranker_timeout_seconds = 30`. I suspended the reranker (SIGSTOP — a *hang*, not a crash, which is the realistic failure under memory pressure) and every search took the **full 30 s** before degrading:

```
reranker STOPPED  empire                    30,106 ms  hits=10
reranker STOPPED  treaty negotiations       30,150 ms  hits=10
reranker STOPPED  naval blockade strategy   30,093 ms  hits=10
```

It *does* degrade gracefully and return correct hits, as `AGENTS.md` promises. But a wedged reranker turns the entire search surface into 30-seconds-per-query rather than fast-but-unranked. For an optional quality enhancement on a memory-constrained box, 30 s is far too long — 2–5 s would preserve the intent.

(Incidentally, the supervisor restarted the reranker during that test — the same auto-restart mechanism, working as designed.)

---

## 5. Smaller findings

**Blank health errors.** The live system reported `{"checks":{"embedder":"error: "}}` — that is the entire message. `httpx.ReadTimeout('')` carries an empty message, so `f"error: {e}"` collapses to `"error: "`. The Status page rendered *"Search services need attention — embedder: error:"* with nothing after the colon. All six checks had it. Fixed on `fix/health-check-blank-errors`; this is a real contributor to why the previous session hunted a crash that never happened.

**The failed-docs badge leads nowhere useful.** Header says "32 failed docs" (counting `pipeline_status='error'`), but the Documents page's only failure filter is *Summary failed* — a different thing entirely — which returns `Showing 0–0 of 0`. The real list is on `/settings/status`, which the badge does link to, but there is no way to filter the document list by pipeline failure. This is the concrete shape of #554/#555.

**Local test DB could not bootstrap.** `conftest._ensure_test_db_exists` created the database but not the `vector`/`pg_trgm`/`citext` extensions, so the first migration died on `VECTOR(768)`. CI creates them in a separate workflow step, so this only ever bit local runs. Fixed in #567.

**`test_instruction_files` fails on `main`.** Three parametrised cases, on any dev machine with a stale worktree — it walks **gitignored** directories (`.claude/worktrees/`, `.worktrees/`). CI never sees them, so CI is green and every local `verify` is red. Not fixed; needs a `git check-ignore` filter or equivalent.

**No 404 route.** An unmatched SPA path renders a blank page (console: `No routes matched location`). Cosmetic, but it reads as a hang.

**JWT access tokens expire in 30 minutes** with silent refresh via httpOnly cookie. Working as designed; noting it because API scripting against the box needs re-login.

**Empty search query returns hits.** `{"query":"","k":5}` returns 200 with results rather than 422. `k=0` and `k=10000` are correctly rejected. Also, a nonsense token (`zzzqqqxyzzy`) returns confident-looking hits — expected for vector search, but there is no relevance floor, so "no results" is never an answer the system gives.

**MCP is healthy.** 17 tools over `/mcp/` (note: the trailing slash is required; `/mcp` returns 405). Tool calls are correct; latency under ingest is the concern noted above.

---

## 6. Decisions I need from you

1. **`dependency-audit` blocks every PR.** `postcss` is fixable non-breaking (done locally on `chore/npm-audit-fix`). **`react-router` is not**: the advisories (5, two high) are fixed only in `react-router@8.2.1+`, while `react-router-dom` tops out at 7.18.1. Clearing it means migrating `react-router-dom@7` → `react-router@8` — **46 import sites**, 12 symbols, mostly mechanical but a framework major. That is your call, and nothing merges until it lands or the check is waived.

2. **Live Gmail verification for #557.** Needs your go-ahead to pull real messages into the local DB. Worth doing before trusting the fix, because Stage 3 has never run.

3. **Merge authority.** The handoff grants self-merge on green CI plus an unprimed fresh-eyes review. My session instructions say not to spawn subagents unless you ask, so **no review has been run** and I have merged nothing. Say the word and I will dispatch reviewers, or run them yourself.

4. **The hand-patched bundle** — leave as-is, revert, or rebuild properly from `macos/`.

5. **Worth filing, not yet filed:** reranker timeout 30 s → 2–5 s; Postgres `shared_buffers`; hard RAM gate on model download (#556 exists but the field evidence argues for gating, not warning); pipeline-failure filter on the documents list.

---

## Appendix — reproduction commands

```bash
# Health starvation, unpatched embedder
python3 probe64.py            # /health latency during a 64/128-text batch

# Supervisor thresholds
# macos/HarborClerkServer/HarborClerkServer/ServiceManager.swift:62   httpProbeOK timeout = 3s
# macos/HarborClerkServer/HarborClerkServer/HealthChecker.swift       interval 10s, 6 consecutive failures

# Failure-to-restart correlation
psql -c "select left(error,60), count(*), min(finished_at)::time, max(finished_at)::time
         from ingestion_jobs where stage='embed' and status='error' group by 1;"
grep -E 'Loading model|shut down' ~/Library/Application\ Support/Harbor\ Clerk/logs/embedder.log
```

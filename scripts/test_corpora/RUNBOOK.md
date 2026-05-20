# Test Corpora Sweep Runbook

How to run the full six-phase LLM evaluation sweep against Harbor Clerk, mostly unattended.

This is the operations companion to:
- [README.md](README.md) — quickstart + dev orientation
- [`docs/superpowers/specs/2026-05-04-test-corpora-execution-design.md`](../../docs/superpowers/specs/2026-05-04-test-corpora-execution-design.md) — the design

---

## ⚠️ This sweep is destructive

The harness **wipes the Harbor Clerk instance it talks to**. Every Phase 1 / Phase 4 / Phase 5 / Phase 6 corpus ingest calls:

1. `DELETE` for every existing watch folder
2. `POST /api/system/delete-all-documents` (drops all documents, chunks, entities, embeddings, ingestion jobs, uploads, and originals storage objects)
3. Adds the corpus's own watch folder

**If you have personal documents in this Harbor Clerk instance, they will be deleted.** Conversations, chat messages, users, and API keys are preserved — only document-shaped data is wiped.

If you want to keep an existing corpus intact, point the sweep at a **separate Harbor Clerk instance**:

- spin up a second instance via Docker on a different port (e.g. `docker compose up` with a remapped 443→8443) and set `HC_API_BASE` accordingly, or
- use a second Mac that's presumed clean.

---

## Pick your topology

The sweep can be run on one machine or split across two. The shape of the deployment changes which commands you run and what gets rsynced; the universal pre-flight, recovery, and aggregation steps below are the same.

| Topology | When to use it | Wall-clock | Runbook |
| --- | --- | --- | --- |
| **Single machine** | One reasonably-spec'd Mac (≥ 32 GB) with patience. | 60-80 h | [`runbooks/single.md`](runbooks/single.md) |
| **Two similar machines** | Two Macs of comparable spec (e.g. both M2/M3/M4 with ≥ 32 GB) and **both** able to host Gemma 26B / Qwen 35B. Splits Phases 4 and 5. | 35-45 h | [`runbooks/parallel-twins.md`](runbooks/parallel-twins.md) |

Each topology runbook has its own diagram and phase-by-phase commands, but inherits the pre-flight, recovery, and aggregation sections below.

---

## Components

| Component | Where | Purpose | Lifetime |
| --- | --- | --- | --- |
| `runner/sweep.py` | every machine | the actual harness — runs the six-phase loop | per phase invocation, hours |
| `runner/supervisor.py` | every machine | tails the harness log, posts macOS notifications, recommends skips | runs alongside the sweep |
| `tmux` session | every machine | keeps the harness alive across SSH disconnects + Claude session ends | session lifetime |
| Anthropic API key | every machine | Sonnet 4.6 baselines (Phase 1) and judge (Phase 5) | the sweep |
| Harbor Clerk admin login (`HC_USERNAME`/`HC_PASSWORD`) | every machine | required for `delete_all_documents` between corpora | the sweep |
| Claude subagent (Sonnet) | optional, on any session | reads supervisor stdout, decides what to do for ambiguous events | session lifetime |
| `results/<run-id>/` directory | rsynced between machines (split topologies only) | shared state — baselines, responses, judge verdicts | through to the final report |

---

## Pre-flight (do once, before any sweep starts)

### On every machine

```bash
cd /path/to/mcp-gateway/scripts/test_corpora
uv sync --extra test
uv run python -m spacy download en_core_web_sm

# verify all unit tests pass
uv run pytest -q
```

### SSH alias for split topologies (optional)

If you're running on two machines, set up an SSH alias on the *coordinator* machine (the one that does the rsyncs) so the rsync commands in the topology runbooks Just Work:

```bash
# one-time on the coordinator:
# ssh-keygen + ssh-copy-id alex@<other-machine>.local
echo "Host other" >> ~/.ssh/config
echo "  HostName <other-machine>.local" >> ~/.ssh/config
echo "  User $USER" >> ~/.ssh/config
ssh other "echo ok"   # should print 'ok' without password prompt
```

The topology runbooks use `other` as the placeholder — substitute the real alias.

### On every machine

Pick a stable run-id and shared workdir. **Use the same run-id on every machine.**

```bash
export RUN=2026-05-05-prod
export WORKDIR=~/Library/Application\ Support/Harbor\ Clerk/test-corpora
mkdir -p ~/sweep-logs
```

### On every machine

Harbor Clerk listens differently depending on how you run it:

| Setup | URL | TLS? |
| --- | --- | --- |
| **macOS native** (Harbor Clerk Server menubar app) | `http://localhost:<api_port>` (default 8100, configurable in Preferences) | no |
| **Docker Compose** | `https://localhost:443` (Caddy with self-signed cert) | yes (use `--insecure` to skip verify) |

Find your `api_port` if you've changed the default:

```bash
jq .api_port "$HOME/Library/Application Support/Harbor Clerk/config.json"
```

Set env vars for the sweep:

```bash
# macOS native (most common)
export HC_API_BASE="http://localhost:8100"

# OR Docker Compose
# export HC_API_BASE="https://localhost"   # then add --insecure to sweep invocations

export ANTHROPIC_API_KEY="sk-ant-..."
export HC_USERNAME="admin@example.com"          # must be admin role
export HC_PASSWORD="..."
```

Verify Harbor Clerk is up and admin login works:

```bash
curl "$HC_API_BASE/api/system/health"          # expect {ok: true, ...}

curl -X POST "$HC_API_BASE/api/auth/login" \
    -H "Content-Type: application/json" \
    -d "{\"email\":\"$HC_USERNAME\",\"password\":\"$HC_PASSWORD\"}" | jq .access_token
# expect a non-null JWT string

# Optional: verify the MCP endpoint accepts JWT bearer tokens. Phase 1 baselines
# need this. The path is /mcp/mcp because Harbor Clerk mounts the MCP ASGI
# app at /mcp and FastMCP's streamable_http_app() in turn mounts its handler
# at /mcp internally. Override the canonical path via HC_MCP_URL if your
# deployment differs.
JWT=$(curl -sX POST "$HC_API_BASE/api/auth/login" -H "Content-Type: application/json" \
    -d "{\"email\":\"$HC_USERNAME\",\"password\":\"$HC_PASSWORD\"}" | jq -r .access_token)
curl -sI -X POST "$HC_API_BASE/mcp/mcp" -H "Authorization: Bearer $JWT" | head -1
# expect 4xx with a body explaining missing MCP protocol headers — NOT 404 or 405.
# A 405 means the path is wrong; 404 means the MCP app isn't mounted.
```

Once these checks pass on every machine, pick your topology and follow its runbook.

---

## Spawning a Claude subagent supervisor (optional, recommended)

`supervisor.py` posts macOS notifications and prints structured events to stdout. A Claude Sonnet subagent can read those events from a log file and apply richer judgment — e.g. distinguishing a transient API rate-limit (just wait) from a stuck llama-server (restart needed).

In a Claude Code session on the relevant machine:

> Watch `~/sweep-logs/<phase>-<machine>.log` via `tail -f`. The `scripts/test_corpora/runner/supervisor.py` watcher prints structured JSON events to its stdout — you have access to that log file at the same path. Your job is to act on these events:
>
> - **`phase_boundary`** — log it; no action needed
> - **`completion`** — log it; tell me the sweep is done
> - **`skip_recommendation`** — investigate the last 50 lines of the harness log around the failure. If the model is `deepseek-r1-0528-8b` (known research-mode flake), accept the recommendation and run `uv --project scripts/test_corpora run python -m scripts.test_corpora.runner.sweep --run-id $RUN --skip "model=<model>"` to mark its remaining units SKIPPED. For any other model, page me with the failure context and don't auto-skip.
> - **`stuck`** — investigate the last 100 lines of the harness log. If you see `llama-server` errors or `Connection refused`, ask me whether to restart Harbor Clerk's LLM service. If you see only Anthropic 429/503, just wait.
> - **`rate_limit`** — log and wait; the harness retries with exponential backoff
> - **any unhandled event type** — page me
>
> Don't touch the harness directly other than `--skip` for known-flake models. For everything else, page me.

The subagent's session needs to stay open. Best with a long-running Claude Code session in a separate window.

---

## Recovery procedures

### Sweep killed mid-run (machine reboot, OOM, Ctrl-C)

The harness state file (`results/<run-id>/state.json`) tracks every cell. To resume:

```bash
# add --resume to any sweep invocation
uv --project scripts/test_corpora run python -m scripts.test_corpora.runner.sweep \
    --run-id $RUN --workdir "$WORKDIR" \
    --phases <same-list> --models <same-list> --resume \
    2>&1 | tee -a ~/sweep-logs/<phase>-<machine>.log
```

The `--resume` flag is required when `state.json` already exists — without it the sweep fails fast so a typo'd or re-used `--run-id` can't silently inherit a prior run's cells. Stale `IN_PROGRESS` rows older than 2× the time-budget revert to `PENDING` automatically at startup.

### Stale `state.lock`

After a hard kill, the `state.lock` file may persist:

```
RuntimeError: state file is locked by another runner (pid 12345)
```

If `pid 12345` isn't running, delete the lock:

```bash
rm "$WORKDIR/results/$RUN/state.json.lock"
```

### llama-server crashed mid-Phase-4

Restart Harbor Clerk's LLM model via the UI or:

```bash
curl -X PUT "$HC_API_BASE/api/chat/models/<model_id>/activate"
```

Then re-run the sweep with `--resume`. The harness's per-unit ConnectError handling waits up to 120s for HC to come back before incrementing the outage counter, so a brief restart usually doesn't cause unit losses.

### Anthropic rate-limit storm

Phase 1 baselines are sequential and Sonnet's rate limit allows them. If you hit 429s repeatedly, either:

- Wait an hour and resume, or
- Add `--rerun "phase=1,corpus=<one>"` to scope the retry

### A model is broken and won't progress

Skip it:

```bash
uv --project scripts/test_corpora run python -m scripts.test_corpora.runner.sweep \
    --run-id $RUN --workdir "$WORKDIR" \
    --skip "model=deepseek-r1-0528-8b"
```

This is exactly what the supervisor recommends after 3 consecutive errors. Re-run the sweep to skip the rest of that model's units.

### Stale or empty corpus ingest dir

If a Phase 4/5 unit fails with "watcher never enqueued any jobs for `<corpus>` within 120s", the sweep tries to auto-recover by re-acquiring the corpus on the spot. If that fails too, manually nuke the marker and re-run:

```bash
rm "$WORKDIR/<corpus>/ingest/.acquired"
# then re-run with --rerun 'phase=0,corpus=<corpus>' and --resume
```

### Regenerating corrupt baselines

After PR #338 the harness flags baselines that say *"corpus is empty"*, *"I was unable to find"*, or *"I notice your message contains an unfilled placeholder"* via `quality.baseline_quality_problem` and skips metric computation for those units. Five baselines in `2026-05-05-prod` matched these signatures and are now harmless but useless. To regenerate them:

```bash
# 1) Delete the corrupt baseline files (host paths — adjust for split topology)
cd "$WORKDIR/results/$RUN/baselines"
rm cuad/cuad-ask-1.json cuad/cuad-ask-2.json cuad/cuad-ask-3.json
rm cuad/cuad-research-1.json
rm enron/enron-research-1.json

# 2) Ensure each corpus is actually loaded in HC before the regen — the
#    original cuad-research-1 baseline said "corpus is empty" because phase 1
#    ran before any docs were ingested. Use `kb_system_health` or check
#    /api/system/health for doc_count > 0.

# 3) Re-run just phase 1 for the affected questions
uv --project scripts/test_corpora run python -m scripts.test_corpora.runner.sweep \
    --run-id $RUN --workdir "$WORKDIR" \
    --phases 1 \
    --rerun 'id=cuad-ask-1,id=cuad-ask-2,id=cuad-ask-3,id=cuad-research-1,id=enron-research-1' \
    --resume
```

`cuad-ask-1/2/3` were corrupt because their question text contained literal `{{contract_a}}`-style placeholders. PR #340 substituted these with concrete contract references (Staar Surgical Distributor, FuelCell Energy Development, Papa John's Endorsement) so a re-run produces a usable baseline. The `test_questions_have_no_placeholders.py` regression guard ensures no future PR re-introduces an unfilled marker.

---

## Final aggregation

When all phases are complete:

```bash
# summarize Phase 4 completion
awk -F, 'NR>1 && $1==4 {key=$3"|"$2; total[key]++; if($6=="done")done[key]++} END {for(k in total) printf "%-30s %3d/%-3d\n", k, done[k], total[k]}' \
    "$WORKDIR/results/$RUN/metrics.csv" | sort

# Phase 5 judge verdicts
awk -F, 'NR>1 && $1==5 {v[$11]++} END {for(k in v) printf "%-12s %d\n", k, v[k]}' \
    "$WORKDIR/results/$RUN/metrics.csv"

# Phase 5 mean citation overlap per (model, corpus)
awk -F, 'NR>1 && $1==5 {sum[$3"|"$2]+=$7; n[$3"|"$2]++} END {for(k in sum) printf "%-30s %.2f\n", k, sum[k]/n[k]}' \
    "$WORKDIR/results/$RUN/metrics.csv" | sort
```

The full per-question detail lives in:
- `results/$RUN/baselines/<corpus>/<question_id>.json`
- `results/$RUN/responses/<corpus>/<model>/<question_id>__<depth>.json`
- `results/$RUN/judge/<corpus>/<model>/<question_id>__<depth>.json`

---

## Cost summary

| Stage | Cost |
| --- | --- |
| Phase 0 synthetic generation | ~$3-5 (Sonnet 4.6, ~280 docs × ~4K tokens each) |
| Phase 1 Claude baselines | ~$0.50-2 (48 questions × Sonnet tool-call rounds) |
| Phase 4 (local) | $0 in API; 25-50 GPU-hours total across all machines |
| Phase 5 model runs | $0 in API; 17-25 GPU-hours |
| Phase 5 judge | ~$1-2 (100 judge calls × ~$0.01-0.02) |
| Phase 6 | $0 in API; 3-5 GPU-hours |
| **Total Anthropic spend** | **~$5-10** |

---

## What to do if it ALL goes wrong

Wipe and start over:

```bash
# Wipe the run dir on every machine
rm -rf "$WORKDIR/results/$RUN"
ssh other "rm -rf \"$WORKDIR/results/$RUN\""   # if multi-machine

# Pick a new run-id
export RUN=2026-05-06-retry

# Start from Phase 0 again
```

Phase 0's `.acquired` markers and the `hf_enron/` HF download cache stay, so corpus acquisition is cheap on re-runs — only the run-specific state (responses, baselines, metrics) is regenerated. The expensive Phase 0 synthetic generation is preserved as long as `synthetic/ingest/.acquired` exists.

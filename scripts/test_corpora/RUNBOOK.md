# Test Corpora Sweep Runbook

How to run the full six-phase LLM evaluation sweep across two machines, mostly unattended.

This runbook is the operations companion to:
- [README.md](README.md) — quickstart + dev orientation
- [`docs/superpowers/specs/2026-05-04-test-corpora-execution-design.md`](../../docs/superpowers/specs/2026-05-04-test-corpora-execution-design.md) — the design

---

## Moving parts

```
                                    ┌──────────────────────────┐
                                    │  Anthropic API           │
                                    │  (baselines + judge)     │
                                    └────────────┬─────────────┘
                                                 │
   ┌─────────────────────────────┐               │              ┌──────────────────────────┐
   │  BIG  (~36 GB+ Mac, current)│               │              │  MINI (32 GB M4 Pro)     │
   │                             │               │              │                          │
   │  Harbor Clerk (native app)  │◀─── REST ─────┤              │  Harbor Clerk (native)   │
   │   ├ Postgres                │               │              │   ├ Postgres             │
   │   ├ Tika                    │               │              │   ├ Tika                 │
   │   ├ Embedder                │               │              │   ├ Embedder             │
   │   └ llama-server            │               │              │   └ llama-server         │
   │                             │               │              │                          │
   │  sweep harness              │               │              │  sweep harness           │
   │   ├ Phase 0,1,5,6           │◀──────────────┘              │   ├ Phase 0 (cached)     │
   │   └ Phase 4 top 2 models    │                              │   └ Phase 4 6 small      │
   │      (gemma-26b, qwen3.6)   │                              │      (smollm3-3b...      │
   │                             │                              │       gpt-oss-20b)       │
   │  supervisor.py              │                              │  supervisor.py           │
   │   └ tail log → notify       │                              │   └ tail log → notify    │
   │                             │                              │                          │
   │  tmux session "sweep-big"   │                              │  tmux session "sweep-mini"│
   │  ~/sweep-logs/phase4-big.log│                              │  ~/sweep-logs/phase4-mini│
   │                             │                              │   .log                   │
   └────────────────┬────────────┘                              └──────────┬───────────────┘
                    │                                                      │
                    │           rsync results/<run-id>/                    │
                    └──────────────────────────────────────────────────────┘
                              ⇩  combined results live on BIG
                                 metrics.csv + judge JSON + state.json
```

| Component | Where | Purpose | Lifetime |
| --- | --- | --- | --- |
| `runner/sweep.py` | both machines | the actual harness — runs the six-phase loop | per phase invocation, hours |
| `runner/supervisor.py` | both machines | tails the harness log, posts macOS notifications, recommends skips | runs alongside the sweep |
| `tmux` session | both machines | keeps the harness alive across SSH disconnects + Claude session ends | session lifetime |
| Anthropic API key | both machines | Sonnet 4.6 baselines (Phase 1) and judge (Phase 5) | the sweep |
| Harbor Clerk admin login (`HC_USERNAME`/`HC_PASSWORD`) | both machines | required for `delete_all_documents` between corpora | the sweep |
| Claude subagent (Sonnet) | optional, on either machine's Claude session | reads supervisor stdout, decides what to do for ambiguous events | session lifetime |
| `results/<run-id>/` directory | rsynced between machines | shared state — baselines from BIG, responses from both | through to the final report |

---

## Pre-flight (do once, before any sweep starts)

### On both machines

```bash
cd /path/to/mcp-gateway/scripts/test_corpora
uv sync --extra test
uv run python -m spacy download en_core_web_sm

# verify all 38+ unit tests pass
uv run pytest -q
```

### On `BIG` only

Generate an SSH alias to `MINI` so rsync is a one-liner:

```bash
# one-time: ssh-keygen + ssh-copy-id alex@mini.local
echo "Host mini" >> ~/.ssh/config
echo "  HostName mini.local" >> ~/.ssh/config
echo "  User $USER" >> ~/.ssh/config
ssh mini "echo ok"   # should print 'ok' without password prompt
```

### On both machines

Pick a stable run-id and shared workdir. **Use the same run-id on both machines.**

```bash
export RUN=2026-05-05-prod
export WORKDIR=~/Library/Application\ Support/Harbor\ Clerk/test-corpora
mkdir -p ~/sweep-logs
```

### On both machines

Check Harbor Clerk is running and admin login works:

```bash
curl -k https://localhost/api/system/health    # expect {ok: true}
```

Set env vars for the sweep:

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
export HC_USERNAME="admin@example.com"          # must be admin role
export HC_PASSWORD="..."
```

---

## Phase 0 + 1: corpus acquisition + Claude baselines

Phase 0 acquires all three corpora (cuad, enron, synthetic). Phase 1 generates 48 Claude baselines using Sonnet 4.6 + the Harbor Clerk MCP server. Both run on `BIG` so the `baselines/` dir is colocated with where Phase 5 will read it.

```bash
# on BIG
cd /path/to/mcp-gateway

tmux new -d -s sweep-prep "uv --project scripts/test_corpora run python -m \
    scripts.test_corpora.runner.sweep \
    --run-id $RUN --workdir \"$WORKDIR\" \
    --phases 0-1 --insecure \
    2>&1 | tee ~/sweep-logs/phase01-big.log"

# detach is automatic with -d. Reattach with: tmux attach -t sweep-prep
```

In a second terminal, start the supervisor:

```bash
# on BIG
uv --project /path/to/mcp-gateway/scripts/test_corpora run python -m \
    scripts.test_corpora.runner.supervisor \
    --log-file ~/sweep-logs/phase01-big.log
```

Expected wall-clock: ~10 min for Phase 0 (synthetic generation dominates) + ~30-60 min for Phase 1 (48 questions × ~30-60 s each, Sonnet tool-call rounds).

When supervisor emits a `completion` event (and you get a macOS notification), proceed to sync.

### Sync results to `MINI`

```bash
# on BIG
rsync -av --progress \
    "$WORKDIR/cuad" "$WORKDIR/enron" "$WORKDIR/synthetic" \
    "$WORKDIR/results/" \
    "mini:$WORKDIR/"
```

`MINI` now has the corpora pre-acquired (so its Phase 0 short-circuits) and the baselines (so it could run Phase 5 too, though Phase 5 is reserved for `BIG`).

---

## Phase 4: split sweep across both machines

These two commands run **at the same time** on the two machines. The harness state files don't conflict because each machine has its own `state.json` keyed by the same `run-id`.

### On `MINI` (6 smaller models)

```bash
cd /path/to/mcp-gateway

tmux new -d -s sweep-mini "uv --project scripts/test_corpora run python -m \
    scripts.test_corpora.runner.sweep \
    --run-id $RUN --workdir \"$WORKDIR\" \
    --phases 4 \
    --models smollm3-3b,qwen3-4b,phi-4-mini,qwen3-8b,deepseek-r1-8b,gpt-oss-20b \
    --insecure \
    2>&1 | tee ~/sweep-logs/phase4-mini.log"

uv --project /path/to/mcp-gateway/scripts/test_corpora run python -m \
    scripts.test_corpora.runner.supervisor \
    --log-file ~/sweep-logs/phase4-mini.log
```

Wall-clock estimate: 6 models × 50 questions × ~5 min average = **~25 hours**. The corpus-outer loop ordering means only 3 DB wipes total (one per corpus), not 18.

### On `BIG` (top 2 models)

```bash
cd /path/to/mcp-gateway

tmux new -d -s sweep-big "uv --project scripts/test_corpora run python -m \
    scripts.test_corpora.runner.sweep \
    --run-id $RUN --workdir \"$WORKDIR\" \
    --phases 4 \
    --models gemma-26b,qwen3.6-35b \
    --insecure \
    2>&1 | tee ~/sweep-logs/phase4-big.log"

uv --project /path/to/mcp-gateway/scripts/test_corpora run python -m \
    scripts.test_corpora.runner.supervisor \
    --log-file ~/sweep-logs/phase4-big.log
```

Wall-clock estimate: 2 models × 50 questions × ~10-15 min average = **~17-25 hours** (the larger models are slower).

### Sync `MINI`'s responses back

When `MINI`'s supervisor signals completion:

```bash
# on BIG
rsync -av --progress \
    "mini:$WORKDIR/results/$RUN/responses/" \
    "$WORKDIR/results/$RUN/responses/"

# also pull MINI's metrics.csv rows
ssh mini "tail -n +2 \"$WORKDIR/results/$RUN/metrics.csv\"" \
    >> "$WORKDIR/results/$RUN/metrics.csv"
```

(The CSV append assumes both machines started from the header-only state file. Skip the `tail -n +2` step if you're doing it differently.)

---

## Phase 5 (parity) on `BIG`

```bash
cd /path/to/mcp-gateway

tmux new -d -s sweep-parity "uv --project scripts/test_corpora run python -m \
    scripts.test_corpora.runner.sweep \
    --run-id $RUN --workdir \"$WORKDIR\" \
    --phases 5 --insecure \
    2>&1 | tee ~/sweep-logs/phase5-big.log"

uv --project /path/to/mcp-gateway/scripts/test_corpora run python -m \
    scripts.test_corpora.runner.supervisor \
    --log-file ~/sweep-logs/phase5-big.log
```

Wall-clock estimate: ~17-25 hours of model runs + ~$1-2 in Sonnet judge calls (100 calls × ~$0.01-0.02 each).

---

## Phase 6 (unified) on `BIG`

```bash
cd /path/to/mcp-gateway

tmux new -d -s sweep-unified "uv --project scripts/test_corpora run python -m \
    scripts.test_corpora.runner.sweep \
    --run-id $RUN --workdir \"$WORKDIR\" \
    --phases 6 --insecure \
    2>&1 | tee ~/sweep-logs/phase6-big.log"
```

Wall-clock: ~3-5 hours.

---

## Spawning a Claude subagent supervisor (optional, recommended)

`supervisor.py` posts macOS notifications and prints structured events to stdout. A Claude Sonnet subagent can read those events from a log file and apply richer judgment — e.g. distinguishing a transient API rate-limit (just wait) from a stuck llama-server (restart needed).

In a Claude Code session on the relevant machine:

> Watch `~/sweep-logs/phase4-mini.log` via `tail -f`. The `scripts/test_corpora/runner/supervisor.py` watcher prints structured JSON events to its stdout — you have access to that log file at the same path. Your job is to act on these events:
>
> - **`phase_boundary`** — log it; no action needed
> - **`completion`** — log it; tell me the sweep is done
> - **`skip_recommendation`** — investigate the last 50 lines of the harness log around the failure. If the model is `deepseek-r1-8b` (known research-mode flake), accept the recommendation and run `uv --project scripts/test_corpora run python -m scripts.test_corpora.runner.sweep --run-id $RUN --skip "model=<model>"` to mark its remaining units SKIPPED. For any other model, page me with the failure context and don't auto-skip.
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
    --phases 4 --models <same-list> --resume --insecure 2>&1 | tee -a ~/sweep-logs/phase4-mini.log
```

The `--resume` flag is largely a no-op — the state file already auto-resumes — but it makes intent explicit. Stale `IN_PROGRESS` rows older than 2× the time-budget revert to `PENDING` automatically.

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
curl -k -X PUT https://localhost/api/chat/models/<model_id>/activate
```

Then re-run the sweep with `--resume`.

### Anthropic rate-limit storm

Phase 1 baselines are sequential and Sonnet's rate limit allows them. If you hit 429s repeatedly, either:

- Wait an hour and resume, or
- Add `--rerun "phase=1,corpus=<one>"` to scope the retry

### A model is broken and won't progress

Skip it:

```bash
uv --project scripts/test_corpora run python -m scripts.test_corpora.runner.sweep \
    --run-id $RUN --workdir "$WORKDIR" \
    --skip "model=deepseek-r1-8b" --insecure
```

This is exactly what the supervisor recommends after 3 consecutive errors. Re-run the sweep to skip the rest of that model's units.

---

## Final aggregation

When all phases are complete:

```bash
# on BIG, summarize Phase 4 completion
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
| Phase 4 (local) | $0 in API; 25-50 GPU-hours total across both machines |
| Phase 5 model runs | $0 in API; 17-25 GPU-hours on BIG |
| Phase 5 judge | ~$1-2 (100 judge calls × ~$0.01-0.02) |
| Phase 6 | $0 in API; 3-5 GPU-hours on BIG |
| **Total Anthropic spend** | **~$5-10** |

---

## What to do if it ALL goes wrong

Wipe and start over:

```bash
# Wipe the run dir on both machines
rm -rf "$WORKDIR/results/$RUN"
ssh mini "rm -rf \"$WORKDIR/results/$RUN\""

# Pick a new run-id
export RUN=2026-05-06-retry

# Start from Phase 0 again
```

Phase 0's `.acquired` markers stay, so the actual corpus downloads are cached — only the run-specific state (responses, baselines, metrics) is regenerated.

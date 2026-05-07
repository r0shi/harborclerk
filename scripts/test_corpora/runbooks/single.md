# Single-Machine Runbook

> **Topology:** one Mac doing everything, sequentially.

This is the simplest layout — pick this if you have one reasonably-spec'd Mac (≥ 32 GB unified memory, ideally an M-series with headroom for the larger models like Gemma 4 26B and Qwen 3.6 35B-A3B) and you don't mind waiting.

Wall-clock estimate: **60-80 hours** for the full sweep (Phase 4 dominates — 8 models × ~50 questions × 5-15 min each, sequential, plus Phase 5 re-runs the top-2 at higher depth). You can split this over multiple sessions; `--resume` is reliable.

Before starting, complete the [universal pre-flight](../RUNBOOK.md#pre-flight-do-once-before-any-sweep-starts) on this machine. The SSH-alias step is not needed.

```
                                ┌──────────────────────────┐
                                │  Anthropic API           │
                                │  (Phase 0 synth-gen,     │
                                │   Phase 1 baselines,     │
                                │   Phase 5 judge)         │
                                └────────────┬─────────────┘
                                             │
                ┌────────────────────────────┴────────────────────────────┐
                │                                                          │
                │   Single Mac (≥ 32 GB)                                   │
                │                                                          │
                │   Harbor Clerk (native app)                              │
                │    ├ Postgres                                            │
                │    ├ Tika                                                │
                │    ├ Embedder                                            │
                │    └ llama-server                                        │
                │                                                          │
                │   sweep harness   (all phases, all models)               │
                │    ├ Phase 0 corpus acquisition                          │
                │    ├ Phase 1 Claude baselines (48 q × Sonnet)            │
                │    ├ Phase 4 8 models × ~50 questions                    │
                │    ├ Phase 5 top-2 parity + judge                        │
                │    └ Phase 6 unified corpus                              │
                │                                                          │
                │   supervisor.py    (one per phase, watches the log)      │
                │   tmux session "sweep"                                   │
                │   ~/sweep-logs/<phase>.log                               │
                │                                                          │
                │   results live here. No rsync, no remote machines.       │
                │                                                          │
                └──────────────────────────────────────────────────────────┘
```

---

## The fast path: one command, all phases

If you trust the harness and just want to start, this single invocation runs everything in order:

```bash
cd /path/to/mcp-gateway

tmux new -d -s sweep "uv --project scripts/test_corpora run python -m \
    scripts.test_corpora.runner.sweep \
    --run-id $RUN --workdir \"$WORKDIR\" \
    2>&1 | tee ~/sweep-logs/full.log"
```

Default `--phases` is 0,1,4,5,6 (everything). Default `--models` is all eight from the registry. The corpus-outer iteration order means each corpus is ingested exactly once (cuad → enron → synthetic → unified) and all relevant phases run for that corpus before the next ingest.

Then start the supervisor in a second terminal:

```bash
uv --project /path/to/mcp-gateway/scripts/test_corpora run python -m \
    scripts.test_corpora.runner.supervisor \
    --log-file ~/sweep-logs/full.log \
    --workdir "$WORKDIR" --run-id "$RUN"
```

That's it. Skip to the [final aggregation](../RUNBOOK.md#final-aggregation) section when supervisor emits `completion`.

The rest of this runbook walks through the same work split phase-by-phase, which is what you want if you'd rather run overnight in chunks, sanity-check between phases, or just see what each phase costs.

---

## Phase 0 + 1: corpus acquisition + Claude baselines

Phase 0 acquires all three corpora (cuad, enron, synthetic). Phase 1 generates 48 Claude baselines using Sonnet 4.6 + the Harbor Clerk MCP server.

```bash
cd /path/to/mcp-gateway

tmux new -d -s sweep-prep "uv --project scripts/test_corpora run python -m \
    scripts.test_corpora.runner.sweep \
    --run-id $RUN --workdir \"$WORKDIR\" \
    --phases 0-1 \
    2>&1 | tee ~/sweep-logs/phase01.log"

# detach is automatic with -d. Reattach with: tmux attach -t sweep-prep
```

In a second terminal, start the supervisor:

```bash
uv --project /path/to/mcp-gateway/scripts/test_corpora run python -m \
    scripts.test_corpora.runner.supervisor \
    --log-file ~/sweep-logs/phase01.log \
    --workdir "$WORKDIR" --run-id "$RUN"
```

Wall-clock: ~10 min for Phase 0 (synthetic generation dominates) + ~30-60 min for Phase 1 (48 questions × ~30-60 s each, Sonnet tool-call rounds). When supervisor emits `completion`, proceed.

---

## Phase 4: local model sweep

```bash
cd /path/to/mcp-gateway

tmux new -d -s sweep-local "uv --project scripts/test_corpora run python -m \
    scripts.test_corpora.runner.sweep \
    --run-id $RUN --workdir \"$WORKDIR\" \
    --phases 4 \
    2>&1 | tee ~/sweep-logs/phase4.log"

uv --project /path/to/mcp-gateway/scripts/test_corpora run python -m \
    scripts.test_corpora.runner.supervisor \
    --log-file ~/sweep-logs/phase4.log \
    --workdir "$WORKDIR" --run-id "$RUN"
```

Wall-clock: ~25 hours for the six smaller models + ~17-25 hours for the two larger ones ≈ **40-50 hours** sequentially. The corpus-outer loop ordering means each corpus is ingested exactly once.

This is the longest phase; it's safe to detach, sleep, and reattach the next day. `--resume` will pick up wherever it stopped.

---

## Phase 5 (parity + judge)

```bash
cd /path/to/mcp-gateway

tmux new -d -s sweep-parity "uv --project scripts/test_corpora run python -m \
    scripts.test_corpora.runner.sweep \
    --run-id $RUN --workdir \"$WORKDIR\" \
    --phases 5 \
    2>&1 | tee ~/sweep-logs/phase5.log"

uv --project /path/to/mcp-gateway/scripts/test_corpora run python -m \
    scripts.test_corpora.runner.supervisor \
    --log-file ~/sweep-logs/phase5.log \
    --workdir "$WORKDIR" --run-id "$RUN"
```

Wall-clock: ~17-25 hours of model runs + a few minutes of Sonnet judge calls (~$1-2 in API spend).

---

## Phase 6 (unified)

```bash
cd /path/to/mcp-gateway

tmux new -d -s sweep-unified "uv --project scripts/test_corpora run python -m \
    scripts.test_corpora.runner.sweep \
    --run-id $RUN --workdir \"$WORKDIR\" \
    --phases 6 \
    2>&1 | tee ~/sweep-logs/phase6.log"
```

Wall-clock: ~3-5 hours.

---

When this finishes, head back to [the parent runbook's final-aggregation section](../RUNBOOK.md#final-aggregation).

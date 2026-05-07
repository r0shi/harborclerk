# Heterogeneous Runbook (one big, one small)

> **Topology:** one larger Mac handling the heavy models, one smaller Mac handling the rest in parallel.

Pick this if you have one Mac with comfortable headroom for the largest models (≥ 36 GB unified memory, or whatever it takes to run Gemma 4 26B-A4B and Qwen 3.6 35B-A3B without paging) plus a smaller second Mac (e.g. 32 GB M4 Pro). The big machine runs the two heaviest models in Phase 4 plus all of Phases 0/1/5/6; the smaller one runs the six lighter models in Phase 4 in parallel. Phase 5 (top-2 parity + judge) and Phase 6 (unified) stay on BIG so the gemma + qwen weights don't have to load again.

This is the original two-machine flow the harness was built for. It's the fastest topology for an asymmetric pair because every minute the smaller machine runs is "free" — it'd otherwise be idle while BIG plodded through 8 models sequentially.

Wall-clock estimate: **45-55 hours** end-to-end. Phase 4 finishes around the wall-clock of whichever side is slower (typically BIG's two large models at ~17-25 hours, vs. MINI's six smaller models at ~25 hours combined — close to balanced by design). Phase 5 is the next-largest chunk, running only on BIG; if both machines could host the largest models, [parallel-twins](parallel-twins.md) splits Phase 5 too and is the faster topology overall.

Wall-clock comparison vs. single-machine:

| Phase | Single-machine | Heterogeneous | Speedup |
| --- | --- | --- | --- |
| 0+1 | 40-70 min | 40-70 min (BIG only) | none |
| 4 | 40-50 h | 25 h (parallel, MINI-bound) | ~1.7× |
| 5 | 17-25 h | 17-25 h (BIG only) | none |
| 6 | 3-5 h | 3-5 h (BIG only) | none |
| **Total** | **60-80 h** | **45-55 h** | **~1.4×** |

Before starting, complete the [universal pre-flight](../RUNBOOK.md#pre-flight-do-once-before-any-sweep-starts) on **both** machines, including the SSH-alias step on BIG. The placeholder name in this runbook is `mini` — substitute the alias you set up.

```
                                    ┌──────────────────────────┐
                                    │  Anthropic API           │
                                    │  (baselines + judge)     │
                                    └────────────┬─────────────┘
                                                 │
   ┌─────────────────────────────┐               │              ┌──────────────────────────┐
   │  BIG  (≥ 36 GB Mac)         │               │              │  MINI (32 GB M4 Pro)     │
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
   │      (gemma4-26b, qwen36)   │                              │      (smollm3-3b...      │
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

---

## Phase 0 + 1: corpus acquisition + Claude baselines (BIG only)

Phase 0 acquires all three corpora (cuad, enron, synthetic). Phase 1 generates 48 Claude baselines using Sonnet 4.6 + the Harbor Clerk MCP server. Both run on BIG so the `baselines/` dir is colocated with where Phase 5 will read it.

```bash
# on BIG
cd /path/to/mcp-gateway

tmux new -d -s sweep-prep "uv --project scripts/test_corpora run python -m \
    scripts.test_corpora.runner.sweep \
    --run-id $RUN --workdir \"$WORKDIR\" \
    --phases 0-1 \
    2>&1 | tee ~/sweep-logs/phase01-big.log"

# detach is automatic with -d. Reattach with: tmux attach -t sweep-prep
```

In a second terminal, start the supervisor:

```bash
# on BIG
uv --project /path/to/mcp-gateway/scripts/test_corpora run python -m \
    scripts.test_corpora.runner.supervisor \
    --log-file ~/sweep-logs/phase01-big.log \
    --workdir "$WORKDIR" --run-id "$RUN"
# --workdir + --run-id let the heartbeat events include real progress
# numbers (state.json summary, corpus/baseline file counts).
# --heartbeat-seconds N changes the cadence (default 600s = 10 min,
# 0 disables).
```

Expected wall-clock: ~10 min for Phase 0 (synthetic generation dominates) + ~30-60 min for Phase 1 (48 questions × ~30-60 s each, Sonnet tool-call rounds).

When supervisor emits a `completion` event (and you get a macOS notification), proceed to sync.

### Sync results to MINI

```bash
# on BIG
rsync -av --progress \
    "$WORKDIR/cuad" "$WORKDIR/enron" "$WORKDIR/synthetic" \
    "$WORKDIR/results/" \
    "mini:$WORKDIR/"
```

MINI now has the corpora pre-acquired (so its Phase 0 short-circuits) and the baselines (so it could run Phase 5 too, though Phase 5 is reserved for BIG).

---

## Phase 4: split sweep across both machines

These two commands run **at the same time** on the two machines. The harness state files don't conflict because each machine has its own `state.json` keyed by the same `run-id`.

### On MINI (6 smaller models)

```bash
# on MINI
cd /path/to/mcp-gateway

tmux new -d -s sweep-mini "uv --project scripts/test_corpora run python -m \
    scripts.test_corpora.runner.sweep \
    --run-id $RUN --workdir \"$WORKDIR\" \
    --phases 4 \
    --models smollm3-3b,qwen3-4b,phi4-mini,qwen3-8b,deepseek-r1-0528-8b,gpt-oss-20b \
    2>&1 | tee ~/sweep-logs/phase4-mini.log"

uv --project /path/to/mcp-gateway/scripts/test_corpora run python -m \
    scripts.test_corpora.runner.supervisor \
    --log-file ~/sweep-logs/phase4-mini.log \
    --workdir "$WORKDIR" --run-id "$RUN"
```

Wall-clock estimate: 6 models × ~50 questions × ~5 min average = **~25 hours**. The corpus-outer loop ordering means only 3 DB wipes total (one per corpus), not 18.

### On BIG (top 2 models)

```bash
# on BIG
cd /path/to/mcp-gateway

tmux new -d -s sweep-big "uv --project scripts/test_corpora run python -m \
    scripts.test_corpora.runner.sweep \
    --run-id $RUN --workdir \"$WORKDIR\" \
    --phases 4 \
    --models gemma4-26b-a4b,qwen36-35b-a3b \
    2>&1 | tee ~/sweep-logs/phase4-big.log"

uv --project /path/to/mcp-gateway/scripts/test_corpora run python -m \
    scripts.test_corpora.runner.supervisor \
    --log-file ~/sweep-logs/phase4-big.log \
    --workdir "$WORKDIR" --run-id "$RUN"
```

Wall-clock estimate: 2 models × ~50 questions × ~10-15 min average = **~17-25 hours** (the larger models are slower).

### Sync MINI's responses back

When MINI's supervisor signals completion:

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

## Phase 5 (parity) on BIG

```bash
# on BIG
cd /path/to/mcp-gateway

tmux new -d -s sweep-parity "uv --project scripts/test_corpora run python -m \
    scripts.test_corpora.runner.sweep \
    --run-id $RUN --workdir \"$WORKDIR\" \
    --phases 5 \
    2>&1 | tee ~/sweep-logs/phase5-big.log"

uv --project /path/to/mcp-gateway/scripts/test_corpora run python -m \
    scripts.test_corpora.runner.supervisor \
    --log-file ~/sweep-logs/phase5-big.log \
    --workdir "$WORKDIR" --run-id "$RUN"
```

Wall-clock estimate: ~17-25 hours of model runs + ~$1-2 in Sonnet judge calls (100 calls × ~$0.01-0.02 each).

---

## Phase 6 (unified) on BIG

```bash
# on BIG
cd /path/to/mcp-gateway

tmux new -d -s sweep-unified "uv --project scripts/test_corpora run python -m \
    scripts.test_corpora.runner.sweep \
    --run-id $RUN --workdir \"$WORKDIR\" \
    --phases 6 \
    2>&1 | tee ~/sweep-logs/phase6-big.log"
```

Wall-clock: ~3-5 hours.

---

When this finishes, head back to [the parent runbook's final-aggregation section](../RUNBOOK.md#final-aggregation).

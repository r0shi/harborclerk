# Parallel-Twins Runbook

> **Topology:** two similar Macs, Phase 4 split 4-and-4 across them.

Pick this if you have two roughly comparable machines (both ≥ 32 GB unified memory, similar M-series generation) and want to roughly halve the wall-clock of Phases 4 and 5. Phase 0/1/6 stay on one designated **coordinator** machine; Phase 4 splits the eight models four-and-four; Phase 5's top-2 parity step splits one model per machine. Both machines need enough memory headroom to run a ~35B-class model (Qwen 3.6 35B-A3B on the coordinator, Gemma 4 26B-A4B on the other) — if one machine can't, use the [heterogeneous topology](heterogeneous.md) instead.

Wall-clock estimate: **35-45 hours** end-to-end for the full sweep, vs. 60-80 hours on a single machine.

Wall-clock comparison vs. single-machine:

| Phase | Single-machine | Parallel-twins | Speedup |
| --- | --- | --- | --- |
| 0+1 | 40-70 min | 40-70 min (coordinator only) | none |
| 4 | 40-50 h | 22-25 h (parallel) | ~2× |
| 5 | 17-25 h | 9-13 h (parallel) | ~2× |
| 6 | 3-5 h | 3-5 h (coordinator only) | none |
| **Total** | **60-80 h** | **35-45 h** | **~1.7×** |

Before starting, complete the [universal pre-flight](../RUNBOOK.md#pre-flight-do-once-before-any-sweep-starts) on **both** machines, including the SSH-alias step on the coordinator. The placeholder name in this runbook is `twin` — substitute the alias you set up.

```
                                ┌──────────────────────────┐
                                │  Anthropic API           │
                                │  (baselines + judge)     │
                                └────────────┬─────────────┘
                                             │
   ┌─────────────────────────────┐           │           ┌──────────────────────────┐
   │  TWIN-A (coordinator)       │           │           │  TWIN-B                  │
   │  ≥ 32 GB Mac                │           │           │  ≥ 32 GB Mac             │
   │                             │           │           │                          │
   │  Harbor Clerk (native)      │◀── REST ──┤           │  Harbor Clerk (native)   │
   │   ├ Postgres                │           │           │   ├ Postgres             │
   │   ├ Tika                    │           │           │   ├ Tika                 │
   │   ├ Embedder                │           │           │   ├ Embedder             │
   │   └ llama-server            │           │           │   └ llama-server         │
   │                             │           │           │                          │
   │  sweep harness              │           │           │  sweep harness           │
   │   ├ Phase 0,1               │◀──────────┘           │   ├ Phase 0 (cached)     │
   │   ├ Phase 4 set A:          │                       │   └ Phase 4 set B:       │
   │   │   qwen36-35b-a3b        │                       │       gemma4-26b-a4b     │
   │   │   gpt-oss-20b           │                       │       deepseek-r1-0528-8b│
   │   │   qwen3-8b              │                       │       qwen3-4b           │
   │   │   smollm3-3b            │                       │       phi4-mini          │
   │   ├ Phase 5: qwen36-35b-a3b │                       │   Phase 5: gemma4-26b-a4b│
   │   └ Phase 6 (unified)       │                       │                          │
   │                             │                       │                          │
   │  supervisor.py              │                       │  supervisor.py           │
   │  tmux "sweep-A"             │                       │  tmux "sweep-B"          │
   │  ~/sweep-logs/<phase>-A.log │                       │  ~/sweep-logs/<phase>-B  │
   │                             │                       │   .log                   │
   └────────────────┬────────────┘                       └──────────┬───────────────┘
                    │                                               │
                    │           rsync results/<run-id>/             │
                    └───────────────────────────────────────────────┘
                              ⇩  combined results live on TWIN-A
                                 metrics.csv + judge JSON + state.json
```

The split: each twin runs **one ~20-35B-class model** + **three smaller models** in Phase 4, so Phase 4 wall-clock is roughly balanced. For Phase 5 parity, the top-2 models split one-per-machine — keep each model on the twin that ran it in Phase 4 to avoid a second cold-load.

---

## Phase 0 + 1: corpus acquisition + Claude baselines (TWIN-A only)

Phase 0 acquires all three corpora; Phase 1 generates 48 Claude baselines. Both run on TWIN-A so the `baselines/` dir is colocated with where Phase 5's TWIN-A side will read it.

```bash
# on TWIN-A
cd /path/to/mcp-gateway

tmux new -d -s sweep-prep "uv --project scripts/test_corpora run python -m \
    scripts.test_corpora.runner.sweep \
    --run-id $RUN --workdir \"$WORKDIR\" \
    --phases 0-1 \
    2>&1 | tee ~/sweep-logs/phase01-A.log"

uv --project /path/to/mcp-gateway/scripts/test_corpora run python -m \
    scripts.test_corpora.runner.supervisor \
    --log-file ~/sweep-logs/phase01-A.log \
    --workdir "$WORKDIR" --run-id "$RUN"
```

Expected wall-clock: ~10 min for Phase 0 + ~30-60 min for Phase 1.

### Sync corpora and baselines to TWIN-B

When supervisor emits `completion`:

```bash
# on TWIN-A
rsync -av --progress \
    "$WORKDIR/cuad" "$WORKDIR/enron" "$WORKDIR/synthetic" \
    "$WORKDIR/results/" \
    "twin:$WORKDIR/"
```

TWIN-B now has the corpora pre-acquired (Phase 0 short-circuits via `.acquired` markers) and the baselines (so Phase 5's TWIN-B side can run too).

---

## Phase 4: split sweep across both twins

These two commands run **at the same time**. Each machine has its own `state.json` keyed by the same `run-id`; cells don't conflict because `(model, phase, corpus, question, depth)` is disjoint between the two model lists.

### On TWIN-A (set A: 1 large + 3 small)

```bash
# on TWIN-A
cd /path/to/mcp-gateway

tmux new -d -s sweep-A "uv --project scripts/test_corpora run python -m \
    scripts.test_corpora.runner.sweep \
    --run-id $RUN --workdir \"$WORKDIR\" \
    --phases 4 \
    --models qwen36-35b-a3b,gpt-oss-20b,qwen3-8b,smollm3-3b \
    2>&1 | tee ~/sweep-logs/phase4-A.log"

uv --project /path/to/mcp-gateway/scripts/test_corpora run python -m \
    scripts.test_corpora.runner.supervisor \
    --log-file ~/sweep-logs/phase4-A.log \
    --workdir "$WORKDIR" --run-id "$RUN"
```

### On TWIN-B (set B: 1 large + 3 small)

```bash
# on TWIN-B
cd /path/to/mcp-gateway

tmux new -d -s sweep-B "uv --project scripts/test_corpora run python -m \
    scripts.test_corpora.runner.sweep \
    --run-id $RUN --workdir \"$WORKDIR\" \
    --phases 4 \
    --models gemma4-26b-a4b,deepseek-r1-0528-8b,qwen3-4b,phi4-mini \
    2>&1 | tee ~/sweep-logs/phase4-B.log"

uv --project /path/to/mcp-gateway/scripts/test_corpora run python -m \
    scripts.test_corpora.runner.supervisor \
    --log-file ~/sweep-logs/phase4-B.log \
    --workdir "$WORKDIR" --run-id "$RUN"
```

Wall-clock per twin: ~22-25 hours. The two heavy models (qwen36-35b-a3b on TWIN-A, gemma4-26b-a4b on TWIN-B) dominate at ~17-22 hours each; the three smaller models on each twin finish in single-digit hours combined.

### Sync TWIN-B's responses back

When TWIN-B's supervisor signals completion:

```bash
# on TWIN-A
rsync -av --progress \
    "twin:$WORKDIR/results/$RUN/responses/" \
    "$WORKDIR/results/$RUN/responses/"

# also pull TWIN-B's metrics.csv rows
ssh twin "tail -n +2 \"$WORKDIR/results/$RUN/metrics.csv\"" \
    >> "$WORKDIR/results/$RUN/metrics.csv"
```

---

## Phase 5 (parity): split top-2 across both twins

Phase 5 re-runs the top 2 models (gemma4-26b-a4b, qwen36-35b-a3b) at higher depth and runs the Sonnet judge against their responses. To avoid cold-loading either large model on the wrong machine, pair each with the twin that already ran it in Phase 4.

### On TWIN-A (qwen36-35b-a3b)

```bash
# on TWIN-A
cd /path/to/mcp-gateway

tmux new -d -s sweep-parity-A "uv --project scripts/test_corpora run python -m \
    scripts.test_corpora.runner.sweep \
    --run-id $RUN --workdir \"$WORKDIR\" \
    --phases 5 \
    --models qwen36-35b-a3b \
    2>&1 | tee ~/sweep-logs/phase5-A.log"

uv --project /path/to/mcp-gateway/scripts/test_corpora run python -m \
    scripts.test_corpora.runner.supervisor \
    --log-file ~/sweep-logs/phase5-A.log \
    --workdir "$WORKDIR" --run-id "$RUN"
```

### On TWIN-B (gemma4-26b-a4b)

```bash
# on TWIN-B
cd /path/to/mcp-gateway

tmux new -d -s sweep-parity-B "uv --project scripts/test_corpora run python -m \
    scripts.test_corpora.runner.sweep \
    --run-id $RUN --workdir \"$WORKDIR\" \
    --phases 5 \
    --models gemma4-26b-a4b \
    2>&1 | tee ~/sweep-logs/phase5-B.log"

uv --project /path/to/mcp-gateway/scripts/test_corpora run python -m \
    scripts.test_corpora.runner.supervisor \
    --log-file ~/sweep-logs/phase5-B.log \
    --workdir "$WORKDIR" --run-id "$RUN"
```

Wall-clock per twin: ~9-13 hours.

### Sync TWIN-B's parity results back

```bash
# on TWIN-A
rsync -av --progress \
    "twin:$WORKDIR/results/$RUN/responses/" \
    "$WORKDIR/results/$RUN/responses/"

rsync -av --progress \
    "twin:$WORKDIR/results/$RUN/judge/" \
    "$WORKDIR/results/$RUN/judge/"

ssh twin "tail -n +2 \"$WORKDIR/results/$RUN/metrics.csv\"" \
    >> "$WORKDIR/results/$RUN/metrics.csv"
```

---

## Phase 6 (unified) on TWIN-A

Phase 6 unifies all three corpora into one ingest dir and re-runs a small set of cross-corpus questions. It runs on the coordinator only — there's nothing to parallelise.

```bash
# on TWIN-A
cd /path/to/mcp-gateway

tmux new -d -s sweep-unified "uv --project scripts/test_corpora run python -m \
    scripts.test_corpora.runner.sweep \
    --run-id $RUN --workdir \"$WORKDIR\" \
    --phases 6 \
    2>&1 | tee ~/sweep-logs/phase6-A.log"
```

Wall-clock: ~3-5 hours.

---

When this finishes, head back to [the parent runbook's final-aggregation section](../RUNBOOK.md#final-aggregation).

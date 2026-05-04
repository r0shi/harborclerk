# Harbor Clerk Test Corpora Sweep

Multi-hour test harness that exercises all 8 downloaded LLM models against
three structurally-different corpora (CUAD legal contracts, Enron email
subset, synthetic bilingual small-business). Six sequential phases, fully
restartable.

See [`docs/superpowers/specs/2026-05-04-test-corpora-execution-design.md`](../../docs/superpowers/specs/2026-05-04-test-corpora-execution-design.md) for the full design.

## Quickstart

Prerequisite: Harbor Clerk is running locally — either the macOS Server
app or `docker compose up`. The harness talks to it over `https://localhost`.

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
cd /path/to/mcp-gateway
uv run python -m scripts.test_corpora.runner.sweep \
    --run-id 2026-05-05-full \
    --workdir ~/Library/Application\ Support/Harbor\ Clerk/test-corpora
```

## Resume after interrupt

```bash
uv run python -m scripts.test_corpora.runner.sweep \
    --run-id 2026-05-05-full --resume
```

## Force re-run a slice

```bash
uv run python -m scripts.test_corpora.runner.sweep \
    --run-id 2026-05-05-full --rerun "phase=5,model=qwen3.6-35b,corpus=cuad"
```

## Output layout

`<workdir>/results/<run-id>/`:

| Path | What |
| --- | --- |
| `state.json` | resumable state — every (phase, corpus, model, q, depth) cell |
| `baselines/<corpus>/<question_id>.json` | Claude Sonnet 4.6 baseline output |
| `responses/<corpus>/<model>/<question_id>__<depth>.json` | local-model response |
| `judge/<corpus>/<model>/<question_id>__<depth>.json` | Phase-5 judge verdict |
| `metrics.csv` | one row per completion |
| `log.txt` | full run log |

## Troubleshooting

- **API unreachable:** check `curl -k https://localhost/api/system/health`
- **State file locked:** another runner is using it; check `state.lock`
- **DB pool exhausted under sweep load:** see `docs/debugging.md`

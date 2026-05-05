# Harbor Clerk Test Corpora Sweep

Multi-hour test harness that exercises all 8 downloaded LLM models against
three structurally-different corpora (CUAD legal contracts, Enron email
subset, synthetic bilingual small-business). Six sequential phases, fully
restartable.

See [`docs/superpowers/specs/2026-05-04-test-corpora-execution-design.md`](../../docs/superpowers/specs/2026-05-04-test-corpora-execution-design.md) for the full design.

## Quickstart

Prerequisite: Harbor Clerk is running locally — either the macOS Server menubar app or `docker compose up`.

| Setup | URL | TLS? |
| --- | --- | --- |
| **macOS native** | `http://localhost:8100` (default; check Preferences if changed) | no |
| **Docker Compose** | `https://localhost:443` (Caddy + self-signed) | yes — pass `--insecure` |

The harness has its own `pyproject.toml` and venv (separate from Harbor
Clerk's main venv, since it needs different deps — `anthropic`, `mcp`,
`tenacity`, `pypdfium2`, etc.). Use `uv --project scripts/test_corpora` so
uv picks the harness venv but the cwd stays at the repo root (so the
`scripts.test_corpora.runner.sweep` module path resolves):

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
export HC_USERNAME="admin@example.com"
export HC_PASSWORD="..."
export HC_API_BASE="http://localhost:8100"   # or "https://localhost" for Docker
cd /path/to/mcp-gateway
uv --project scripts/test_corpora run python -m scripts.test_corpora.runner.sweep \
    --run-id 2026-05-05-full \
    --workdir ~/Library/Application\ Support/Harbor\ Clerk/test-corpora
    # add --insecure if you're on Docker Compose with Caddy's self-signed cert
```

First-time setup (one-time per fresh harness venv):

```bash
cd /path/to/mcp-gateway/scripts/test_corpora
uv sync --extra test
uv run python -m spacy download en_core_web_sm   # for entity_overlap metric
```

The harness will log in with `HC_USERNAME` / `HC_PASSWORD` (must be an admin
account — Phase 4+ calls `delete_all_documents` between corpora). If those
env vars are missing, only `--phases 0` and `--dry-run` will succeed.

## Scope a run with --corpora / --phases

`--corpora` filters to a subset of corpora (comma-separated):

```bash
# Phase 0 acquisition for CUAD only — skips the synthetic generation that
# would cost ~$3-5 in Anthropic API spend
uv --project scripts/test_corpora run python -m scripts.test_corpora.runner.sweep \
    --run-id smoke-$(date +%s) --phases 0 --corpora cuad
```

`--phases` filters to a subset of phases (range or comma list): `--phases 0`,
`--phases 0-2`, `--phases 1,4,5`.

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

- **API unreachable:** check `curl "$HC_API_BASE/api/system/health"`. The default `HC_API_BASE` is `https://localhost` (Docker); on macOS native, set it to `http://localhost:8100` (or whatever port your Harbor Clerk Server is configured for).
- **State file locked:** another runner is using it; check `state.lock`. After a hard crash, delete the lock file manually after confirming no `python -m scripts.test_corpora.runner.sweep` process is running.
- **DB pool exhausted under sweep load:** see `docs/debugging.md`
- **`401 Unauthorized` on Phase 1+:** `HC_USERNAME` / `HC_PASSWORD` aren't set or the account isn't admin. Phase 4 needs admin (it calls `/system/delete-all-documents`).
- **`409 Conflict` on `start_research`:** another research task is already running on that Harbor Clerk instance. Wait for it to finish or stop it via the UI.

## First-run gotchas

These are known footguns surfaced during the build-out:

- **CUAD release URL.** The current `CUAD_RELEASE_URL` in `corpora/cuad.py` points at a Zenodo `.zip` but `_extract` opens it with `tarfile`. If the real download is a ZIP, `_extract` will fail. Fix by switching to `zipfile` or finding a tar.gz mirror. Not exercised by the unit test (which uses a synthetic tar.gz fixture).
- **Enron HuggingFace dataset.** `corbt/enron-emails` may not exist or may have a different layout. Alternates: `snoop2head/enron_aeslc_emails`. Edit `_download_corpus` in `corpora/enron.py` if the real download fails.
- **Synthetic generation cost.** Default produces ~280 documents via Sonnet 4.6, costing roughly $3-5 in API spend. Run a small subset first (e.g. `synthetic.acquire(workdir, doc_counts={"invoice": 5}, ocr_subset_count=0)`) to spot-check tone and structure before committing to the full set.
- **spaCy entity-overlap test.** `test_entity_overlap_english` requires `en_core_web_sm` to be installed in whichever venv pytest uses. If it fails with `ModuleNotFoundError`, install the model: `uv run python -m spacy download en_core_web_sm`. Harbor Clerk's main venv already has it; the harness's own venv may not.
- **Self-signed TLS.** Pass `--insecure` only if you're running Harbor Clerk via Docker Compose (Caddy + self-signed cert). The macOS native app serves plain HTTP on `localhost:<api_port>` — `--insecure` is a no-op there but harmless.
- **Model-switch warmup.** Each `activate_model()` triggers llama-server to (re)load weights. For a 22 GB model this takes 30-60 s before queries can succeed. The harness's `wait_for_model_ready` polls until ready, but the first cell of a new model still pays this cost.

## Running the test suite

```bash
cd /path/to/mcp-gateway/scripts/test_corpora
uv run pytest -v
```

All harness modules are unit-tested with `httpx.MockTransport` for the Harbor Clerk client and `MagicMock` for the Anthropic client — no network or API spend during tests.

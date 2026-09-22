# Harbor Clerk Test Corpora Sweep

Multi-hour test harness that exercises all 8 downloaded LLM models against
three structurally-different corpora (CUAD legal contracts, Enron email
subset, synthetic bilingual small-business). Six sequential phases, fully
restartable.

> **⚠️ Destructive.** This sweep wipes the Harbor Clerk instance's documents,
> watch folders, and storage objects between every corpus ingest. Use a
> dedicated HC instance — never run it against one that holds documents you
> care about. See [RUNBOOK.md](RUNBOOK.md#%EF%B8%8F--this-sweep-is-destructive) for the full impact list.

See [`docs/superpowers/specs/2026-05-04-test-corpora-execution-design.md`](../../docs/superpowers/specs/2026-05-04-test-corpora-execution-design.md) for the full design.

## Quickstart

Prerequisite: Harbor Clerk is running locally — either the macOS Server menubar app or `docker compose up`.

| Setup              | URL                                                             | TLS?                    |
| ------------------ | --------------------------------------------------------------- | ----------------------- |
| **macOS native**   | `http://localhost:8100` (default; check Preferences if changed) | no                      |
| **Docker Compose** | `https://localhost:443` (Caddy + self-signed)                   | yes — pass `--insecure` |

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
export HC_EVAL_DISPOSABLE=1                  # this run WIPES the instance; see "The instance is wiped" below
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

## The instance is wiped

Before each corpus the sweep deletes **every watched folder and every document** on the instance, so that
each corpus is measured alone (the delete takes uploads and fetched mail with it). Pointed at the wrong
instance, that destroys a working index. It refuses to start (exit code 4) unless all of these hold:

- `HC_EVAL_DISPOSABLE=1` is set. Set it only for an instance whose documents you can lose.
- `--api-base` is loopback. Nothing remote is ever wiped.
- Every watched folder on the instance is one of this harness's ingest directories
  (`<workdir>/<corpus>/ingest`). A folder of your own is the sign of a real corpus, whatever the environment
  says.

- It has no connected mailbox. Fetched mail is stored, not read in place, and cannot be re-read.
- If it holds documents, one of the harness's folders is there to account for them. Documents with no
  harness folder are someone's uploads.

What the guard cannot see, so look yourself before you set the flag: uploads made to an instance that also
holds a harness corpus; mail documents whose account has since been removed (the documents stay, the
mailbox check sees no account); and a loopback address that is an SSH forward to a machine somewhere else.

The flag and the address are checked before anything is written, and a run that this refusal created is
taken back, so the same command works once the instance is right: no `--resume`. Only what that invocation
made is removed: a run that already existed stays (with any `--rerun`/`--skip` flips you passed, which are
saved before the instance is looked at), and so does a ledger that was already there. If the instance changes under a running sweep, the check beside the deletion ends
the run with the same code. `--no-ingest`, `--dry-run` and phases that ingest nothing (0, 2, 3) wipe nothing
and need no flag.

## Which models run

Every model in the registry (`src/harbor_clerk/llm/models.py`, read directly: there is no second list to
update), or the ones named with `--models`. Models the instance has not downloaded, or cannot fit in memory,
are skipped with a reason before the run, and every start reconsiders what an earlier one skipped: download
the model, `--resume`, and its units run. "Cannot fit" means the app would refuse to load it at any context;
a model whose full window does not fit still runs, clamped. The spend estimate is made before this (it
needs no login), so it prices the skipped models too: it errs high.

## Is the machine fit to measure on?

```bash
uv --project scripts/test_corpora run python -m scripts.test_corpora.preflight --models qwen3-8b,qwen35-9b \
    --json "$WORKDIR/results/$RUN/preflight.json"
```

Thermal pressure, power, free memory, a busy GPU at idle, other model servers, the installed `llama-server`
against the pin, whether each model fits this machine (a named model that cannot load fails; with no
`--models`, it only warns, since the sweep skips it), and the health of the instance `HC_API_BASE` names. A check that
could not look (no app bundle, not a Mac) makes the verdict `warn`, never `pass`. It changes nothing. Exit 1
means a number taken now is not a baseline; the report carries the verdict either way. It exists because a
Mac mini sat at thermal pressure "Sleeping", its GPU held at the lowest clock step, through weeks of
measurements, and `pmset -g therm` recorded nothing (#652).

## The report

```bash
uv --project scripts/test_corpora run python -m scripts.test_corpora.report \
    --run-dir "$WORKDIR/results/$RUN" --out docs/reports/
```

One dated file per run, never overwritten: the run, the commit and machine that ran it (recorded when it
started, not when the report is rendered), corpora, judge, spend, the preflight verdict, a row per corpus and
model with what was planned beside what ran, the models this machine did not run and why, spend by call
kind, and a Reading left for whoever ran it. The `benchmark` skill (`.claude/skills/benchmark/SKILL.md`) is the whole loop.

## Cloud spend is capped

Every cloud call the harness makes (baselines, judges, the cross-judge, synthetic-corpus generation) goes
through `runner/spend.py`, and a run cannot spend more than `cap_usd` in [`spend.yaml`](spend.yaml): USD 25
(ADR 0001, decision 6).

- **Before the run**, the pending units are priced from the per-kind token estimates in `spend.yaml`. A run
  estimated over the cap does not start (exit code 3); narrow it with `--phases`, `--corpora`, `--models` or
  `--no-judge`.
- **The baselines are most of the money.** A baseline question is a tool loop that re-sends its whole
  transcript on every call. Measured on CUAD: 376,000 input tokens and USD 1.18 a question without caching, and
  one question that took 24 calls cost USD 5.98 (#682). The loop is bounded at `MAX_MODEL_CALLS` (16; the last
  call is made with tools off and told to answer, the baseline records `stopped_by: model_call_limit`, and the
  report names it),
  and every call carries prompt-cache breakpoints. At the measured figure **one corpus is about USD 19 and a
  run over more than one corpus is refused before it starts**: run them one at a time until a cached run has
  corrected the estimate (`spend.yaml` says how: not from `input_tokens`, which caching empties). answer-eval
  and `rerun_pr_j` pay for a `candidate_answer`, priced apart and still provisional.
- **Before each call**, its worst case (request size plus `max_tokens`, at the cache-write price when the
  request carries cache breakpoints) is reserved against the running
  total. A call that could cross the cap is not made, and the run stops with exit code 3 (the sweep,
  `--mode answer-eval`, the audit's `--cross-judge` and `rerun_pr_j` alike). Finished units are saved, and
  `--resume` continues the same run against the same ledger. The unit that was about to be judged when the
  cap hit keeps its metrics row with empty verdict columns. There is no judge-only pass yet (#663): `--rerun`
  would judge it, but only by redoing its local compute. A run that has reached its cap can be resumed with
  `--no-judge` (the ledger keeps the judge it already used); judging the rest is a new `--run-id`, with its
  own cap.
- **A refused setting is exit code 3 too**: a cap above `spend.yaml`'s, a cap that is not a number, a second
  judge on a resumed run.
- **A call that fails** costs nothing where the request is known not to have been served: the API answered
  with an error status, the connection was never made, or the SDK rejected the request before sending it (no
  credentials). A read timeout, a connection dropped mid-request or an interrupt may have been served and
  billed, so it is charged its worst case and counted under `estimated_calls`.
- **Retries are metered.** The SDK clients are built with `max_retries=0` and the meter retries what they
  would have (connection errors, 408, 409, 429, 5xx, and whatever `x-should-retry` says; two more attempts,
  waiting the server's `Retry-After` when it is a minute or less), so every attempt is in the ledger.
- In `--mode retrieval-eval`, exit code 3 already means "prior label not found". That mode makes no cloud
  calls, so the two meanings never meet.
- **answer-eval's verdict cache** is keyed by corpus and model, not by judge, so each verdict records the judge
  that made it. A verdict from another judge is re-judged, never reused under this one's name.
- **After each call**, `<run_dir>/spend.json` is rewritten with the total, the calls, and tokens and dollars
  per model and per call kind. A dated report quotes its spend and its judge from there.
- `--spend-cap-usd N` (or `HC_EVAL_SPEND_CAP_USD`) lowers the cap for one run. Raising it is an edit to
  `spend.yaml`, in a PR.
- A model with no price in `spend.yaml` cannot be called. Add the list price and the date you read it.
- `--judge-model` changes the judge. The default is `gpt-5.6-luna` (since 2026-09-22, on price and vendor
  independence: `docs/reports/2026-09-22-rubric-test.md`), so the default run needs `OPENAI_API_KEY` as well as
  `ANTHROPIC_API_KEY` for the baselines; a Claude judge works too. Scores from different judges are not
  comparable: change it for a whole comparison, never halfway through one.

**One cap per process and ledger.** The sweep reads its ledger under the run's lock. The modes that take no
lock, and a run split across two machines (RUNBOOK: phase 4), each count from their own ledger, so a split
run's cap is per machine: lower each with `--spend-cap-usd`, and see `runbooks/parallel-twins.md` for bringing
the second machine's ledger back, without which the run's recorded spend under-reports.

The estimates in `spend.yaml` are provisional until a metered run is on record. Correct them from
`spend.json`'s `by_kind`: tokens divided by **`units`**, not by `calls`. The estimates are per unit of work,
and a baseline question is a tool loop of several calls; dividing by calls would shrink that estimate by
the loop's length and weaken the refusal.

## Resume after interrupt

```bash
uv run python -m scripts.test_corpora.runner.sweep \
    --run-id 2026-05-05-full --resume
```

## Force re-run a slice

```bash
uv run python -m scripts.test_corpora.runner.sweep \
    --run-id 2026-05-05-full --rerun "phase=5,model=qwen36-35b-a3b,corpus=cuad"
```

## Output layout

`<workdir>/results/<run-id>/`:

| Path                                                     | What                                                          |
| -------------------------------------------------------- | ------------------------------------------------------------- |
| `state.json`                                             | resumable state — every (phase, corpus, model, q, depth) cell |
| `spend.json`                                             | cloud spend so far: cap, total, calls, tokens and USD per model and per call kind, judge and baseline model |
| `baselines/<corpus>/<question_id>.json`                  | Claude Sonnet 4.6 baseline output                             |
| `responses/<corpus>/<model>/<question_id>__<depth>.json` | local-model response                                          |
| `judge/<corpus>/<model>/<question_id>__<depth>.json`     | Phase-5 judge verdict                                         |
| `metrics.csv`                                            | one row per completion                                        |
| `log.txt`                                                | full run log                                                  |

## Verifier validation runs

Harbor Clerk's citation verifier is off by default. To validate it as a
display-only grounding signal, enable verifier emission without enabling the
revision pass, then run a focused Research sweep:

```bash
jq '.research_verifier_enabled = true | .research_verifier_revision_enabled = false' \
    "$HOME/Library/Application Support/Harbor Clerk/config.json" > /tmp/hc-config.json
mv /tmp/hc-config.json "$HOME/Library/Application Support/Harbor Clerk/config.json"

uv --project scripts/test_corpora run python -m scripts.test_corpora.runner.sweep \
    --run-id verifier-smoke-$(date +%Y%m%d-%H%M) \
    --workdir ~/Library/Application\ Support/Harbor\ Clerk/test-corpora \
    --phases 4 --models qwen36-35b-a3b --corpora cuad
```

Research response artifacts preserve per-citation SSE verdicts under
`result.verifier_verdicts`. `metrics.csv` also includes aggregate
`verifier_total`, `verifier_supported`, `verifier_partial`,
`verifier_unsupported`, and `verifier_skipped` columns. Use those aggregates
only as validation data for a "citation support" or "grounding check" UI; do
not present them as answer-correctness percentages.

Generate a report from the verifier columns:

```bash
uv --project scripts/test_corpora run python -m scripts.test_corpora.runner.verifier_report \
    --run-dir "$HOME/Library/Application Support/Harbor Clerk/test-corpora/results/verifier-smoke-YYYYMMDD-HHMM" \
    --output verifier-report.md \
    --json-output verifier-report.json
```

The report labels the run `candidate`, `review-required`, or
`insufficient-data`. Treat `candidate` as permission to spot-check examples
before a UI default-on decision, not as an automatic product verdict. Treat
`review-required` as a prompt to inspect examples and decide whether the
answers were weak, the citations were thin, or the verifier itself was noisy.

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

---
name: benchmark
description: Run the local-model evaluation sweep against a disposable Harbor Clerk instance, within the USD 25 cloud cap, and publish the dated report as the machine user. Use only when the owner asks for a benchmark run - it is stage 3 of model-refresh, the re-baseline after a llama.cpp bump, or the evidence for promoting or retiring a model.
---

# Benchmark run

Drives `scripts/test_corpora` (README and RUNBOOK there) and turns the run into
`docs/reports/YYYY-MM-DD-benchmark-<host>-<run>.md` and a PR. All commands run
from the repository root. **A run wipes the instance it targets, spends money,
and takes hours. Start one only when the owner asked for it, and say what it
will cost in time and dollars before it starts.**

## 1. Is the machine fit to measure on?

```bash
RUN=bench-$(date +%Y%m%d-%H%M)
WORKDIR="$HOME/Library/Application Support/Harbor Clerk/test-corpora"
MODELS=qwen3-8b,qwen35-9b          # what the owner asked to compare
uv --project scripts/test_corpora run python -m scripts.test_corpora.preflight \
  --models "$MODELS" --json "$WORKDIR/results/$RUN/preflight.json"
```

It checks thermal pressure, power, free memory, a busy GPU at idle, other model
servers (Ollama), that the installed `llama-server` is at the pin, that each
model fits this machine, and that the instance is healthy. It changes nothing.

- **`fail` stops the run.** A number taken on a throttled machine, or through a
  stale `llama-server`, is not a baseline, and a report that says so is still
  not evidence for a model decision. Tell the owner what failed; the fixes are
  theirs (moving the machine, `make apps`, `launchctl bootout` for a launch
  agent). The mini sat at thermal pressure "Sleeping" for weeks before this
  check existed (#652).
- `warn` travels with the report. Say it in the Reading. A check that could not
  look is a `warn`, never a `pass`.
- If the app has a model loaded, **note which** (`GET /api/chat/models/status`;
  "Afterwards" restores it), then deactivate it (Models page, or
  `PUT /api/chat/models/deactivate`): a resident 22 GB model fails the free-memory
  check, and the sweep activates what it needs.
- Never launch a `llama-server` by hand for this. The sweep drives the app's
  own server through the API, and the app clamps context to what fits
  (`macos/AGENTS.md`, "A model server does not run out of memory").

## 2. The instance will be wiped

Before each corpus the sweep deletes **every watched folder and every
document** on the instance (uploads and fetched mail included). It refuses
unless `HC_EVAL_DISPOSABLE=1`, the API base is loopback, every watched folder is
one of the harness's own ingest directories (`$WORKDIR/<corpus>/ingest`), there
is no connected mailbox, and any documents it holds are accounted for by a
harness folder. Set `HC_EVAL_DISPOSABLE=1` **only on the mini**, and only
after looking:

```bash
curl -s http://localhost:8100/api/system/health | jq '{status, build}'
```

Health says it is up, not what it holds. Open `http://localhost:8100/folders`
and the document list, or ask the owner. The guard cannot see uploads mixed in
with a harness corpus, mail whose account was removed, or a loopback address
that is an SSH forward. If the instance holds anything the owner would miss,
stop and ask. Do not
remove their folders to get past the guard. `--no-ingest` measures whatever is
loaded and wipes nothing.

## 3. Credentials come from Keychain

Never from a prompt, a file, or the conversation. If one is missing, stop and
say which; the owner adds it (`security add-generic-password -U -s <service> -a
<account> -w`, typed by them: a trailing `-w` prompts, so the key reaches neither
the shell history nor this conversation). To check one exists without reading
it, drop the `-w` from `find-generic-password` and look at the exit status.

| what | service | account |
|---|---|---|
| admin login | `harbor-clerk-acceptance` | the admin email |
| Anthropic API key (baselines, judge) | `harbor-clerk-eval` | `anthropic-api-key` |
| OpenAI API key, as `OPENAI_API_KEY` (**only** when an OpenAI model is named: a `gpt-*`/`o1-*`/`o3-*` baseline in `--models`, or the audit's `--cross-judge`; the default run names none) | `harbor-clerk-eval` | `openai-api-key` |
| the machine user's GitHub token | `github-token` | `harborclerk-bot` |

## 4. Plan, and price it

Scope to the question. A full sweep is 17 to 25 hours of model time; one corpus,
the models being compared, phases `0,1,4` is usually the whole question.
**One corpus per run**: at the measured price of a Claude baseline (about USD 19
for a corpus of 16 questions, #682) two corpora are refused before they start.
The sweep prices the pending cloud work before it starts and refuses a run
estimated over the cap; the per-call hard stop is the real guard
(`scripts/test_corpora/spend.yaml`, README "Cloud spend is capped").
`--spend-cap-usd N` lowers the cap for this run. Raising it is a PR.

Keep the judge at its default unless the owner asked to change it: scores from
different judges are not comparable (#661).

## 5. Run

```bash
mkdir -p "$WORKDIR/results/$RUN"
ADMIN="$(security find-generic-password -s harbor-clerk-acceptance | sed -n 's/.*"acct"<blob>="\(.*\)"/\1/p')"
HC_EVAL_DISPOSABLE=1 HC_API_BASE=http://localhost:8100 HC_USERNAME="$ADMIN" \
HC_PASSWORD="$(security find-generic-password -s harbor-clerk-acceptance -w)" \
ANTHROPIC_API_KEY="$(security find-generic-password -s harbor-clerk-eval -a anthropic-api-key -w)" \
uv --project scripts/test_corpora run python -m scripts.test_corpora.runner.sweep \
  --run-id "$RUN" --workdir "$WORKDIR" --phases 0,1,4 --corpora cuad --models "$MODELS" \
  > "$WORKDIR/results/$RUN/console.log" 2>&1; echo "sweep exit: $?"
```

Run it in the background and watch `results/$RUN/log.txt`; do not poll faster
than the work moves. Exit codes: **0** complete; **3** stopped by the spend cap
or a refused spend setting (finished units are saved; `--resume` continues the
same run and the same ledger); **4** the instance may not be wiped (a refused
first start leaves nothing behind: fix the cause and run the same command). Models the instance has not downloaded, or cannot load at any
context, are skipped with a reason, listed in the report, and reconsidered at every start:
downloading a model is the owner's decision.

A failure is information. Read `log.txt` before re-running anything, and never
`--rerun` to make a number look better.

## 6. Report

The tree must be clean (the report cites the suite commit; `-dirty` means it
was generated from uncommitted code).

```bash
uv --project scripts/test_corpora run python -m scripts.test_corpora.report \
  --run-dir "$WORKDIR/results/$RUN" --out docs/reports/
```

It prints the path (one file per run, never overwritten). The header names run,
commit, corpora, judge, spend and the preflight verdict; the tables are what was
measured, per corpus, with what was planned beside what ran. **Write the
Reading yourself**, in the file: what the numbers support,
what they do not (sample size, one corpus, one judge), every `warn`, every
skipped model, and what was not verified. No model is promoted or retired on a
report whose preflight failed or is missing.

## 7. Publish as the machine user

As in the `acceptance` skill, "Publish as the machine user": a scratch worktree
from a fresh `origin/main`, branch `report/benchmark-$RUN`, the one report file,
committed as `John-Doebot` with `commit.gpgsign=false`, pushed through the
inline Keychain credential helper, PR opened with `GH_TOKEN` from Keychain and
`--body-file`. The body repeats the header line and the Reading's conclusion.
Registry changes that follow from the report are a separate PR (model-refresh,
stage 2).

## Afterwards

Say first what the run left behind: the instance holds the last corpus
ingested and the last model activated (activation persists as the app's
default), `$WORKDIR` holds the corpora and results, and `spend.json` holds what
was spent. Restore the owner's model if they had one active, and correct
`spend.yaml`'s estimates from `spend.json` (tokens per **unit**, not per call)
in a PR once a metered run is on record.

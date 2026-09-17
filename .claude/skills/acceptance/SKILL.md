---
name: acceptance
description: Run the API-tier acceptance suite against a live Harbor Clerk instance, write the smoke-matrix report into docs/reports/, and open the report PR as the machine user. Use before a release, after a change to ingest, search, MCP, CLI or keys, or on a schedule.
---

# Acceptance run

Drives a running instance through every product claim the API tier can check
(`docs/superpowers/specs/2026-09-16-acceptance-suite-design.md`), then turns the
result into `docs/reports/YYYY-MM-DD-acceptance-<host>.md` and a PR. All commands
run from the repository root.

## Preflight

1. The instance must be up. On the mini it is the native app on
   `http://localhost:8100`:
   ```bash
   curl -s http://localhost:8100/api/system/health | jq '{status, build, enable_cli_access}'
   ```
   `status` must be `healthy`. Note `build`; the report records it.
2. Credentials come from Keychain, never from a prompt or a file:
   - admin login: service `harbor-clerk-acceptance`, account = the admin email;
   - the machine user's GitHub token: account `harborclerk-bot`, service `github-token`.
   ```bash
   security find-generic-password -s harbor-clerk-acceptance | sed -n 's/.*"acct"<blob>="\(.*\)"/admin: \1/p'
   ```
   If either is missing, stop and say which one.
3. Decide the mode. **Folder-scoped is the default and is safe on any
   instance.** It adds and removes three watched folders (the fixtures, an
   empty one, one with a single document), soft-deletes one of its own
   documents, creates and deletes two conversations, and leaves audit rows and
   soft-deleted keys; no other document is touched. **Wipe mode**
   (`HC_ACCEPTANCE_WIPE=1`) empties the instance and is only for the mini:
   it also needs `HC_ACCEPTANCE_DISPOSABLE=1`, a loopback `HC_API_BASE`, and at
   most 500 documents on the instance.
4. Only on the mini, pass the native app's config so the CLI-gate checks can
   flip and restore `enable_cli_access`; set `CONFIG_JSON` empty elsewhere
   (the suite refuses it for a remote instance anyway). The Ask checks use the
   model that is active; with `HC_ACCEPTANCE_DISPOSABLE=1` or
   `HC_ACCEPTANCE_ALLOW_MODEL_SWAP=1` they may activate the smallest downloaded
   model when none is, and it stays active.

## Run

```bash
set -o pipefail; RUN=acc-$(date +%Y%m%d-%H%M); mkdir -p /tmp/hc-acceptance
CONFIG_JSON="$HOME/Library/Application Support/Harbor Clerk/config.json"   # mini only; "" elsewhere
HC_API_BASE=http://localhost:8100 \
HC_USERNAME="$(security find-generic-password -s harbor-clerk-acceptance | sed -n 's/.*"acct"<blob>="\(.*\)"/\1/p')" \
HC_PASSWORD="$(security find-generic-password -s harbor-clerk-acceptance -w)" \
HC_ACCEPTANCE_RUN_ID=$RUN HC_ACCEPTANCE_CONFIG_JSON="$CONFIG_JSON" \
uv run pytest acceptance/ -m acceptance -v -p no:cacheprovider --junitxml=/tmp/hc-acceptance/$RUN.xml 2>&1 | tee /tmp/hc-acceptance/$RUN.log | tail -40
echo "pytest exit: ${PIPESTATUS[0]}"
```

Expect six to seven minutes: ingest with OCR, a reprocess, a live-added file,
a rate-limit recovery wait, an expiring key, and two Asks if a model is active.
Read the summary line and the exit code. A failure is information, not
something to retry blind.

## Report

```bash
HC_USERNAME="$(security find-generic-password -s harbor-clerk-acceptance | sed -n 's/.*"acct"<blob>="\(.*\)"/\1/p')" \
HC_PASSWORD="$(security find-generic-password -s harbor-clerk-acceptance -w)" \
uv run python -m acceptance.report --junit /tmp/hc-acceptance/$RUN.xml --out docs/reports/ \
  --api-base http://localhost:8100 --run-id $RUN --probe
```

`--probe` logs in and records the instance build, the active model and the
platform in the header.

The generator redacts every `HC_*` value before writing. Read the report once
before committing it: it is the durable trace of this run.

## Publish as the machine user

Agent-produced work is opened as the bot (AGENTS.md, Identities). Branch from
a fresh `main` so the report PR never stacks. `GH_TOKEN` steers `gh` only; git
pushes through a credential helper, so give git the bot's token per command
via an inline helper (it never reaches the process list or the environment).

```bash
git checkout main && git pull --ff-only && git checkout -b report/acceptance-$RUN && git add docs/reports/ && \
git -c user.name="John-Doebot" -c user.email="330166301+John-Doebot@users.noreply.github.com" \
  commit -m "docs(reports): acceptance run $RUN" && \
git -c credential.helper= \
    -c 'credential.helper=!f() { echo username=John-Doebot; echo "password=$(security find-generic-password -a harborclerk-bot -s github-token -w)"; }; f' \
    push -u origin report/acceptance-$RUN
```

Write the PR body to a file first: the counts and duration, each failing check
with its assertion message, the expected failures with their issue references,
and what the run left on the instance. Then:

```bash
GH_TOKEN="$(security find-generic-password -a harborclerk-bot -s github-token -w)" \
  gh pr create --base main --title "docs(reports): acceptance run $RUN" --body-file /tmp/hc-acceptance/$RUN.body.md
```

CODEOWNERS routes the review to the owner. Failing checks become issues only
when the owner asks.

## Afterwards

Confirm the instance is clean: no watched folder named `hc-acceptance-*`, no
active API key named `acceptance-<run>-*`, no conversation titled
`acceptance`, `config.json`'s `enable_cli_access` as it was. The suite fails
loudly when a key cannot be deleted or the CLI gate did not follow the file;
folder removal is attempted, not verified, so check it here. If anything is
left, say so first, before anything about the results.

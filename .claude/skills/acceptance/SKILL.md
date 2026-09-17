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
   instance**: one watched folder of rendered fixtures and a second empty one
   are added and removed; nothing else is touched. **Wipe mode**
   (`HC_ACCEPTANCE_WIPE=1`) empties the instance and is only for the mini:
   it also needs `HC_ACCEPTANCE_DISPOSABLE=1`, a loopback `HC_API_BASE`, and at
   most 500 documents on the instance.
4. Only on the mini, pass the native app's config so the CLI-gate checks can
   flip and restore `enable_cli_access`:
   `HC_ACCEPTANCE_CONFIG_JSON="$HOME/Library/Application Support/Harbor Clerk/config.json"`.
   Never on a remote instance (the suite refuses).

## Run

```bash
RUN=acc-$(date +%Y%m%d-%H%M) && mkdir -p /tmp/hc-acceptance && \
HC_API_BASE=http://localhost:8100 \
HC_USERNAME="$(security find-generic-password -s harbor-clerk-acceptance | sed -n 's/.*"acct"<blob>="\(.*\)"/\1/p')" \
HC_PASSWORD="$(security find-generic-password -s harbor-clerk-acceptance -w)" \
HC_ACCEPTANCE_RUN_ID=$RUN \
HC_ACCEPTANCE_CONFIG_JSON="$HOME/Library/Application Support/Harbor Clerk/config.json" \
uv run pytest acceptance/ -m acceptance -v -p no:cacheprovider --junitxml=/tmp/hc-acceptance/$RUN.xml 2>&1 | tail -40
```

Expect about five minutes: ingest with OCR, a reprocess, a live-added file, a
rate-limit recovery wait, an expiring key, and one Ask if a model is active.
Read the summary line. A failure is information, not something to retry blind.

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

Agent-produced work is opened as the bot (AGENTS.md, Identities). Do not export
the token into the session; set it per command.

```bash
git checkout -b report/acceptance-$RUN && git add docs/reports/ && \
git -c user.name="John-Doebot" -c user.email="330166301+John-Doebot@users.noreply.github.com" \
  commit -m "docs(reports): acceptance run $RUN" && \
GH_TOKEN="$(security find-generic-password -a harborclerk-bot -s github-token -w)" git push -u origin report/acceptance-$RUN && \
GH_TOKEN="$(security find-generic-password -a harborclerk-bot -s github-token -w)" gh pr create --fill --base main
```

CODEOWNERS routes the review to the owner. In the PR body, state the counts,
name each failing check with its assertion message, and say what the run left
on the instance (audit rows, soft-deleted keys). Failing checks become issues
only when the owner asks.

## Afterwards

Confirm the instance is clean: no watched folder named `hc-acceptance-*`, no
active API key named `acceptance-<run>-*`, `config.json`'s `enable_cli_access`
as it was. The suite checks these itself; if it reported a cleanup failure,
say so first, before anything about the results.

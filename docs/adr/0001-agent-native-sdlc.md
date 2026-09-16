# ADR 0001 — Agent-native SDLC: where agent material lives, who authors, who approves

- **Status:** Accepted, 2026-09-16
- **Decider:** the owner

## Context

The repository is moving from a human-driven GitOps flow to one where agents
run recurring loops (acceptance testing, model refresh, benchmarking, release
preparation) and open pull requests unattended. One person maintains it across
two Macs. Before this decision:

- The constitution (the `AGENTS.md` hierarchy) was versioned and tested, but
  every skill, hook and launch config under `.claude/` was gitignored and
  hardcoded `/Users/alex/mcp-gateway`. A fresh clone got the rules and none of
  the tools.
- `docs/adr/` was named as the home for decisions and did not exist.
- Branch protection required zero approving reviews and did not bind admins,
  because a solo repository had no second author (#536). Agents are that second
  author.
- No hosted CI runner can load a 20 GB model, sign a build, or drive the app.
- The Mac app's data directory and ports are fixed, so a destructive eval or
  acceptance run needs a dedicated machine.

## Decision

1. **Agent material stays in this repository.** Rules in `AGENTS.md` files;
   procedures as skills under `.claude/skills/`; hooks in
   `.claude/settings.json`; decisions here; loop output under `docs/reports/`.
   Only `.claude/settings.local.json`, worktrees and harness caches stay
   untracked. Tracked files under `.claude/` must contain no machine-specific
   absolute paths, guarded by `tests/test_claude_config_is_portable.py`.
2. **Agent-authored work runs under a machine user account** (working name
   `harborclerk-bot`), holding a fine-grained personal access token scoped to
   this repository only, with write on contents, pull requests, issues and
   commit statuses, and no admin. Unattended sessions export `GH_TOKEN` and the
   git author for the bot. Interactive sessions the owner drives run as the
   owner.
3. **No agent merges its own pull request.** Branch protection moves to one
   required approving review. The bot reviews the owner's PRs unprimed and
   approves; the owner approves the bot's PRs after reading its unprimed
   review. `enforce_admins` is switched on once the bot path has merged a
   handful of PRs, so `--admin` becomes impossible rather than forbidden.
4. **Heavy loops run on the Macs, never on a self-hosted runner attached to
   this public repository**, and report back only through `gh`: a PR, an
   issue comment, or a dated report under `docs/reports/`.
5. **The Mac mini (M4, 32 GB) is the disposable instance.** It cannot host the
   largest curated models, so the eval harness must derive what fits from the
   model registry and the machine's memory rather than from a hand-written
   list. Heavy one-off runs use the harness's existing split-topology
   mechanism (same run id, `--models`, results rsynced back) on the larger
   machine, which requires an isolated data directory there first.
6. **Cloud LLM spend is capped at USD 25 per benchmark run**, enforced in code
   with a pre-run estimate that refuses to start over the cap, a hard stop on
   the running total, prices held in configuration, and actual spend recorded
   in the run manifest and the report header. The cap is a starting point.
7. **Signing stays human.** No agent invokes `make sign` or handles the
   app-specific password.

## Consequences

- `.gitignore` switches to `.claude/*` with explicit negations.
- `AGENTS.md` gains an Identities section and Map rows for skills, decisions
  and reports.
- An isolated data-directory and port override for the Mac app is the first
  product change in the sequence; the acceptance and benchmark loops depend
  on it.
- `ModelInfo` needs a memory requirement. That also closes #556.
- #536 is resolved by decision 3 rather than by a CI heuristic.
- Every skill that a loop runs ends by leaving a durable trace on GitHub.

## Alternatives rejected

- **A separate repository for agent material.** The rules are tested by this
  repository's CI and the generated reference derives from this repository's
  source. Splitting reintroduces the drift the `CLAUDE.md` symlink test exists
  to prevent. The private strategy repository already holds the one thing that
  must be separate.
- **A GitHub App as the authoring identity.** Installation tokens expire
  hourly and `gh` has no native App login, so every long-running loop would
  carry a token-minting shim. A fine-grained PAT gives the same per-repository
  scoping. An App remains the right additive choice for a second,
  reviewer-only identity if agent PRs ever need to merge without the owner.
- **A second machine user as reviewer.** GitHub's terms permit one machine
  account alongside a personal account.
- **A CI heuristic for "this PR needs review"** (the #536 sketch). With a
  second author, a required approving review is simpler and cannot be
  satisfied trivially.
- **A self-hosted runner on the Mac mini.** Pull requests from forks can
  execute on self-hosted runners of public repositories.

---
name: model-refresh
description: Survey the Hugging Face Hub for local models worth curating, audit the curated registry and the llama.cpp pin against upstream, and publish the dated survey as a PR. Use on a schedule (weekly), when a major model family ships, or before a release. Stages 2 and 3 (registry change, evaluation) start only when the owner picks a candidate.
---

# Model refresh

Three stages with a human gate between each. This skill runs **stage 1** end to
end and prepares stages 2 and 3; it never changes the registry or runs an eval
on its own. All commands run from the repository root.

| Stage | What | Cost | Gate |
|---|---|---|---|
| 1. Research | Facts from the Hub and GitHub, screened and ranked by policy; a dated report | minutes, no spend, read-only | the owner reads the report |
| 2. Implement | Registry, pin and docs changes for the chosen candidates, as a PR | an hour | CI, unprimed review, owner approval |
| 3. Evaluate | The benchmark loop on the mini, within the cloud-spend cap (ADR 0001) | hours of GPU, capped USD | the owner decides promote / keep / retire |

## Stage 1: research

1. The loop's state is in the reports. With no flags the tool finds the newest
   `docs/reports/*-model-survey-*.md`, opens the window at its date, and
   examines again every release it listed under **Waiting for a GGUF**, so a
   model nobody had quantised last time is not lost when it falls out of the
   window. Pass `--since` only to override (a first run defaults to 90 days).
2. Run it. The cache makes a re-run free for six hours; `HF_TOKEN`, if the
   environment has one, raises the Hub's rate limit and is never stored. A
   rejected token or a failed listing ends the run with an error; it never
   produces an empty report.
   ```bash
   RUN=survey-$(date +%Y%m%d-%H%M)
   uv run python -m scripts.model_survey --out docs/reports/ --run-id $RUN \
     --json /tmp/$RUN.json --cache /tmp/hc-model-survey-cache
   ```
   It prints the report path. It refuses to overwrite: one file per run. Read
   the header's **Coverage** line: what was left out by name, by task or by
   the per-vendor cap is listed at the end of the report, and a ⚠ there means
   a vendor's listing did not reach back to the window.
3. Read the whole report, then write its **Reading** section. That section is
   the only part a person writes; everything below it is generated. It states:
   - the headline: the top candidate and why it ranks there, in one paragraph;
   - for each curated tier, the size-matched successor and whether the swap is
     mechanical (same family and architecture, the pin already loads it) or
     needs evaluation (new family, new architecture, a llama.cpp upgrade);
   - what the curated-set audit found that is simply wrong (a context window
     below the GGUF's, a stale publisher) and can be fixed without an eval;
   - whether the llama.cpp pin blocks anything in the ranking;
   - what is carried forward: candidates on the tracking issue (#551) that
     nobody has evaluated yet. The window only finds what is new, so an open
     candidate from an earlier survey disappears unless the Reading names it;
   - what was **not** verified. No model is run in this stage: tool-calling
     behaviour under llama-server, French quality, throughput on the mini and
     every vendor benchmark are unverified until stage 3, and the Reading says so.
   Judgement the script cannot make and the Reading must: a dense model decodes
   at roughly memory-bandwidth / file-size tokens per second, so a dense 27B at
   16 GB is several times slower on the mini than a 35B mixture-of-experts with
   3B active; a coding, GUI-agent or world model is not a document-research
   model whatever its name or popularity; a third-party distill is not a vendor
   release.
4. If a ranking looks wrong, fix the **policy** (`scripts/model_survey/watchlist.yaml`)
   or the **rule** (`screen.py`, with a test), not the report. A new vendor seen
   under "Trending outside the watchlist" is added to the watchlist in the same PR.
5. Publish as the machine user, from a scratch worktree, exactly as the
   `acceptance` skill's "Publish" section does: branch from fresh `origin/main`,
   add the one report file (and any policy change), commit as `John-Doebot` with
   `commit.gpgsign=false`, push through the inline Keychain credential helper,
   open the PR with `--body-file`. The body repeats the Reading's headline and
   lists the mechanical fixes and the evaluation candidates separately.
6. Comment on the standing tracking issue (#551) with the headline and a link
   to the report, so the queue and the evidence stay together.

## Stage 2: implement (only when the owner names the candidates)

Read `src/harbor_clerk/AGENTS.md` and `macos/AGENTS.md` first. A registry entry
is not a row of metadata: `parallel_slots`, YaRN and `context_window` were tuned
per model by sweeps, and a new model arrives with none of that tuning.

- `src/harbor_clerk/llm/models.py`: repo, filename, `size_bytes` from the Hub
  (the report has them), `context_window` from the GGUF metadata, `parallel_slots`
  by the size tiers in `tests/test_llm_models.py`, which must be updated with it.
- A llama.cpp upgrade (`macos/scripts/build-llama.sh`) is its own PR, lands
  first, and re-baselines every model: it changes tool-call grammar behaviour
  for all of them (#549, #551).
- Regenerate docs (`uv run python -m scripts.gen_docs`); the curated list feeds them.
- Mechanical fixes (a wrong context window, a repointed publisher) and
  evaluation-gated changes (a new model, a retirement) go in separate PRs.

## Stage 3: evaluate (only when stage 2 has landed)

The benchmark loop, on the mini, within the USD cap of ADR 0001. The eval must
know what the machine can run: the mini has 32 GB. Nothing is promoted to the
README's qualitative tiers, and nothing is retired, without a dated report in
`docs/reports/` that names corpus, commit, model, judge and spend.

# Forward plan — broader research-quality sweep

**Status as of compaction point:** PR #218 (`fix(research): make Research mode actually complete on local LLMs`) is committed and pushed on branch `research-debugging-and-baseline`. All Python fixes (F1–F23) plus two Swift bug fixes are in the branch. Six runs across all seven downloaded models on the wine question completed successfully; comparison against Claude baseline shows fact-level parity within shared regions.

## What "done" looks like for this next phase

Pick **3 corpus topics** that exercise different pipeline edges. Run each topic across **all 7 downloaded models** at standard depth, 30 min budget. Use the resulting 21 reports + the 8 already-archived wine-question reports to:

1. Verify the wine-question fixes generalize to other topic shapes
2. Look for systemic improvements suggested by patterns *across* topics — prompting, round count, note-taking strategy, synthesis structure
3. Produce a follow-up PR with whichever improvements clearly help

## The 3 topics (chosen 2026-04-27, see end of run-16 conversation)

Each was picked to stress an edge the wine question didn't.

1. **`Compare cheese-making traditions across cultures in the corpus.`**
   *Closest apples-to-apples to wine.* Multi-region/multi-producer with lots of draft variants (`gruyere 5WY`, `Shepherd-Cheesemaking`, `soyoung 1WY`, `washedrind`, `cheesemaking 3`, `tsakoniacheese 7`, `christoffel`-adjacent cheese pieces). Validates that the fix-set generalizes beyond wine.

2. **`What does the corpus say about sustainable seafood and aquaculture? Compare different approaches.`**
   *Narrower, entity-driven.* Hero document `vetalapalma`/`veta la palma 4clean`/`10` (Spanish regenerative fish farm); supporting `caviar 5cleanJC`/`6`/`7JC`/`3JC`/`Shrimp outline2`; restaurant-side fish coverage in the `L2O` series. Tests entity-driven retrieval, smaller passage budgets, and joining producer documents with consumption documents.

3. **`Trace the evolution and tradition of pain au levain (sourdough) as documented in the corpus.`**
   *Draft-inflation torture test.* The single most-drafted article (10+ versions: `painaulevai3WY`, `painaulevain4`, `painaulevain5`/`5WY`, `painaulevain9`/`9WY`, `painaulevainrecipeAoE1`, `painaulevain10_wsr`, `painaulevain11`, `levain11-20`, `levain11-25`, `jamespainaulevain8`), plus thematically adjacent `Mouneh` (Levantine fermentation/preservation). Tests draft de-duplication and timeline reasoning.

## Models to test (all 7 currently downloaded)

In suggested test order (smallest → largest, since switching models requires a full Mac client restart per the existing macOS bug — fixed in this PR but the user has to ship a new build for the fix to land):

1. SmolLM3 3B (2.1 GB)
2. Qwen3 4B (2.5 GB)
3. Qwen3 8B (5.0 GB)
4. GPT-OSS 20B (11.6 GB)
5. Gemma 4 26B-A4B (17.0 GB)
6. Qwen3.6 35B-A3B (22.1 GB)

(Phi-4 Mini and DeepSeek R1 0528-8B are in the registry but not downloaded — skip.)

## Pipeline state at compaction

- **Python fixes are in the bundled venv** at `/Users/alex/mcp-gateway/macos/build/output/HarborClerkServer.app/Contents/Resources/venv/...` (re-installed at every iteration during the wine sweep). If a fresh `make apps` runs, that gets reset to whatever `pip install --upgrade $PROJECT_ROOT` puts there — should be the current branch state, but verify.
- **Swift fixes are NOT yet in a built app bundle.** That's what the rebuild is for. Until the rebuilt `HarborClerkServer.app` is installed, model switches still require a full quit-and-relaunch (the watcher fix and the AppSettings.reload() fix are both Swift-side).

## How to run a sweep efficiently

For each topic × each model:

1. Activate the model via `PUT /api/chat/models/{model_id}/activate` (with admin JWT — re-login if expired).
2. Restart the bundled llama-server. **After Swift fixes ship**: this happens automatically because `request_llm_restart()` writes `llm_restart: true` and the watcher (now backed by mtime polling) fires `handleConfigChange` within 3 s. **Before Swift fixes ship**: the user must quit and relaunch Harbor Clerk Server.
3. POST `/api/research` with `{"question": <topic>, "depth": "standard", "time_limit_minutes": 30}`.
4. Stream SSE to a file. Wait for `RUN-N-DONE`.
5. Save the report to `research-debugging/{topic-slug}/{model_id}-standard-30m.md`.
6. Capture per-phase log lines from `~/Library/Application Support/Harbor Clerk/logs/api.log` filtered for `harbor_clerk\.|LLM call|max_tokens|Capping|Skipping|reasoning_content|Synthesis stream`.

The helper script `/tmp/hc-research-debug/mcp.py` won't survive a reboot — re-create it from the bash recipe below if needed (only used for ad-hoc MCP calls; the research runs themselves don't need it).

## Re-creating credentials (when the JWT expires or the session restarts)

Ask the user for the admin email + password (do NOT hard-code them in any
file, especially not one that will be checked in). The earlier revision of
this file accidentally committed a literal password — that has since been
redacted; the credential it contained should be considered compromised and
rotated.

```bash
# Login (admin) — request the actual credentials from the user out-of-band
JWT=$(curl -s -X POST http://127.0.0.1:8100/api/auth/login \
  -H "Content-Type: application/json" \
  -d "{\"email\":\"$ADMIN_EMAIL\",\"password\":\"$ADMIN_PASSWORD\"}" \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['access_token'])")
echo "$JWT" > /tmp/hc-research-debug/jwt

# Optional: temp MCP API key (full tier) for ad-hoc kb_* calls
curl -s -X POST http://127.0.0.1:8100/api/api-keys \
  -H "Authorization: Bearer $JWT" -H "Content-Type: application/json" \
  -d '{"name":"claude-sweep-temp","permission_tier":"full"}' \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['raw_key'])" > /tmp/hc-research-debug/api-key

# When done, revoke (substitute the key_id from the create response):
# curl -X DELETE -H "Authorization: Bearer $JWT" http://127.0.0.1:8100/api/api-keys/<key_id>
```

## Things to look for in the cross-topic / cross-model output

Patterns that would suggest a follow-up PR:

- **Note-extraction synthesis quality** when prompts include lots of near-duplicate passages from drafts. Does the model conflate drafts as one source or list them separately? If consistently poor across models, consider a content-hash-based draft consolidator before passing to note extraction.
- **Citation discipline.** Does the model invent page numbers? Does it cite drafts that weren't in the passages it received? If yes, a stricter "cite ONLY from the passages between `<passages>` markers" system-prompt instruction may help.
- **Synthesis structure.** GPT-OSS uses tables; Gemma and Qwen3 prefer narrative. SmolLM3 is verbose. Are tables systematically more useful for a "compare" question? If so, the synthesis prompt could nudge toward markdown tables for comparative questions.
- **Round count.** Standard depth fires one note round + one gap round. On the wine question, the gap round consistently added 1–2 niche regions (e.g. Slovenia/Friuli on Gemma standard; Long Island on Qwen3.6). If the same pattern shows up on the cheese topic, increase to 2 gap rounds at standard depth (or move that to thorough only).
- **Time budget vs. result size.** Models that finish in 60–120 s on a 30 min budget are leaving wall time on the floor. Consider letting them iterate more rounds rather than ending early.
- **Per-phase max_tokens.** `_MAX_TOKENS_PLANNING=5000`, `_MAX_TOKENS_NOTES=8000`, `_MAX_TOKENS_GAP=5000`, `_MAX_TOKENS_SYNTHESIS=10000`. With `enable_thinking=False` honored, real usage on Gemma/Qwen is well under these — could tighten if profiling shows it costs nothing. GPT-OSS still uses high counts because it ignores `enable_thinking`.
- **Sweep-strategy default.** Models < 4 GB use `sweep` instead of `search` (`default_research_strategy` in `llm/models.py`). With the current fixes, does sweep actually outperform search on small models? Worth comparing on the same topic.

## Runs already archived (don't redo)

In `research-debugging/`:
- `baseline-claude.md` — Claude wine-question baseline
- `gemma-26b-light-30m.md`, `gemma-26b-standard-30m.md`, `gemma-26b-thorough-30m.md`
- `qwen3-8b-standard-30m.md`, `qwen3-4b-standard-30m.md`
- `smollm3-3b-standard-30m.md`
- `gpt-oss-20b-standard-30m.md`
- `qwen36-35b-standard-30m.md`
- `comparison-top2-vs-baseline.md`
- `findings.md`

The cheese / seafood / sourdough topics are net-new — write to a subdirectory (e.g. `research-debugging/cheese/`) for cleanliness.

## Key insights to carry forward (don't relearn these)

- llama-server's default `reasoning_format: deepseek` splits Gemma's `<|channel>thought` block into `reasoning_content`. **Setting `reasoning_format: "none"` per request was the wrong direction** — it removes the channel split so JSON mode's grammar isn't applied. Keep the default.
- `chat_template_kwargs={"enable_thinking": False}` is honored by Gemma 4, Qwen3 (8B/4B), Qwen3.6, SmolLM3. **GPT-OSS 20B ignores it** and continues to think.
- For models that ignore `enable_thinking`, the `reasoning_content`-as-fallback in both `_llm_complete` (sync) and `_stream_llm_tokens` (synthesis stream) is what saves the output.
- spaCy's top entities in the corpus are dominated by CARDINAL/ORDINAL noise (`one`, `first`, `1`, `2`); these are filtered out of seeded queries by `_SEED_QUERY_BLOCKED_ENTITY_TYPES`. The corpus has 207 topic labels including obvious junk (`Argan`, `Levain`, `Charcuterie` as topic names are noisy too). Cleaning the topic clustering is out-of-scope for this PR.
- Per-call INFO logs include `phase=`, `elapsed=`, `prompt_tokens=`, `completion_tokens=`, and a WARNING when the cap is hit. **These have been the load-bearing diagnostics throughout** — keep using them when triaging future runs.

## Pending work after sweep

1. Apply any prompt/round/synthesis improvements that the cross-topic/cross-model analysis suggests
2. Land a follow-up PR with those improvements (do not amend PR #218; it's a coherent fix-set on its own)
3. Possibly: open a separate Swift PR if the rebuild surfaces any issues that weren't caught by `xcodebuild ... build` locally

# Research debugging — full journey

## TL;DR

Research at standard depth, 30-minute budget, on Gemma 4 26B-A4B (the largest active model at the start of this work) silently produced an empty report. Diagnosed and fixed across 23 incremental code changes (F1–F23) plus two macOS-side bug fixes. After the fixes, all 7 downloaded curated models complete a standard-depth 30-minute research run and produce substantive reports. Comparison of the two largest models (Qwen3.6 35B-A3B, Gemma 4 26B-A4B) against a Claude-driven baseline is in [comparison-top2-vs-baseline.md](comparison-top2-vs-baseline.md).

## Problem statement (user-reported)

> "I can't even get [Research] to complete. Iterate repeating that question until you can get it to complete with one model at standard research depth."

## What we found, in the order we found it

### Symptom #1: Run #1 (no fixes) — silent half-completion
- Took 9 min, did eventually complete and write a 6,257-char report.
- During the run, the SSE stream went silent for >2 min after entering the "analyzing" phase. From the user-facing UI's perspective the run looked stuck because the UI's idle/timeout heuristic disconnects long before the server-side task finishes.
- llama-server slot was active the whole time, decoding 5,000+ tokens for a query-planning call that should have produced ~200 tokens of JSON.

### Root cause #1: runaway generation in every LLM call ([F1–F19])
- `_llm_complete` sent `payload = {"messages": messages, "temperature": 0.3}` with no `max_tokens`. llama-server defaulted to unbounded.
- Models like Gemma 4 26B that "think" before answering route the thinking through llama-server's `reasoning_format: deepseek` channel — when the cap on the *thinking* exhausts the budget, `content` ends up empty even though the API call returned 200.
- Bumping caps alone wasn't enough: tighter caps (1200/300) silently produced no JSON; looser caps (5000/1200) burned wall-time. Setting `reasoning_format: "none"` per-request (F13) was the wrong direction — it removed the channel split so the JSON wasn't grammar-constrained in `content` at all.

### Root cause #2: the silver bullet — `enable_thinking: False` ([F20])
- Direct probe of llama-server with `chat_template_kwargs: {"enable_thinking": False}` on Gemma 4 produced clean JSON in **29 completion tokens**, vs **187** with thinking on. Same result on Qwen3 (8B and 4B) and Qwen3.6 35B-A3B.
- After F20 (passing this kwarg on every `_llm_complete` and `_stream_llm_tokens` call), Gemma 26B planning dropped from 35–69 s to **2 s**. Notes phase dropped from cap-hitting to ~970 tokens.
- GPT-OSS 20B *ignores* the kwarg (it's a hardcoded reasoning model); see #4.

### Root cause #3: model switch silently no-ops ([F21])
- Documented in the prior memory note `project_llm_restart_investigation`. Confirmed in this work: `PUT /api/chat/models/{id}/activate` writes `llm_model_id` to config.json but Swift's config-watcher never reacts (root cause below in macOS bugs).
- F21 adds an explicit `request_llm_restart()` call from the activate-model route, which writes `llm_restart: true` to config.json — Swift is *supposed* to act on that flag.

### Root cause #4: GPT-OSS streaming synthesis silently produces 0 tokens ([F22])
- GPT-OSS 20B emits everything through `delta.reasoning_content` (it ignores `enable_thinking: False`). The streaming function only read `delta.content`.
- F22 buffers `delta.reasoning_content` and emits it as a fallback if no `delta.content` token ever arrives. No thinking is mixed into otherwise-clean output, but on GPT-OSS the report is now non-empty.

### Root cause #5: report model_id read at GET-time, not stored ([F23])
- User-noticed bug: opening an old report after switching models showed the *new* model's id. The `ChatMessage` rows already stored the right `model_id` at write time; the GET endpoint was just substituting `get_settings().llm_model_id`.
- F23 reads `model_id` from the assistant message that holds the report.

## macOS-side bug fixes (Swift)

These are the bugs that made the whole loop painful. Both fixed.

### Swift fix #1: `restartService` ignored config changes
The StatusWindow "Restart" button on a service called `restartService(service)`, which used `AppSettings.shared` directly. Since the config watcher was broken (see below), `AppSettings` was stale from app startup, so restarting the LLM service relaunched llama with the *old* model_id.

Fixed by calling `AppSettings.shared.reload()` at the top of `restartService`.

### Swift fix #2: config-watcher silently dropped events
`startConfigWatcher` set up a `DispatchSource.makeFileSystemObjectSource(eventMask: [.write, .rename])`. In practice the handler never fired after Python wrote config.json (verified with `log show --predicate 'subsystem == "com.harborclerk.server"'`).

Fixed in two ways:
1. Added an INFO log at the start of the dispatch handler so future debugging can confirm whether it fires at all.
2. Added a 3-second mtime polling timer as a belt-and-suspenders fallback. The dispatch source is still preferred (instant), but if it drops the event the poller will catch it within ~3 s.

Both Swift fixes are in `macos/HarborClerkServer/HarborClerkServer/ServiceManager.swift` (build verified locally with `xcodebuild ... build`). **A fresh `make` of the macOS server app is required for these to take effect.** After that, model switches via the UI/API will trigger a real llama-server restart without requiring the user to quit-and-relaunch the app.

## Run-by-run summary

All runs use the same question and `time_limit=30` minutes, `depth=standard` unless noted.

| # | Model | Depth | Code state | Time | Report | Notes |
|---|---|---|---|---|---|---|
| 1 | Gemma 4 26B-A4B | std | OLD code | ~9 min | 6,257 chars | Server-side completion despite SSE silence — the user just never saw it because the UI disconnects |
| 2 | Gemma 4 26B-A4B | std | F1–F7 | ~2:23 | 2,943 chars | Caps too tight; planning fallback fired; ~3 regions covered |
| 3 | Gemma 4 26B-A4B | std | F1–F11 | ~2:36 | 3,474 chars | Same fallback-query problem (cap during thinking) |
| 4 | Gemma 4 26B-A4B | std | F1–F12 | ~3 min | 4,457 chars | JSON mode + reasoning_format=deepseek hits the same wall |
| 5 | Gemma 4 26B-A4B | std | F1–F13 | ~3 min | 3,003 chars | reasoning_format=none was wrong direction |
| 6 | Gemma 4 26B-A4B | std | F1–F15 | ~5 min | 1,613 chars (skeletal) | Notes call returned `content=""` because thinking ate it |
| 7 | Gemma 4 26B-A4B | std | F1–F19 | ~7 min | 0 chars (synthesis silent) | Streaming has no reasoning_content fallback |
| **8** | **Gemma 4 26B-A4B** | **std** | **F1–F20** ✓ | **1:35** | **4,184 chars** | **First good run** — `enable_thinking: False` is the fix. ~12 regions. |
| 9 | Gemma 4 26B-A4B | light | F1–F20 | 70 s | 4,710 chars | Clean. ~10 regions. |
| 10 | Gemma 4 26B-A4B | thorough | F1–F20 | ~2 min | 5,497 chars | Clean. ~14 regions. |
| 11 | GPT-OSS 20B | std | F1–F21 | ~3 min | 0 chars (synthesis silent) | Notes great, synthesis silent (delta.content empty for entire stream) |
| 12 | Qwen3 8B | std | F1–F22 | 98 s | 7,089 chars | Clean. ~14 regions. |
| 13 | Qwen3 4B | std | F1–F22 | 60 s | 4,729 chars | Sweep strategy (smaller-model default). ~9 regions. |
| 14 | SmolLM3 3B | std | F1–F22 | ~115 s | 10,694 chars | Verbose. Notes hit cap. ~5 regions surfaced cleanly. |
| 15 | **Qwen3.6 35B-A3B** | std | F1–F23 | ~120 s | **9,431 chars** | **Best of the local models.** ~25 regions/producers. |
| 16 | GPT-OSS 20B (retry) | std | F1–F22 | ~5 min | 7,660 chars | F22 fallback fired and produced a real report. ~21 regions. |

## Logging additions (F7)

Every LLM call now emits one INFO line of the form:

```
LLM call phase=planning elapsed=11.0s prompt_tokens=784 completion_tokens=600 max_tokens=600
```

Plus a WARNING when the cap is hit, when the parser produced no plausible queries, when the prompt was capped, and when synthesis falls back to reasoning_content. These have been the load-bearing diagnostics through the entire fix journey.

## What's NOT changed (out of scope for this PR)

- **Strategy choice** (`search` vs `sweep`) and `_DEPTH_CONFIG` numbers — the existing values worked; we only fixed the plumbing. Tuning these is a separate, easier task now that the pipeline reliably completes.
- **Topic labels.** The corpus's top-entity list is dominated by spaCy CARDINAL/ORDINAL noise ("one", "first", "1") and corpus topics include junk labels ("Argan", "Levain", "Charcuterie") inflating the prompt. F10 filters CARDINAL/ORDINAL out of seeded queries, but cleaning corpus topics is a separate concern.
- **Frontend SSE handling.** The "looks stuck" UX during long phases (notes/synthesis) is a frontend issue — the server-side stream is fine, but the WKWebView/fetch idle behavior makes it look frozen. Worth a separate investigation.

## Files touched in this PR

| File | What |
|---|---|
| `src/harbor_clerk/llm/research.py` | F1–F20, F22, F23 — caps, parsers, prompt cap, deadline guard, plausibility filter, JSON mode, enable_thinking, reasoning_content fallback (sync + streaming), per-call logging |
| `src/harbor_clerk/llm/health.py` | F21 — `request_llm_restart()` helper for explicit restart signaling |
| `src/harbor_clerk/api/routes/chat.py` | F21 — activate_model + deactivate_model both call `request_llm_restart()` on actual changes |
| `src/harbor_clerk/api/routes/research.py` | F23 — read `model_id` from the assistant message instead of current settings |
| `macos/HarborClerkServer/HarborClerkServer/ServiceManager.swift` | Swift fix #1 (AppSettings reload in restartService) + Swift fix #2 (mtime poller fallback for config watcher + handler entry log) |
| `research-debugging/baseline-claude.md` | Claude-MCP-tools-driven baseline used as the comparison reference |
| `research-debugging/findings.md` | This file |
| `research-debugging/comparison-top2-vs-baseline.md` | Top-2 vs baseline coverage matrix and analysis |
| `research-debugging/{model}-{depth}-30m.md` | Individual model run outputs (8 files) |

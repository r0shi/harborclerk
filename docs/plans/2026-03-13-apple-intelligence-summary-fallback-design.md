# Apple Intelligence Summarization Fallback — Design

**Goal**: Use Apple's Foundation Models framework as a middle-tier summarization fallback on macOS native builds — better than extractive, available without downloading a GGUF model.

**Supersedes**: `2026-02-26-apple-intelligence-summarization-fallback.md` (HTTP microservice approach replaced with simpler CLI tool).

---

## Fallback Chain

When the summarize stage runs for a document version:

1. **LLM** (llama-server with user's chosen GGUF model) — existing path. If `llm_model_id` is empty or the call fails → try next.
2. **Apple Intelligence** (Foundation Models via `apple-summarize` CLI) — macOS native only. If binary not found, exits non-zero, or times out → try next.
3. **Extractive** (first substantial paragraph heuristic) — always works, unchanged.

The `summary_model` column records the source: model ID for LLM, `"apple-intelligence"` for Foundation Models, `"extractive"` for the heuristic.

## Architecture Decision: CLI Tool, Not HTTP Server

The earlier design (Feb 26) proposed an HTTP microservice (`apple-intelligence-server`) mimicking the OpenAI API. We're going with a simpler CLI tool instead:

- **No port management, no health checks, no persistent process.** Summarization is infrequent (once per document). Process spawn overhead is negligible.
- **No ServiceManager integration.** The Swift Server app stays a pure process manager for long-running services only.
- **Simpler error handling.** Binary not found = not available. Non-zero exit = failed. No HTTP client code, no retry on 503.
- **Easier testing.** Pipe text in, get summary out.

## Swift CLI Tool: `apple-summarize`

A standalone Swift command-line tool using the Foundation Models framework (macOS 26+, Apple Silicon).

**Interface:**
- **Input**: reads document text from stdin
- **Output**: writes summary text to stdout
- **Exit codes**: 0 = success, 1 = Foundation Models unavailable or generation error
- **Arguments**: `--max-chars <N>` (default 500) — target summary length
- **Prompt**: "Summarize this document in 2-3 sentences. Be specific about the subject matter. Stay under {max_chars} characters."

**Build:**
- New Swift package: `macos/apple-summarize/`
- `Package.swift` depends on Foundation Models framework
- Single source file: `Sources/main.swift`
- Built by `macos/Makefile` as a new target, output goes to `macos/build/output/apple-summarize`
- Bundled inside `HarborClerkServer.app/Contents/Resources/apple-summarize`
- Build is conditional — only on macOS 26+ with Xcode 26+. CI and Docker skip it entirely.

## Python Integration

New function in `src/harbor_clerk/llm/summarize.py`:

```python
def _apple_intelligence_summary(text: str, max_chars: int) -> str | None:
    """Call the apple-summarize CLI tool. Returns None if unavailable or failed."""
```

- Locates the binary: checks `APPLE_SUMMARIZE_PATH` env var first, then a well-known path relative to the native app bundle
- Calls `subprocess.run(["apple-summarize", "--max-chars", str(max_chars)], input=text, capture_output=True, timeout=120, text=True)`
- Returns `stdout.strip()` on exit code 0, `None` otherwise
- Logs the outcome at debug level

**Invocation point**: In `generate_summary()`, between the existing LLM attempt and the extractive fallback:

```python
# 1. Try LLM (existing)
summary = _llm_summary(chunks, max_chars)
if summary:
    return summary, settings.llm_model_id

# 2. Try Apple Intelligence (new)
summary = _apple_intelligence_summary(sampled_text, max_chars)
if summary:
    return summary, "apple-intelligence"

# 3. Extractive fallback (existing)
return _extractive_fallback(chunks, max_chars), "extractive"
```

## Input Handling

Same sampling strategy as the LLM path for selecting which text to send:
- **Short docs** (<20 chunks): concatenate all chunks
- **Medium/long docs** (20+): strategic sampling — first 3 + last 2 + evenly-spaced middle chunks

Apple Intelligence on-device models have limited context (~4K tokens). Cap input to **12,000 characters**. The prompt notes these are excerpts when text is sampled.

## Runtime Detection

No startup probing or config flags. The Python worker simply tries to invoke the binary at summarize time:
- Binary not found → `FileNotFoundError` → return `None` → extractive fallback
- Binary exists but Foundation Models unavailable (wrong OS/hardware) → non-zero exit → return `None`
- Binary succeeds → use the summary

On Docker/Linux, the binary doesn't exist, so it falls through instantly. On macOS without Apple Intelligence, the binary exits non-zero. No wasted time either way.

## What Doesn't Change

- **Pipeline flow**: summarize stage still runs on io queue, same 900s timeout, same fan-out with entities/embed
- **Database schema**: no new columns — `summary_model` already handles attribution
- **Frontend**: already displays summary + model name in parentheses
- **Docker/Linux**: unaffected — binary won't exist, falls through to extractive
- **Existing LLM summarization**: completely unchanged, still the primary path

## Files to Create

| File | Description |
|------|-------------|
| `macos/apple-summarize/Package.swift` | SPM manifest, Foundation Models dependency |
| `macos/apple-summarize/Sources/main.swift` | Read stdin, call Foundation Models, write stdout |

## Files to Modify

| File | Change |
|------|--------|
| `macos/Makefile` | Add `apple-summarize` build target, bundle into server app Resources |
| `src/harbor_clerk/llm/summarize.py` | Add `_apple_intelligence_summary()`, insert into fallback chain |
| `src/harbor_clerk/config.py` | Add `apple_summarize_path: str` setting (default empty — auto-detect) |

## Not In Scope

- Using Apple Intelligence for chat — separate, larger effort
- Replacing llama-server — this is fallback only
- Cloud API calls — everything stays on-device
- Document type classification via Apple Intelligence — extractive/MIME fallback is sufficient

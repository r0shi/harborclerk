# Apple Intelligence Summarization Fallback

**Goal**: On Apple Silicon Macs running macOS 26+, use the Foundation Models framework as a middle-tier summarization fallback — better than extractive, available without downloading a model.

**Fallback chain after this feature**:
1. User-selected model (qwen3-4b, etc.) via llama-server → best quality
2. Apple Intelligence via Foundation Models → good quality, zero setup
3. Extractive heuristic (first substantial paragraph) → always works

---

## Context

- The Swift Server app is a **pure process manager** — no HTTP server of its own
- Python workers call `POST {llama_server_url}/v1/chat/completions` for summaries
- Foundation Models is Swift-only (macOS 26+), not callable from Python
- `generate_summary()` in `src/harbor_clerk/llm/summarize.py` already has the LLM path + extractive fallback

## Design

### Approach: Standalone Swift CLI tool (`apple-intelligence-server`)

Build a small Swift command-line executable that:
- Starts an HTTP server on a configurable port (default 8103)
- Exposes `GET /health` and `POST /v1/chat/completions` (OpenAI-compatible subset)
- Routes requests to Foundation Models on-device model
- Returns responses in the same JSON format as llama-server

**Why a standalone CLI tool?**
- Keeps the Swift Server app as a pure process manager (no architectural change)
- ServiceManager already knows how to spawn/manage/health-check subprocesses
- Can be tested independently
- Clean separation — if Foundation Models isn't available, the tool simply isn't started

### Service Integration

The Swift Server app gets a new managed service: `AppleIntelligenceService`
- Started **only when** no user-selected LLM model is active (`llm_model_id` is empty)
- Stopped when user activates a model (llama-server takes over port 8102, or AI server runs on 8103)
- Same health check pattern as other services (`GET /health`)

### Python-side: Two-URL fallback in `generate_summary()`

```python
# In generate_summary():
# 1. Try llama_server_url (user model) — existing path
# 2. Try apple_intelligence_url (Foundation Models) — new path
# 3. Extractive fallback — existing path
```

New config setting: `apple_intelligence_url: str = Field(default="http://localhost:8103")`

The Swift Server app passes `APPLE_INTELLIGENCE_URL` env var to Python services, same as `LLAMA_SERVER_URL`.

## Files to Create

| File | Description |
|------|-------------|
| `macos/apple-intelligence-server/` | Swift package for the CLI tool |
| `macos/apple-intelligence-server/Package.swift` | SPM manifest, depends on Foundation Models |
| `macos/apple-intelligence-server/Sources/main.swift` | HTTP server + Foundation Models bridge |

## Files to Modify

| File | Change |
|------|--------|
| `macos/HarborClerkServer/Services/AppleIntelligenceService.swift` | **New** — managed service for the CLI tool |
| `macos/HarborClerkServer/Services/ServiceManager.swift` | Add AI service to startup chain (after Tika, before API) |
| `macos/HarborClerkServer/Settings/AppSettings.swift` | Add `appleIntelligencePort` setting (default 8103) |
| `macos/scripts/build-apple-intelligence.sh` | **New** — build script for the Swift CLI tool |
| `macos/Makefile` | Add `apple-intelligence` target |
| `src/harbor_clerk/config.py` | Add `apple_intelligence_url` setting |
| `src/harbor_clerk/llm/summarize.py` | Add Apple Intelligence as second fallback tier |

## Open Questions

1. **Foundation Models availability detection**: Does the framework gracefully report when on-device models aren't downloaded/available? Need to handle the case where macOS 26 is running but models haven't been set up yet.
2. **Context window**: What's the input limit for Foundation Models' on-device model? Our summary input is ~3000 chars which should be fine, but need to verify.
3. **Latency**: On-device inference should be fast, but first call may have model load latency. May need a longer initial health check timeout.
4. **Build gating**: The `apple-intelligence-server` target should be conditional — only build on macOS 26+ with Xcode 26+. CI and Docker builds skip it entirely.

## Not In Scope

- Using Apple Intelligence for chat (Feature 9) — that's a separate, larger effort
- Replacing llama-server with Apple Intelligence — this is fallback only
- Any cloud API calls — everything stays on-device

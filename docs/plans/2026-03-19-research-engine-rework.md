# Research Engine Rework

**Date:** 2026-03-19
**Status:** Approved

## Problem

The research engine uses a fixed 20-round iteration limit as its primary stopping condition. This is too coarse — tasks often don't run long enough to cover a topic thoroughly (a typical run is 1-7 minutes), but the user would be willing to let research run for up to a few hours. Additionally, the model tends to make only 1 tool call per round despite the code supporting multiple, and notes can grow unbounded in long sessions.

## Design

### 1. Wall-Time Stopping Condition

Replace fixed `max_rounds` with a user-selected wall-time limit per task.

**Time limit selector:** Presented alongside the query input in the New Research form. Options: 15m, 30m, 45m, 1h, 1.5h, 2h, 2.5h, 3h. Default: 30m.

**Stopping behavior:**
- **Soft limit** (user-selected time): Stop starting new rounds. Finish current round, then proceed to synthesis.
- **Hard limit** (soft + 10 minutes): Force synthesis even if mid-round. Prevents runaway if the current round's LLM call hangs.
- **Safety cap**: 500 rounds as an absolute backstop.

Existing stopping conditions remain:
- **Stall detection**: 3 rounds with unchanged notes length.
- **Model `<report>` signal**: Model emits `<report>` tag to indicate it's done.

### 2. Notes Condensation

For long-running tasks, notes can grow beyond what fits in the context window. Periodically consolidate notes to keep them bounded.

**Trigger:** Every 15 rounds, OR when notes exceed 50% of the effective context window (YaRN-aware).

**Mechanism:** A dedicated LLM call with a condensation prompt: "Consolidate these research notes. Keep all citations. Remove redundancy. Tighten phrasing. Preserve every [Document Title, page X] reference."

**YaRN awareness:** The condensation threshold uses the same `context_tokens` calculation that already respects `llm_yarn_enabled` and per-model YaRN config. No new infrastructure needed.

### 3. Multi-Tool-Call Prompting

The model can already emit multiple tool calls per round (the code supports it), but in practice makes only 1. Update the research system prompt to encourage batch tool calls:

> "Call multiple tools per round to explore different angles simultaneously. For example, search with 3 different queries, or search + read_passages + entity_search in one turn. This is faster than one tool per round."

This is a prompt-only change — no architecture changes needed.

### 4. UI Changes

**New Research form:**
- Time limit selector (dropdown or segmented control) next to the strategy toggle.
- Options: 15m, 30m, 45m, 1h, 1.5h, 2h, 2.5h, 3h. Default: 30m.

**Running view:**
- Show elapsed time and time limit instead of "Round X of Y".
- Round count still tracked internally and shown in the activity log.

**Completed metadata:**
- Show elapsed time (already present) and time limit.

**SSE progress events:**
- Add `elapsed_seconds` and `time_limit_minutes` fields.
- Keep `round` for the activity log but remove `max_rounds`.

### 5. DB Changes

Add `time_limit_minutes` column to `research_state`:
- Type: `INTEGER`, nullable, default 30.
- New tasks populate this field; legacy tasks with `max_rounds` still work (code checks both).
- Keep `max_rounds` column for backwards compatibility but set it to 500 (safety cap) for new tasks.

Migration: single `ADD COLUMN` for `time_limit_minutes`.

### 6. API Changes

**POST /api/research:** Accept optional `time_limit_minutes` in request body (default 30, min 15, max 180).

**GET /api/research/{id}:** Include `time_limit_minutes` in response.

### 7. Backend Loop Changes

The iteration loop in `research_stream()`:
- Record `start_time = datetime.now(UTC)` before the loop.
- Each round checks `elapsed = (now - start_time).total_seconds()` against `time_limit_minutes * 60`.
- If elapsed >= soft limit, break to synthesis after current round completes.
- If elapsed >= hard limit (soft + 600s), force-break even mid-tool-execution.
- Condensation check after each round: if `round % 15 == 0` or `len(notes) > context_tokens * 0.5 * CHARS_PER_TOKEN`, run condensation call.

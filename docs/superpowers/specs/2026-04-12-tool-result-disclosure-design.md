# Tool Result Disclosure Triangles

> **For agentic workers:** Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this spec.

**Goal:** Make full tool call results visible inline via disclosure triangles in both Ask (chat) and Research modes, with formatted views for common tools and a raw JSON toggle.

**Motivation:** Transparency for non-technical users and easier debugging when research quality regresses.

---

## Data Flow

### SSE Streaming (live)

The `tool_result` SSE event gains a `result` field containing the full tool output JSON string.

**Chat (`chat.py`):** The `tool_result` event currently sends `{ type, name, summary }`. Add `result: <raw JSON string>` from the tool execution.

**Research (`research.py`):** The `tool_result` event currently sends `{ type, name, summary }`. Add `result: <raw JSON string>` from the tool observation.

### API Reload

`_enrich_tool_calls()` in `api/routes/chat.py` already looks up `role='tool'` messages by `tool_call_id` to derive summaries. Include the full `content` from the tool-role message as `raw_result` in the response.

The `ToolCallOut` schema (or equivalent) gains `raw_result: str | None`.

### Frontend Types

- `ToolCallInfo` in `ChatContext.tsx` gains `rawResult?: string`
- `ToolCallEntry` in `ResearchContext.tsx` gains `rawResult?: string`

Both contexts populate `rawResult` from the SSE `result` field (live) and API response (reload).

---

## Frontend Components

### ToolResultDisplay (new)

**File:** `frontend/src/components/ToolResultDisplay.tsx`

**Props:** `rawResult: string`, `toolName: string`

**Behavior:**
1. Parses `rawResult` as JSON (falls back to plain text display on parse failure)
2. Renders **formatted view** by default using tool-specific templates
3. Provides a "Show raw" / "Show formatted" toggle at the bottom switching to pretty-printed `<pre>` JSON

**Tool-specific templates:**

| Tool name pattern | Template |
|---|---|
| `search_documents`, `kb_search`, `kb_batch_search` | Hit list: doc title, page, score, snippet (~100 chars) |
| `read_passages`, `kb_read_passages` | Text blocks with doc title + page header |
| `entity_search`, `kb_entity_search` | Tagged list: entity name, type, source doc |
| Everything else | Generic recursive renderer: objects as labeled groups, arrays as bullet lists, scalars as values |

Templates are intentionally lean — no hover states, no interactivity, just clean readable output. The generic fallback handles any tool output readably.

### Chat Mode — ToolCallCard Changes

The existing `ToolCallCard` component (ChatPage.tsx) has an expand/collapse accordion. In the expanded body, add `<ToolResultDisplay>` below the existing arguments display when `rawResult` is present. No structural change to the card.

### Research Mode — ToolLogEntry Changes

Convert `ToolLogEntry` (ResearchPage.tsx) from a flat row to a `<details>/<summary>` element:

- **Summary row:** Same as current — icon (pulsing blue if active, green check if done) + tool name + inline summary text
- **Disclosure body:** `<ToolResultDisplay>` with the full result
- Entries without results yet (still in-flight) remain flat with no disclosure triangle
- All triangles start **collapsed**

---

## Backend Changes

### chat.py — SSE emission

In the tool call execution loop, the `tool_result` SSE event currently sends:
```python
{"type": "tool_result", "name": name, "summary": summary}
```

Change to:
```python
{"type": "tool_result", "name": name, "summary": summary, "result": raw_result_string}
```

Where `raw_result_string` is the string returned by `execute_tool()`.

### research.py — SSE emission

In the step consumer loop, when emitting `tool_result` events, include the full observation string:
```python
{"type": "tool_result", "name": tc_name, "summary": summary, "result": observation}
```

Where `observation` is already available from `step.observation or step.output`.

### api/routes/chat.py — _enrich_tool_calls

Add `raw_result` to the enriched tool call dict, sourced from the matching `role='tool'` message's `content` field (already looked up for summary derivation).

---

## Scope Notes

- **smolagents regression:** Separately investigate smolagents 1.22→1.24 changelog for research quality changes. Not part of this spec.
- No lazy loading — full results included inline in SSE and API responses (typically 2-10KB per tool call).
- No new API endpoints.
- No database changes.

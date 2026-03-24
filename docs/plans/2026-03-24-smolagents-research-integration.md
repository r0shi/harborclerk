# smolagents Research Engine Integration

**Date:** 2026-03-24
**Status:** Approved

## Problem

The hand-rolled research iteration loop has persistent bugs: notes loss during long runs, empty follow-up responses, context overflow causing the model to produce no output, and fragile stopping conditions. These are solved problems in agent frameworks.

## Design

Replace the research iteration loop with Hugging Face's smolagents `ToolCallingAgent`, keeping our synthesis pass, SSE streaming, and DB persistence.

### 1. LLM Backend

`OpenAIServerModel` connects directly to llama-server — no adapter needed:

```python
model = OpenAIServerModel(
    model_id="local",
    api_base=f"{settings.llama_server_url}/v1",
    api_key="not-needed",
)
```

### 2. Tool Definitions

Existing tools (search_documents, read_passages, etc.) become smolagents `Tool` subclasses. Each wraps the existing `execute_tool` dispatch, preserving all MCP tool logic unchanged:

```python
class SearchDocumentsTool(Tool):
    name = "search_documents"
    description = "Search the knowledge base..."
    inputs = {"query": {"type": "string", ...}, "k": {"type": "integer", ...}}
    output_type = "string"

    def forward(self, query, k=10, **kwargs):
        return asyncio.run(execute_tool("search_documents", {"query": query, "k": k}, self.user_id))
```

### 3. Agent Configuration

`ToolCallingAgent` replaces the hand-rolled iteration loop:
- `max_steps` — derived from wall-time limit (estimated from observed step duration, with safety margin)
- `planning_interval` — controlled by "Research depth" selector
- `stream_outputs=True` — enables step-by-step streaming

**User-facing controls (at research initiation):**
- **Time limit** (existing): 15m-3h in 15m increments, default 30m
- **Research depth** (new): Light / Standard / Thorough
  - Light: `planning_interval=3` — frequent re-planning, more adaptive
  - Standard: `planning_interval=5` — balanced (default)
  - Thorough: `planning_interval=10` — more execution between planning pauses

Strategy (search vs. sweep) becomes a system prompt variation rather than a code path difference.

### 4. Streaming to SSE

`agent.run(task, stream=True)` returns a generator of step objects. We wrap this to yield our existing SSE event format, preserving the frontend contract:

- `progress` events: step number, elapsed time, time limit
- `tool_call` / `tool_result` events: from agent step data
- `notes` events (new): agent's current memory/findings after each step
- `synthesis` / `token` / `done` events: from the post-agent synthesis pass

### 5. Synthesis Pass

Three-stage flow:
1. **Agent iterates**: tool calls → observations → memory accumulates
2. **Agent calls `final_answer()`**: summary of findings → saved to chat history, visible in UI
3. **Synthesis pass**: fresh LLM call with (question + final_answer text) → polished cited report

The user sees the agent's raw conclusions in the activity log and the formatted report as the final output.

### 6. Memory Management

Trust smolagents' built-in memory truncation for now. If findings get lost in long runs (detectable by comparing final_answer quality vs. tool results), upgrade to a hybrid approach: maintain a separate notes string outside the agent, fed back as context each step.

Notes condensation (`_condense_notes`) is removed initially. Re-evaluate if memory truncation proves insufficient.

### 7. Notes Streaming

New `notes` SSE event streams the agent's current accumulated findings after each step. Frontend shows this in a collapsible panel below the activity log.

### 8. What Changes

| Component | Action |
|---|---|
| `research.py` iteration loop | Replace with smolagents ToolCallingAgent |
| `_build_iteration_messages` | Replace with smolagents prompt templates |
| `_stream_llm_call` (research) | Replace with smolagents model backend |
| `_parse_notes` / notes accumulation | Replace with agent memory |
| `_condense_notes` | Remove (evaluate later) |
| Stall detection | Replace with agent's built-in loop termination |
| Tool definitions (`tools.py`) | Refactor into Tool subclasses (new file) |
| `execute_tool` dispatch | Keep — tools delegate to it |
| Synthesis pass | Keep |
| SSE event format | Keep + add `notes` event |
| DB checkpointing (ResearchState) | Keep (adapted) |
| Wall-time stopping | Keep (via max_steps + periodic time check) |
| Frontend time limit selector | Keep |
| Frontend research depth selector | New |

### 9. Dependencies

Add `smolagents>=1.22` to `pyproject.toml` main dependencies. Apache 2.0 license, compatible with MIT. Already installed and verified.

### 10. Files Affected

**New:**
- `src/harbor_clerk/llm/research_tools.py` — smolagents Tool subclasses wrapping existing tools

**Major rewrite:**
- `src/harbor_clerk/llm/research.py` — replace iteration loop with agent.run(), keep synthesis pass and SSE wrapper

**Minor changes:**
- `src/harbor_clerk/api/routes/research.py` — pass `depth` parameter
- `src/harbor_clerk/api/schemas/research.py` — add `depth` field to StartResearchRequest
- `frontend/src/pages/ResearchPage.tsx` — add depth selector
- `frontend/src/contexts/ResearchContext.tsx` — pass depth to API
- `pyproject.toml` — add smolagents dependency

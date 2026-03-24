# smolagents Research Integration — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the hand-rolled research iteration loop with smolagents `ToolCallingAgent`, adding research depth control and notes streaming.

**Architecture:** smolagents `ToolCallingAgent` drives the research loop via `OpenAIServerModel` (llama-server). Existing tools are wrapped as smolagents `Tool` subclasses delegating to `execute_tool()`. The agent's streamed steps are translated to SSE events for the frontend. The synthesis pass remains a separate post-agent LLM call.

**Tech Stack:** smolagents (ToolCallingAgent, OpenAIServerModel, Tool), FastAPI SSE, React/TypeScript

---

### Task 1: Add smolagents dependency

**Files:**
- Modify: `pyproject.toml`

**Step 1:** Add `"smolagents>=1.22"` to the `dependencies` list in `pyproject.toml`.

**Step 2:** Lock and verify:
```bash
cd /Users/alex/mcp-gateway && uv lock && uv sync
uv run python -c "from smolagents import ToolCallingAgent, OpenAIServerModel, Tool; print('OK')"
```

**Step 3:** Commit:
```bash
git add pyproject.toml uv.lock
git commit -m "chore: add smolagents dependency"
```

---

### Task 2: Create research tool wrappers

**Files:**
- Create: `src/harbor_clerk/llm/research_tools.py`

**Step 1:** Create smolagents `Tool` subclasses that wrap existing tools.

Each tool class:
- Defines `name`, `description`, `inputs`, `output_type` as class attributes
- Stores `user_id` as an instance attribute (set at construction)
- Implements `forward()` which calls `execute_tool()` synchronously (smolagents calls `forward()` from a sync context)

Tools to wrap (matching current `get_research_tools()` output):
- `SearchDocumentsTool` — wraps `search_documents`
- `ReadPassagesTool` — wraps `read_passages`
- `ExpandContextTool` — wraps `expand_context`
- `GetDocumentTool` — wraps `get_document`
- `ListDocumentsTool` — wraps `list_documents`
- `CorpusOverviewTool` — wraps `corpus_overview`
- `DocumentOutlineTool` — wraps `document_outline`
- `FindRelatedTool` — wraps `find_related`
- `EntitySearchTool` — wraps `entity_search`
- `EntityOverviewTool` — wraps `entity_overview`
- `EntityCooccurrenceTool` — wraps `entity_cooccurrence`
- `ReadDocumentTool` — wraps `read_document`
- `CorpusTopicsTool` — wraps `corpus_topics`

Use `asyncio.run()` inside `forward()` to call the async `execute_tool()` — smolagents runs tools synchronously. However, since we're already inside an async context (FastAPI), we need `asyncio.run_coroutine_threadsafe()` or a new event loop in the executor thread. The simplest approach: since smolagents runs `forward()` in a thread via its own executor, create a new event loop per call:

```python
def forward(self, query, k=10, **kwargs):
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(
            execute_tool(self._tool_name, self._build_args(query=query, k=k, **kwargs), self.user_id, mode="research")
        )
    finally:
        loop.close()
```

Add a factory function `build_research_tools(user_id)` that returns a list of all tool instances.

**Step 2:** Commit:
```bash
git add src/harbor_clerk/llm/research_tools.py
git commit -m "feat: smolagents Tool wrappers for research tools"
```

---

### Task 3: Add depth field to API and DB

**Files:**
- Modify: `src/harbor_clerk/api/schemas/research.py` — add `depth` field
- Modify: `src/harbor_clerk/api/routes/research.py` — pass `depth` to stream
- Modify: `src/harbor_clerk/models/research_state.py` — add `depth` column
- Create: `alembic/versions/0010_research_depth.py`

**Step 1:** Add `depth` column to `ResearchState`:
```python
depth: Mapped[str | None] = mapped_column(String(10), nullable=True)
```

**Step 2:** Add to `StartResearchRequest`:
```python
depth: str = Field(default="standard", pattern="^(light|standard|thorough)$")
```

**Step 3:** Add `depth` to response schemas (`ResearchSummary`, `ResearchDetail`).

**Step 4:** Create migration `0010_research_depth.py` (idempotent pattern).

**Step 5:** In `start_research()` route, pass `body.depth` to `ResearchState(...)` and `research_stream()`.

**Step 6:** Commit:
```bash
git add src/harbor_clerk/models/research_state.py src/harbor_clerk/api/schemas/research.py \
    src/harbor_clerk/api/routes/research.py alembic/versions/0010_research_depth.py
git commit -m "feat: add research depth field (light/standard/thorough)"
```

---

### Task 4: Rewrite research_stream with smolagents

**Files:**
- Modify: `src/harbor_clerk/llm/research.py` — major rewrite

This is the core task. Replace the hand-rolled iteration loop with smolagents.

**Step 1:** Rewrite `research_stream()`. The new structure:

```python
async def research_stream(conversation_id, user_id=None, resume=False):
    # ... load state from DB (keep existing code) ...

    # Configure smolagents
    from smolagents import ToolCallingAgent, OpenAIServerModel
    from harbor_clerk.llm.research_tools import build_research_tools

    model = OpenAIServerModel(
        model_id="local",
        api_base=f"{settings.llama_server_url}/v1",
        api_key="not-needed",
    )
    tools = build_research_tools(user_id)

    # Map depth to planning_interval
    planning_map = {"light": 3, "standard": 5, "thorough": 10}
    planning_interval = planning_map.get(state.depth or "standard", 5)

    # Estimate max_steps from time limit (assume ~30s per step)
    time_limit_s = (state.time_limit_minutes or 30) * 60
    max_steps = max(10, time_limit_s // 30)

    agent = ToolCallingAgent(
        tools=tools,
        model=model,
        planning_interval=planning_interval,
    )

    # Run agent in executor (it's sync) and translate steps to SSE
    start_time = datetime.now(UTC)
    step_count = 0

    # Run agent.run(stream=True) in a thread and consume steps
    loop = asyncio.get_running_loop()

    def run_agent():
        return list(agent.run(task=user_question, stream=True, max_steps=max_steps, return_full_result=True))

    # Actually, we need to stream steps as they happen, not collect them all.
    # Use a queue: agent thread puts steps, async generator consumes them.
    import queue
    step_queue = queue.Queue()

    def run_agent_with_queue():
        for step in agent.run(task=user_question, stream=True, max_steps=max_steps):
            step_queue.put(step)
            # Check wall-time
            elapsed = (datetime.now(UTC) - start_time).total_seconds()
            if elapsed >= time_limit_s:
                break
        step_queue.put(None)  # sentinel

    executor_future = loop.run_in_executor(None, run_agent_with_queue)

    # Consume steps from queue and yield SSE events
    while True:
        try:
            step = await asyncio.wait_for(
                loop.run_in_executor(None, step_queue.get, True, 30),
                timeout=35,
            )
        except (asyncio.TimeoutError, queue.Empty):
            # Keepalive
            state.heartbeat_at = datetime.now(UTC)
            await session.commit()
            yield ": keepalive\n\n"
            continue

        if step is None:
            break  # Agent finished

        # Translate step to SSE events
        if hasattr(step, 'tool_calls') and step.tool_calls:
            for tc in step.tool_calls:
                yield f"data: {json.dumps({'type': 'tool_call', 'name': tc.name, 'arguments': tc.arguments})}\n\n"
                if step.observations:
                    summary = summarize_tool_result(step.observations)
                    yield f"data: {json.dumps({'type': 'tool_result', 'name': tc.name, 'summary': summary})}\n\n"

        if hasattr(step, 'step_number'):
            step_count = step.step_number
            elapsed = int((datetime.now(UTC) - start_time).total_seconds())
            yield f"data: {json.dumps({'type': 'progress', 'step': step_count, 'elapsed_seconds': elapsed, 'time_limit_minutes': state.time_limit_minutes or 30})}\n\n"

        # Stream notes (agent's current observations/memory)
        if hasattr(step, 'model_output') and step.model_output:
            model_text = step.model_output if isinstance(step.model_output, str) else str(step.model_output)
            if model_text.strip():
                yield f"data: {json.dumps({'type': 'notes', 'content': model_text[:2000]})}\n\n"

        # Checkpoint
        state.current_round = step_count
        state.heartbeat_at = datetime.now(UTC)
        await session.commit()

        # Check for final answer
        if hasattr(step, 'is_final_answer') and step.is_final_answer:
            break

    await executor_future  # ensure thread completes

    # Extract final answer from agent
    final_answer_text = ""
    if hasattr(agent, 'memory'):
        # Get the last step's output
        ... # extract from agent memory

    # ... synthesis pass (keep existing code, using final_answer_text as notes) ...
```

The key architectural decisions in this rewrite:
- Agent runs in a thread (it's sync), steps are passed to the async generator via a `queue.Queue`
- Wall-time check inside the agent thread can break the loop early
- Each step is translated to SSE events matching the existing frontend contract
- New `notes` SSE event streams the agent's thinking
- DB checkpointing after each step (existing pattern)
- Synthesis pass is kept as a separate LLM call using the agent's final answer

**Important:** Keep the synthesis pass code (`_build_synthesis_messages`, `_stream_llm_tokens`) and the SSE done/error event code unchanged. Only the iteration loop is replaced.

**Important:** Keep `_stream_llm_call` and `_stream_llm_tokens` — they're used by the synthesis pass and potentially by chat.py imports.

**Step 2:** Remove old iteration code that's no longer needed:
- `_build_iteration_messages`
- `_parse_notes`
- `_detect_report_signal`
- `_condense_notes`
- `_CONDENSATION_SYSTEM`
- `_CONDENSATION_ROUND_INTERVAL`
- `_ITERATION_SYSTEM_SEARCH`
- `_ITERATION_SYSTEM_SWEEP`
- `_SWEEP_BATCH_PREFIX`
- `_SWEEP_BATCH_SIZE`
- `_STALL_ROUNDS`

Keep:
- `_SYNTHESIS_SYSTEM`
- `_build_synthesis_messages`
- `_stream_llm_call` (used by synthesis and keepalive)
- `_stream_llm_tokens` (used by synthesis)
- `_truncate_for_context`
- `summarize_tool_result` import
- All keepalive/SSE infrastructure
- The synthesis pass section
- The `finally` block for disconnect handling

**Step 3:** Commit:
```bash
git add src/harbor_clerk/llm/research.py
git commit -m "feat: replace hand-rolled research loop with smolagents ToolCallingAgent"
```

---

### Task 5: Frontend — depth selector and notes panel

**Files:**
- Modify: `frontend/src/pages/ResearchPage.tsx` — add depth selector, notes panel
- Modify: `frontend/src/contexts/ResearchContext.tsx` — pass depth, handle notes events

**Step 1:** In `ResearchContext.tsx`:
- Add `depth` parameter to `startResearch` signature
- Pass `depth` in the request body
- Add `notes` to `ResearchProgress` interface
- Handle `notes` SSE event: store latest notes in progress state
- Handle `progress` event: read `step` instead of `round`

**Step 2:** In `ResearchPage.tsx`:
- Add `depth` state: `const [depth, setDepth] = useState<'light' | 'standard' | 'thorough'>('standard')`
- Add depth selector between strategy toggle and time limit:
```tsx
<div className="flex items-center justify-center gap-2">
  <span className="text-[12px] text-gray-500 dark:text-gray-400">Depth</span>
  <div className="inline-flex rounded-lg border border-gray-200 dark:border-gray-700 bg-gray-100 dark:bg-gray-800 p-0.5">
    {(['light', 'standard', 'thorough'] as const).map((d) => (
      <button key={d} onClick={() => setDepth(d)}
        className={`px-3 py-1.5 text-[12px] font-medium rounded-md transition-all duration-150 ${
          depth === d ? 'bg-white dark:bg-gray-700 text-gray-800 dark:text-gray-200 shadow-xs'
            : 'text-gray-500 dark:text-gray-400 capitalize'
        }`}>
        {d.charAt(0).toUpperCase() + d.slice(1)}
      </button>
    ))}
  </div>
</div>
```
- Pass `depth` to `startResearch(q, strategy, timeLimit, depth)`
- Add collapsible notes panel in the running view (below tool log):
```tsx
{progress?.notes && (
  <details className="mt-3 rounded-xl border ...">
    <summary>Research Notes</summary>
    <div className="p-3 text-xs whitespace-pre-wrap">{progress.notes}</div>
  </details>
)}
```
- Update progress display: show `step` count instead of `round` where applicable

**Step 3:** Commit:
```bash
git add frontend/src/pages/ResearchPage.tsx frontend/src/contexts/ResearchContext.tsx
git commit -m "feat: research depth selector and notes panel"
```

---

### Task 6: Update research prompts for smolagents

**Files:**
- Modify: `src/harbor_clerk/llm/research.py` or `src/harbor_clerk/llm/research_tools.py`

**Step 1:** Define the system prompt for the agent. smolagents uses `prompt_templates` for customization. The prompt should:
- Describe the research task
- Instruct the agent to search broadly, vary queries, use citations
- Tell it to call `final_answer()` with a comprehensive summary of findings when done
- Differ by strategy (search vs. sweep — sweep gets a document list injected)

The prompt replaces `_ITERATION_SYSTEM_SEARCH` / `_ITERATION_SYSTEM_SWEEP`. It should NOT mention `<notes>` tags (smolagents manages its own memory). Instead:

```python
RESEARCH_SYSTEM_PROMPT = (
    "You are a research assistant. Systematically search the knowledge base "
    "to answer the user's question.\n\n"
    "## How to work\n"
    "- Search with varied queries to cover different angles\n"
    "- Read passages to verify and gather details\n"
    "- Use entity_search for people, places, organizations\n"
    "- Call multiple tools per step for efficiency\n\n"
    "## Citations\n"
    "Every finding MUST include its source: [Document Title, page X]\n"
    "Never fabricate citations.\n\n"
    "## Finishing\n"
    "When you have thoroughly covered the topic, call final_answer() with "
    "a comprehensive summary of ALL your findings, each with citations."
)
```

**Step 2:** Commit:
```bash
git add src/harbor_clerk/llm/research.py
git commit -m "feat: smolagents research system prompt"
```

---

### Task 7: Verify, lint, and build

**Step 1:** Run checks:
```bash
cd /Users/alex/mcp-gateway && uv run ruff check . && uv run ruff format --check .
cd frontend && npm run lint && npm run type-check && npm run format:check
```

**Step 2:** Build macOS apps:
```bash
cd /Users/alex/mcp-gateway/macos && make apps
```

**Step 3:** Commit any fixes.

---

### Task 8: Create PR, merge, clean up

```bash
git push -u origin feat/smolagents-research
gh pr create --title "feat: smolagents research integration — agent loop, depth control, notes streaming"
```

Watch CI, merge when green, clean up branch.

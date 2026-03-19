# Research Engine Rework — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace fixed 20-round research limit with wall-time limits, add notes condensation, and prompt for multi-tool-call rounds.

**Architecture:** Add `time_limit_minutes` to DB and API. Replace round-based loop termination with wall-time checks. Add a condensation LLM call that fires periodically. Update prompts to encourage batch tool calls. Update frontend to show time selector and elapsed time.

**Tech Stack:** Python (FastAPI, SQLAlchemy, Alembic), React/TypeScript, PostgreSQL

---

### Task 1: DB Migration — add `time_limit_minutes` column

**Files:**
- Create: `alembic/versions/0008_research_time_limit.py`
- Modify: `src/harbor_clerk/models/research_state.py`

**Step 1: Add column to SQLAlchemy model**

In `src/harbor_clerk/models/research_state.py`, add after `max_rounds`:

```python
time_limit_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
```

**Step 2: Create migration**

```python
# alembic/versions/0008_research_time_limit.py
"""Add time_limit_minutes to research_state."""

import sqlalchemy as sa

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("research_state", sa.Column("time_limit_minutes", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("research_state", "time_limit_minutes")
```

**Step 3: Commit**

```bash
git add src/harbor_clerk/models/research_state.py alembic/versions/0008_research_time_limit.py
git commit -m "feat: add time_limit_minutes column to research_state"
```

---

### Task 2: API Schema & Route — accept and return `time_limit_minutes`

**Files:**
- Modify: `src/harbor_clerk/api/schemas/research.py`
- Modify: `src/harbor_clerk/api/routes/research.py`
- Modify: `src/harbor_clerk/llm/models.py` (remove `DEFAULT_RESEARCH_MAX_ROUNDS` usage)

**Step 1: Update schemas**

In `src/harbor_clerk/api/schemas/research.py`:

- `StartResearchRequest`: add `time_limit_minutes: int = Field(default=30, ge=15, le=180)`
- `ResearchSummary`: add `time_limit_minutes: int | None = None`, keep `max_rounds` and `current_round` for backwards compat
- `ResearchDetail`: add `time_limit_minutes: int | None = None`
- `ResearchProgress`: add `time_limit_minutes: int | None = None`

**Step 2: Update POST /research route**

In `src/harbor_clerk/api/routes/research.py`, in `start_research()`:

- Read `body.time_limit_minutes` (default 30).
- Set `max_rounds=500` (safety cap) instead of the per-model setting.
- Pass `time_limit_minutes=body.time_limit_minutes` to `ResearchState(...)`.

**Step 3: Update GET /research/{id} response**

Add `time_limit_minutes=state.time_limit_minutes` to `ResearchDetail(...)` and `ResearchSummary(...)`.

**Step 4: Update GET /research list endpoint**

Add `time_limit_minutes` to each summary.

**Step 5: Commit**

```bash
git add src/harbor_clerk/api/schemas/research.py src/harbor_clerk/api/routes/research.py
git commit -m "feat: API accepts time_limit_minutes for research tasks"
```

---

### Task 3: Research engine — wall-time stopping condition

**Files:**
- Modify: `src/harbor_clerk/llm/research.py`

**Step 1: Add wall-time check to iteration loop**

At the top of `research_stream()`, after loading state, record start time:

```python
start_time = datetime.now(UTC)
time_limit_s = (state.time_limit_minutes or 30) * 60
hard_limit_s = time_limit_s + 600  # soft + 10min grace
```

In the `while current_round < max_rounds:` loop, add at the top (after incrementing `current_round`):

```python
elapsed = (datetime.now(UTC) - start_time).total_seconds()
if elapsed >= time_limit_s:
    logger.info(
        "Research time limit reached (%.0fs / %ds) — moving to synthesis",
        elapsed, time_limit_s,
    )
    state.notes = notes
    state.current_round = current_round
    state.heartbeat_at = datetime.now(UTC)
    await session.commit()
    break
```

**Step 2: Update SSE progress events**

Add `elapsed_seconds` and `time_limit_minutes` to the progress event dict:

```python
progress_event["elapsed_seconds"] = int((datetime.now(UTC) - start_time).total_seconds())
progress_event["time_limit_minutes"] = state.time_limit_minutes or 30
```

Keep `round` and `max_rounds` for activity log compatibility.

**Step 3: Commit**

```bash
git add src/harbor_clerk/llm/research.py
git commit -m "feat: wall-time stopping condition for research"
```

---

### Task 4: Notes condensation

**Files:**
- Modify: `src/harbor_clerk/llm/research.py`

**Step 1: Add condensation prompt and function**

Add near the other prompt constants:

```python
_CONDENSATION_SYSTEM = (
    "You are consolidating research notes. Tighten the text: remove "
    "redundancy, merge related findings, and improve organization. "
    "NEVER remove or modify citations — every [Document Title, page X] "
    "reference must be preserved exactly. Output only the consolidated "
    "notes inside <notes>...</notes> tags."
)

_CONDENSATION_ROUND_INTERVAL = 15
```

Add a helper function:

```python
async def _condense_notes(client, llm_url, notes, context_tokens, timeout=_ITERATION_TIMEOUT):
    """Condense notes via a dedicated LLM call. Returns condensed notes or original on failure."""
    messages = [
        {"role": "system", "content": _CONDENSATION_SYSTEM},
        {"role": "user", "content": f"<notes>\n{notes}\n</notes>"},
    ]
    try:
        content, _ = await _stream_llm_call(client, llm_url, messages, timeout=timeout)
        parsed = _parse_notes(content)
        if parsed and len(parsed) < len(notes):
            return parsed
    except Exception:
        logger.warning("Notes condensation failed, keeping original")
    return notes
```

**Step 2: Add condensation check in the iteration loop**

After the round checkpoint (after stall detection, before the loop continues), add:

```python
# Condense notes periodically or when they get large
notes_token_budget = int(context_tokens * 0.5 * _CHARS_PER_TOKEN)
if current_round % _CONDENSATION_ROUND_INTERVAL == 0 or len(notes) > notes_token_budget:
    logger.info(
        "Condensing notes: round=%d notes_len=%d budget=%d",
        current_round, len(notes), notes_token_budget,
    )
    # Run as task with keepalive
    condense_task = asyncio.create_task(
        _condense_notes(client, llm_url, notes, context_tokens)
    )
    while not condense_task.done():
        done, _ = await asyncio.wait({condense_task}, timeout=_KEEPALIVE_INTERVAL)
        if done:
            break
        state.heartbeat_at = datetime.now(UTC)
        await session.commit()
        yield ": keepalive\n\n"
    notes = condense_task.result()
    state.notes = notes
    await session.commit()
```

**Step 3: Commit**

```bash
git add src/harbor_clerk/llm/research.py
git commit -m "feat: periodic notes condensation in research"
```

---

### Task 5: Multi-tool-call prompting

**Files:**
- Modify: `src/harbor_clerk/llm/research.py`

**Step 1: Update iteration system prompts**

In `_ITERATION_SYSTEM_SEARCH`, update the "How to work" section:

```python
"## How to work\n"
"- Call multiple tools per round to explore different angles simultaneously\n"
"- For example: search with 3 different queries in one turn, or combine\n"
"  search_documents + read_passages + entity_search\n"
"- Search broadly first, then drill into promising results\n"
"- Use different search queries to cover different angles of the topic\n"
"- Read passages to verify and gather detail from search hits\n"
"- Use entity_search to find people, organizations, and places\n\n"
```

Apply the same change to `_ITERATION_SYSTEM_SWEEP`.

**Step 2: Commit**

```bash
git add src/harbor_clerk/llm/research.py
git commit -m "feat: prompt research model for multi-tool-call rounds"
```

---

### Task 6: Frontend — time limit selector in New Research form

**Files:**
- Modify: `frontend/src/pages/ResearchPage.tsx`
- Modify: `frontend/src/contexts/ResearchContext.tsx`

**Step 1: Update ResearchContext to accept `timeLimit`**

In `ResearchContext.tsx`, update `startResearch` signature:

```typescript
startResearch: (question: string, strategy?: string, timeLimitMinutes?: number) => Promise<void>
```

In the `startResearch` implementation, pass `time_limit_minutes` in the request body:

```typescript
if (timeLimitMinutes) body.time_limit_minutes = timeLimitMinutes
```

**Step 2: Add time limit state and selector in ResearchPage**

Add state: `const [timeLimit, setTimeLimit] = useState(30)`

Add time limit selector between the strategy toggle and the Start button:

```tsx
const TIME_LIMITS = [15, 30, 45, 60, 90, 120, 150, 180]

function formatTimeLimit(minutes: number): string {
  if (minutes < 60) return `${minutes}m`
  const h = Math.floor(minutes / 60)
  const m = minutes % 60
  return m > 0 ? `${h}h ${m}m` : `${h}h`
}

// In the form, between strategy toggle and Start button:
<div className="flex items-center justify-center gap-2">
  <span className="text-[12px] text-gray-500 dark:text-gray-400">Time limit</span>
  <select
    value={timeLimit}
    onChange={(e) => setTimeLimit(Number(e.target.value))}
    className="rounded-lg border-0 bg-gray-100 dark:bg-gray-800 text-[12px] px-2 py-1.5 text-gray-700 dark:text-gray-300"
  >
    {TIME_LIMITS.map((m) => (
      <option key={m} value={m}>{formatTimeLimit(m)}</option>
    ))}
  </select>
</div>
```

**Step 3: Pass `timeLimit` to `startResearch`**

In `handleStartResearch`:

```typescript
await startResearch(q, strategy, timeLimit)
```

**Step 4: Commit**

```bash
git add frontend/src/pages/ResearchPage.tsx frontend/src/contexts/ResearchContext.tsx
git commit -m "feat: time limit selector for research tasks"
```

---

### Task 7: Frontend — elapsed time in running view and metadata

**Files:**
- Modify: `frontend/src/contexts/ResearchContext.tsx`
- Modify: `frontend/src/pages/ResearchPage.tsx`

**Step 1: Update ResearchProgress interface**

In `ResearchContext.tsx`, add to `ResearchProgress`:

```typescript
elapsedSeconds?: number
timeLimitMinutes?: number
```

In `processStream`, update the `progress` case to read these from the SSE event:

```typescript
case 'progress':
  setProgress((prev) => ({
    round: event.round,
    maxRounds: event.max_rounds,
    strategy: event.strategy,
    reviewed: event.reviewed,
    total: event.total,
    elapsedSeconds: event.elapsed_seconds,
    timeLimitMinutes: event.time_limit_minutes,
    toolCalls: prev?.toolCalls || [],
  }))
  break
```

**Step 2: Update running view**

In `ResearchPage.tsx`, replace the round indicator in the running view with an elapsed time + time limit display:

```tsx
{/* Time indicator */}
<div className="text-center">
  <span className="text-[13px] font-semibold text-gray-700 dark:text-gray-300">
    {progress.elapsedSeconds != null
      ? `${formatElapsed(progress.elapsedSeconds)} / ${formatElapsed((progress.timeLimitMinutes || 30) * 60)}`
      : `Round ${progress.round}`}
  </span>
  {/* ... keep sweep progress bar if applicable ... */}
</div>
```

**Step 3: Update completed metadata**

In the completed view metadata section, replace rounds display:

```tsx
<span>
  Rounds: {selectedTask.current_round}
</span>
```

(Remove "/ max_rounds" since it's now a safety cap of 500, not meaningful to display.)

**Step 4: Commit**

```bash
git add frontend/src/contexts/ResearchContext.tsx frontend/src/pages/ResearchPage.tsx
git commit -m "feat: show elapsed time in research running view and metadata"
```

---

### Task 8: Verify & lint

**Step 1: Run Python checks**

```bash
cd /Users/alex/mcp-gateway && uv run ruff check . && uv run ruff format --check .
```

**Step 2: Run frontend checks**

```bash
cd /Users/alex/mcp-gateway/frontend && npm run lint && npm run type-check && npm run format:check
```

**Step 3: Fix any issues and commit**

**Step 4: Build macOS apps**

```bash
cd /Users/alex/mcp-gateway/macos && make apps
```

---

### Task 9: Create PR

```bash
git push -u origin feat/research-engine-rework
gh pr create --title "feat: research engine rework — wall-time limits, condensation, multi-tool prompting"
```

Watch CI, merge when green, clean up branch.

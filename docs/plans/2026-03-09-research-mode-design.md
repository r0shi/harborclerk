# Research Mode Design

## Overview

A separate Research tab where users submit a question and the LLM iterates
through the corpus autonomously, producing a monolithic report with citations.
Unlike interactive chat (Ask), research is a "submit and wait" experience —
the model churns through multiple rounds of tool calls, accumulates findings in
a scratchpad, then does a fresh-context synthesis pass to write the final report.

## Motivation

Interactive chat hits limits on broad, corpus-spanning queries ("What does my
corpus say about compliance?", "Find every mention of John Smith"). The model
exhausts its tool-call rounds before covering enough ground. Research mode
removes the round constraint and gives the model a structured way to iterate.

Local LLMs make this viable: tokens are free, so spending 5-10 minutes on
20 rounds of tool calls costs nothing but time.

## Data Model

### `conversations` table — new column

- `mode VARCHAR(10) NOT NULL DEFAULT 'chat'` — `'chat'` or `'research'`

Ask tab filters `mode='chat'`, Research tab filters `mode='research'`.

### `research_state` table (new)

One-to-one with conversation. Tracks research task lifecycle.

| Column | Type | Description |
|---|---|---|
| `conversation_id` | `UUID PK FK` | References `conversations.conversation_id` (cascade delete) |
| `strategy` | `VARCHAR(10) NOT NULL` | `'search'` or `'sweep'` |
| `status` | `VARCHAR(15) NOT NULL` | `'running'`, `'interrupted'`, `'completed'`, `'failed'` |
| `notes` | `TEXT` | Model's accumulated scratchpad (checkpointed each iteration) |
| `current_round` | `INT NOT NULL DEFAULT 0` | Current iteration number |
| `max_rounds` | `INT NOT NULL` | Hard cap for this task |
| `progress` | `JSONB` | Strategy-specific: `{"tools_called": N}` or `{"reviewed": N, "total": M}` |
| `completed_at` | `TIMESTAMPTZ` | When report was generated |
| `error` | `TEXT` | Error message if failed |

### `model_settings` table (new)

Per-model configuration overrides. Extensible JSONB pattern — starts with
research settings, will absorb `max_tool_rounds`, `temperature`, etc. over time.

| Column | Type | Description |
|---|---|---|
| `model_id` | `VARCHAR(50) PK` | Model identifier |
| `settings` | `JSONB NOT NULL DEFAULT '{}'` | Key-value overrides |

Lookup pattern: `model_settings[model_id].research_max_rounds ?? global_default`.

### Chat messages — no changes

Tool calls, tool results, and the final report are stored as regular
`chat_messages` rows. The scratchpad notes are additionally checkpointed to
`research_state.notes` for restart, but the full tool history lives in messages.

## Two Strategies

User-toggleable in the UI, with per-model defaults.

### Search-driven (agentic)

The model controls iteration — it decides what to search next based on what
it found. Better for thematic queries, relies on model intelligence.

Default for 8B+ models: qwen3-8b, deepseek-r1-8b, llama3.1-8b, gpt-oss-20b,
qwen3-30b-a3b.

### Systematic sweep

The system fetches the document list up front, feeds batches to the model each
round. The model extracts relevant findings from each batch. More mechanical,
guarantees coverage proportional to rounds used, works with smaller models.

Default for <8B models: qwen3-4b, phi4-mini, gemma3-4b, smollm3-3b.

### Defaults

20 max rounds for all models. The model can finish early. The sweep strategy
covers as many documents as it can within the round budget — if coverage is
incomplete, the report states what was and wasn't reviewed.

## Research Loop

### Iteration phase

Each iteration:

1. Build messages: research system prompt + user's original question +
   `<notes>{accumulated_notes}</notes>` + continuation instruction
2. For sweep mode: also inject current document batch to focus on
3. Call LLM with tools enabled, stream response
4. Parse updated `<notes>` from model's response
5. Checkpoint: save notes + round number to `research_state`
6. Save tool calls and results as `chat_messages`
7. Yield progress SSE event
8. If model produces `<report>` tag or calls no tools → done

### Synthesis phase

Fresh LLM call (clean context):
- Research synthesis system prompt
- User's original question
- Final accumulated notes
- "Write your final report with citations"

Stream report tokens as SSE events. Save as assistant message.

### Termination

- **Model-decided**: model stops calling tools and emits `<report>` tag
- **Hard cap**: if `current_round >= max_rounds`, force synthesis from
  whatever notes exist
- Both paths lead to the synthesis phase

## System Prompts

### Iteration prompt (search-driven)

```
You are a research assistant for Harbor Clerk. Your task is to systematically
search the knowledge base to thoroughly answer the user's question.

## How to work
- Search broadly first, then drill into promising results
- Use different search queries to cover different angles of the topic
- Read passages to verify and gather detail from search hits
- Use entity_search to find people, organizations, and places

## Notes rules
- Maintain your accumulated findings in a <notes> section at the end of
  every response
- Every finding MUST include its source: document title, page number, and
  chunk ID in parentheses
- When condensing notes, you may rephrase findings but NEVER remove citations
- Citations are the most important part of your notes — the final report
  depends on them

## Finishing
When you are confident you have thoroughly covered the topic, stop calling
tools and write ONLY a <report> tag. A separate synthesis step will produce
the final report from your notes.
```

### Iteration prompt (sweep) — additional section

```
## Current batch
Focus on the following documents this round. Search within them, read relevant
passages, and add any findings to your notes. Not every document will be
relevant — skip irrelevant ones quickly.
```

### Synthesis prompt

```
You are writing a research report for Harbor Clerk. Based on the research
notes below, write a clear, well-organized report answering the user's question.

## Guidelines
- Every claim must cite a source from the notes (document title, page number)
- If a finding has no citation, omit it
- Group findings by theme, not by document
- Be thorough but concise — include all relevant findings, skip filler
- If the evidence is contradictory or incomplete, say so
- Do not invent information not present in the notes
```

## Interruption & Restart

If the SSE connection drops or the user cancels:
- Loop stops, `research_state.status` set to `'interrupted'`
- Notes are already checkpointed (saved after each iteration)

When user returns to an interrupted task, three options:
- **Resume**: continue from checkpoint (load notes, resume at `current_round`)
- **Discard & Start New**: two-click inline confirmation (no `window.confirm`
  — WKWebView returns false). Deletes conversation + all data via cascade.
- Input area disabled until one is chosen

## API Endpoints

All under `/api/research`. JWT required, human users only (`require_human_user`
dependency). API keys cannot access research.

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/research` | Start new research task. Body: `{question, strategy}`. Returns SSE stream. 409 if another task is running. |
| `POST` | `/api/research/{conv_id}/resume` | Resume interrupted task. Returns SSE stream. |
| `DELETE` | `/api/research/{conv_id}` | Delete research task (any status). Cascade deletes messages. |
| `GET` | `/api/research` | List research tasks with status/progress. |
| `GET` | `/api/research/{conv_id}` | Get task detail: conversation + messages + research_state. |
| `GET` | `/api/research/active` | Check if a research task is running (for Ask tab blocker). |

Chat endpoint (`POST /api/chat/.../messages`) returns 409 when research is
running, with `{"error": "Research task in progress", "research_id": "..."}`.

## Frontend

### New tab

"Research" in the nav bar, between Ask and Upload. Route: `/research` (list)
and `/research/:id` (detail).

### Four UI states

**Idle** (no active task):
- Thinking octopus illustration (centered)
- Explanation: "Submit a question and Harbor Clerk will systematically search
  your documents to produce a comprehensive report. This may take several minutes."
- Text input + strategy toggle (Search-driven / Systematic sweep)
- "Start Research" button
- History list below: past research tasks (title, status, date)

**Running**:
- Thinking octopus (smaller, top)
- Progress section:
  - Search-driven: "Round 3 of 20" + tool call log (name + summary, stacking)
  - Sweep: "Reviewed 45 of 233 documents" + progress bar
- Cancel button

**Completed**:
- User's original question at top
- Report rendered as markdown
- Metadata: model, strategy, rounds, elapsed time
- Tool call history in collapsible disclosure

**Interrupted**:
- "This research task was interrupted"
- Resume button + "Discard & Start New" (two-click confirmation)
- Input disabled

### Ask tab busy blocker

On mount and before sending, check `GET /api/research/active`. If active:
- Overlay with thinking octopus illustration
- "Harbor Clerk is working on a research task"
- Link to Research tab
- Input disabled

### Chat fallback message

Update the "I used all tool calls" message in Ask to suggest:
"Try Research mode for broader questions."

## Progress Indicators

| Strategy | Indicator |
|---|---|
| Search-driven | Round N of M + live tool call log (tool name, summary) |
| Sweep | "Reviewed N of M documents" + progress bar |

## Per-Model Defaults

| Tier | Models | Default Strategy | Max Rounds |
|---|---|---|---|
| Large (8B+) | qwen3-8b, deepseek-r1-8b, llama3.1-8b, gpt-oss-20b, qwen3-30b-a3b | search | 20 |
| Small (<8B) | qwen3-4b, phi4-mini, gemma3-4b, smollm3-3b | sweep | 20 |

Stored in `model_settings` table. User can override strategy via UI toggle.

## File Summary

| File | Changes |
|---|---|
| `alembic/versions/0005_research_mode.py` | Migration: `mode` column, `research_state` table, `model_settings` table |
| `src/harbor_clerk/models/conversation.py` | Add `mode` column |
| `src/harbor_clerk/models/research_state.py` | New model |
| `src/harbor_clerk/models/model_settings.py` | New model |
| `src/harbor_clerk/llm/research.py` | `research_stream()` engine: iteration loop + synthesis |
| `src/harbor_clerk/api/routes/research.py` | 6 API endpoints |
| `src/harbor_clerk/api/routes/chat.py` | Filter conversations by `mode='chat'`; 409 when research active |
| `frontend/src/pages/ResearchPage.tsx` | New page: four states, progress, report display |
| `frontend/src/hooks/useResearch.ts` | SSE hook for research progress + report streaming |
| `frontend/src/components/Layout.tsx` | Add Research tab |
| `frontend/src/hooks/useChat.ts` | Handle 409 from research-active check |
| `frontend/src/pages/ChatPage.tsx` | Busy blocker overlay; updated fallback message |

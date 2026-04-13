# Tool Result Disclosure Triangles Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show full tool call results inline via disclosure triangles in both Ask and Research modes, with formatted views for common tools and a raw JSON toggle.

**Architecture:** Backend adds `raw_result` to SSE events and API responses. A new shared `ToolResultDisplay` component renders formatted + raw views. Chat's `ToolCallCard` and Research's `ToolLogEntry` integrate it.

**Tech Stack:** Python/FastAPI (SSE), React/TypeScript (frontend), Tailwind CSS

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `src/harbor_clerk/llm/chat.py:450` | Modify | Add `raw_result` to `tool_result` SSE event |
| `src/harbor_clerk/llm/research.py:386` | Modify | Add `raw_result` to `tool_result` SSE event |
| `src/harbor_clerk/api/routes/chat.py:42-69` | Modify | `_enrich_tool_calls` returns `raw_result` |
| `src/harbor_clerk/api/routes/chat.py:129-135` | Modify | Pass raw content alongside summary |
| `src/harbor_clerk/api/routes/research.py:134-141` | Modify | Pass raw content alongside summary |
| `frontend/src/contexts/ChatContext.tsx:26-30` | Modify | Add `rawResult` to `ToolCallInfo` |
| `frontend/src/contexts/ChatContext.tsx:168-186` | Modify | Populate `rawResult` from SSE |
| `frontend/src/contexts/ResearchContext.tsx:4-9` | Modify | Add `rawResult` to `ToolCallEntry` |
| `frontend/src/contexts/ResearchContext.tsx:103-115` | Modify | Populate `rawResult` from SSE |
| `frontend/src/components/ToolResultDisplay.tsx` | Create | Shared formatted + raw result renderer |
| `frontend/src/pages/ChatPage.tsx:801-883` | Modify | Add `ToolResultDisplay` to `ToolCallCard` body |
| `frontend/src/pages/ResearchPage.tsx:834-868` | Modify | Convert `ToolLogEntry` to disclosure triangle |
| `frontend/src/pages/ResearchPage.tsx:42-59` | Modify | `extractToolCalls` populates `rawResult` |

---

### Task 1: Backend — Add raw_result to SSE events

**Files:**
- Modify: `src/harbor_clerk/llm/chat.py:450`
- Modify: `src/harbor_clerk/llm/research.py:386`

- [ ] **Step 1: Add raw_result to chat SSE tool_result event**

In `src/harbor_clerk/llm/chat.py`, line 450, change:

```python
yield f"data: {json.dumps({'type': 'tool_result', 'name': fn_name, 'summary': summarize_tool_result(result_str)})}\n\n"
```

to:

```python
yield f"data: {json.dumps({'type': 'tool_result', 'name': fn_name, 'summary': summarize_tool_result(result_str), 'raw_result': result_str})}\n\n"
```

`result_str` is already available from line 448.

- [ ] **Step 2: Add raw_result to research SSE tool_result event**

In `src/harbor_clerk/llm/research.py`, line 386, change:

```python
yield f"data: {json.dumps({'type': 'tool_result', 'name': tc_name, 'summary': summary})}\n\n"
```

to:

```python
yield f"data: {json.dumps({'type': 'tool_result', 'name': tc_name, 'summary': summary, 'raw_result': observation})}\n\n"
```

`observation` is already available from line 383.

- [ ] **Step 3: Verify manually**

Start the app, open a chat, ask a question that triggers a search tool. Open browser DevTools Network tab, find the SSE stream, confirm `tool_result` events now include `raw_result` with full JSON.

- [ ] **Step 4: Commit**

```bash
git add src/harbor_clerk/llm/chat.py src/harbor_clerk/llm/research.py
git commit -m "feat: include raw_result in tool_result SSE events"
```

---

### Task 2: Backend — Add raw_result to API reload responses

**Files:**
- Modify: `src/harbor_clerk/api/routes/chat.py:42-69, 129-135`
- Modify: `src/harbor_clerk/api/routes/research.py:134-141`

- [ ] **Step 1: Change tool_results_by_id to store both summary and raw content (chat)**

In `src/harbor_clerk/api/routes/chat.py`, lines 129-135, change:

```python
tool_results_by_id: dict[str, str] = {}
for m in all_msgs:
    if m.role == "tool" and m.tool_call_id and m.content:
        tool_results_by_id[m.tool_call_id] = summarize_tool_result(m.content)
```

to:

```python
tool_results_by_id: dict[str, tuple[str, str]] = {}
for m in all_msgs:
    if m.role == "tool" and m.tool_call_id and m.content:
        tool_results_by_id[m.tool_call_id] = (summarize_tool_result(m.content), m.content)
```

- [ ] **Step 2: Update _enrich_tool_calls to accept and return raw_result**

In `src/harbor_clerk/api/routes/chat.py`, change the `_enrich_tool_calls` function (lines 42-69):

```python
def _enrich_tool_calls(tool_calls: list[dict], results: dict[str, tuple[str, str] | str]) -> list[dict]:
    """Add result summaries and raw results to tool calls.

    results values are either (summary, raw_content) tuples or plain summary strings
    for backwards compatibility.
    """
    enriched = []
    for tc in tool_calls:
        func = tc.get("function", {})
        name = func.get("name", tc.get("name", ""))
        raw_args = func.get("arguments", tc.get("arguments", {}))
        if isinstance(raw_args, str):
            try:
                args = json.loads(raw_args)
            except (json.JSONDecodeError, TypeError):
                args = {}
        else:
            args = raw_args
        tc_id = tc.get("id", "")
        result_val = results.get(tc_id)
        if isinstance(result_val, tuple):
            summary, raw_result = result_val
        else:
            summary = result_val
            raw_result = None
        enriched.append(
            {
                "name": name,
                "arguments": args,
                "result": summary,
                "raw_result": raw_result,
            }
        )
    return enriched
```

- [ ] **Step 3: Change tool_results_by_id in research detail endpoint**

In `src/harbor_clerk/api/routes/research.py`, lines 138-141, change:

```python
tool_results_by_id: dict[str, str] = {}
for m in all_msgs:
    if m.role == "tool" and m.tool_call_id and m.content:
        tool_results_by_id[m.tool_call_id] = _summarize_tool_result(m.content)
```

to:

```python
tool_results_by_id: dict[str, tuple[str, str]] = {}
for m in all_msgs:
    if m.role == "tool" and m.tool_call_id and m.content:
        tool_results_by_id[m.tool_call_id] = (_summarize_tool_result(m.content), m.content)
```

- [ ] **Step 4: Verify manually**

Reload a conversation that has tool calls. Check browser DevTools → Network → response for the conversation detail endpoint. Confirm tool_calls entries now include `raw_result` with full JSON content.

- [ ] **Step 5: Commit**

```bash
git add src/harbor_clerk/api/routes/chat.py src/harbor_clerk/api/routes/research.py
git commit -m "feat: include raw_result in tool call API responses"
```

---

### Task 3: Frontend — Update types and SSE handlers

**Files:**
- Modify: `frontend/src/contexts/ChatContext.tsx:26-30, 168-186`
- Modify: `frontend/src/contexts/ResearchContext.tsx:4-9, 103-115`
- Modify: `frontend/src/pages/ResearchPage.tsx:42-59`

- [ ] **Step 1: Add rawResult to ToolCallInfo (ChatContext)**

In `frontend/src/contexts/ChatContext.tsx`, lines 26-30, change:

```typescript
export interface ToolCallInfo {
  name: string
  arguments: Record<string, unknown>
  result?: string
}
```

to:

```typescript
export interface ToolCallInfo {
  name: string
  arguments: Record<string, unknown>
  result?: string
  rawResult?: string
}
```

- [ ] **Step 2: Populate rawResult in chat SSE handler**

In `frontend/src/contexts/ChatContext.tsx`, in the `tool_result` case (around line 177), change:

```typescript
{ name: event.name, arguments: {}, result: event.summary },
```

to:

```typescript
{ name: event.name, arguments: {}, result: event.summary, rawResult: event.raw_result },
```

- [ ] **Step 3: Add rawResult to ToolCallEntry (ResearchContext)**

In `frontend/src/contexts/ResearchContext.tsx`, lines 4-9, change:

```typescript
export interface ToolCallEntry {
  name: string
  arguments: Record<string, unknown>
  summary?: string
  round?: number
}
```

to:

```typescript
export interface ToolCallEntry {
  name: string
  arguments: Record<string, unknown>
  summary?: string
  rawResult?: string
  round?: number
}
```

- [ ] **Step 4: Populate rawResult in research SSE handler**

In `frontend/src/contexts/ResearchContext.tsx`, in the `tool_result` case (around line 109), change:

```typescript
toolCalls[i] = { ...toolCalls[i], summary: event.summary }
```

to:

```typescript
toolCalls[i] = { ...toolCalls[i], summary: event.summary, rawResult: event.raw_result }
```

- [ ] **Step 5: Add raw_result to ResearchMessage interface**

In `frontend/src/pages/ResearchPage.tsx`, lines 20-28, change:

```typescript
interface ResearchMessage {
  role: string
  content?: string
  tool_calls?: Array<{
    name: string
    arguments: Record<string, unknown>
    result?: string
  }>
}
```

to:

```typescript
interface ResearchMessage {
  role: string
  content?: string
  tool_calls?: Array<{
    name: string
    arguments: Record<string, unknown>
    result?: string
    raw_result?: string
  }>
}
```

- [ ] **Step 6: Map raw_result to rawResult in chat reload path**

In `frontend/src/pages/ChatPage.tsx`, line 150, change:

```typescript
tool_calls: (m.tool_calls as ToolCallInfo[] | undefined) || undefined,
```

to:

```typescript
tool_calls: m.tool_calls
  ? (m.tool_calls as Array<Record<string, unknown>>).map((tc) => ({
      name: tc.name as string,
      arguments: (tc.arguments as Record<string, unknown>) || {},
      result: tc.result as string | undefined,
      rawResult: tc.raw_result as string | undefined,
    }))
  : undefined,
```

- [ ] **Step 7: Populate rawResult in extractToolCalls (ResearchPage)**

In `frontend/src/pages/ResearchPage.tsx`, in `extractToolCalls` (around line 50), change:

```typescript
entries.push({
  name: tc.name,
  arguments: tc.arguments,
  summary: tc.result || undefined,
  round,
})
```

to:

```typescript
entries.push({
  name: tc.name,
  arguments: tc.arguments,
  summary: tc.result || undefined,
  rawResult: tc.raw_result || undefined,
  round,
})
```

- [ ] **Step 8: Commit**

```bash
git add frontend/src/contexts/ChatContext.tsx frontend/src/contexts/ResearchContext.tsx frontend/src/pages/ResearchPage.tsx frontend/src/pages/ChatPage.tsx
git commit -m "feat: add rawResult to tool call types and SSE handlers"
```

---

### Task 4: Frontend — Create ToolResultDisplay component

**Files:**
- Create: `frontend/src/components/ToolResultDisplay.tsx`

- [ ] **Step 1: Create the component file**

Create `frontend/src/components/ToolResultDisplay.tsx`:

```tsx
import { useState } from 'react'

interface ToolResultDisplayProps {
  rawResult: string
  toolName: string
}

// ---------------------------------------------------------------------------
// Tool-specific formatters
// ---------------------------------------------------------------------------

function SearchHitList({ data }: { data: Record<string, unknown> }) {
  const hits = (data.hits || data.results || []) as Record<string, unknown>[]
  if (!hits.length) return <p className="text-gray-400 italic">No results</p>
  return (
    <div className="space-y-1.5">
      {hits.map((hit, i) => (
        <div key={i} className="flex items-baseline gap-2">
          <span className="shrink-0 text-gray-400 tabular-nums w-4 text-right">{i + 1}.</span>
          <div className="min-w-0">
            <span className="font-medium text-gray-600 dark:text-gray-300">
              {(hit.doc_title as string) || 'Untitled'}
            </span>
            {hit.page != null && (
              <span className="ml-1 text-gray-400">p.{String(hit.page)}</span>
            )}
            {hit.score != null && (
              <span className="ml-1.5 text-gray-400/70">({Number(hit.score).toFixed(2)})</span>
            )}
            {hit.snippet && (
              <p className="text-gray-500 dark:text-gray-400 line-clamp-2 mt-0.5">
                {String(hit.snippet).slice(0, 150)}
              </p>
            )}
            {hit.text && !hit.snippet && (
              <p className="text-gray-500 dark:text-gray-400 line-clamp-2 mt-0.5">
                {String(hit.text).slice(0, 150)}
              </p>
            )}
          </div>
        </div>
      ))}
    </div>
  )
}

function PassageBlocks({ data }: { data: Record<string, unknown> }) {
  const passages = (data.passages || data.chunks || []) as Record<string, unknown>[]
  if (!passages.length) return <p className="text-gray-400 italic">No passages</p>
  return (
    <div className="space-y-2">
      {passages.map((p, i) => (
        <div key={i}>
          <div className="text-gray-500 dark:text-gray-400 text-[10px] uppercase tracking-wide mb-0.5">
            {(p.doc_title as string) || 'Untitled'}
            {p.page != null && <span> — p.{String(p.page)}</span>}
          </div>
          <p className="text-gray-600 dark:text-gray-300 whitespace-pre-wrap">
            {String(p.text || p.content || '')}
          </p>
        </div>
      ))}
    </div>
  )
}

function EntityList({ data }: { data: Record<string, unknown> }) {
  const entities = (data.entities || []) as Record<string, unknown>[]
  if (!entities.length) return <p className="text-gray-400 italic">No entities</p>
  return (
    <div className="flex flex-wrap gap-1.5">
      {entities.map((e, i) => (
        <span
          key={i}
          className="inline-flex items-center gap-1 rounded-full bg-gray-100 dark:bg-gray-700 px-2 py-0.5"
        >
          <span className="font-medium text-gray-600 dark:text-gray-300">{String(e.name || e.text || '')}</span>
          {e.type && (
            <span className="text-[10px] text-gray-400 uppercase">{String(e.type)}</span>
          )}
        </span>
      ))}
    </div>
  )
}

function GenericView({ data }: { data: unknown }) {
  if (data === null || data === undefined) return null
  if (typeof data === 'string' || typeof data === 'number' || typeof data === 'boolean') {
    return <span className="text-gray-600 dark:text-gray-300">{String(data)}</span>
  }
  if (Array.isArray(data)) {
    if (data.length === 0) return <span className="text-gray-400 italic">empty list</span>
    return (
      <ul className="list-disc pl-4 space-y-0.5">
        {data.map((item, i) => (
          <li key={i}><GenericView data={item} /></li>
        ))}
      </ul>
    )
  }
  if (typeof data === 'object') {
    const entries = Object.entries(data as Record<string, unknown>)
    if (entries.length === 0) return <span className="text-gray-400 italic">empty</span>
    return (
      <dl className="space-y-1">
        {entries.map(([key, val]) => (
          <div key={key}>
            <dt className="text-[10px] text-gray-400 uppercase tracking-wide">{key}</dt>
            <dd className="ml-2"><GenericView data={val} /></dd>
          </div>
        ))}
      </dl>
    )
  }
  return null
}

// ---------------------------------------------------------------------------
// Determine which formatter to use
// ---------------------------------------------------------------------------

const SEARCH_TOOLS = new Set([
  'search_documents', 'kb_search', 'kb_batch_search',
])
const PASSAGE_TOOLS = new Set([
  'read_passages', 'kb_read_passages', 'expand_context', 'kb_expand_context',
  'read_document', 'kb_read_document',
])
const ENTITY_TOOLS = new Set([
  'entity_search', 'kb_entity_search', 'entity_overview', 'kb_entity_overview',
  'entity_cooccurrence', 'kb_entity_cooccurrence',
])

function FormattedResult({ data, toolName }: { data: Record<string, unknown>; toolName: string }) {
  if (SEARCH_TOOLS.has(toolName)) return <SearchHitList data={data} />
  if (PASSAGE_TOOLS.has(toolName)) return <PassageBlocks data={data} />
  if (ENTITY_TOOLS.has(toolName)) return <EntityList data={data} />
  return <GenericView data={data} />
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export default function ToolResultDisplay({ rawResult, toolName }: ToolResultDisplayProps) {
  const [showRaw, setShowRaw] = useState(false)

  let parsed: unknown = null
  let parseOk = false
  try {
    parsed = JSON.parse(rawResult)
    parseOk = true
  } catch {
    // not JSON — show as plain text
  }

  if (!parseOk) {
    return (
      <div className="text-[11px] text-gray-500 dark:text-gray-400 whitespace-pre-wrap max-h-64 overflow-auto">
        {rawResult}
      </div>
    )
  }

  // Check for error responses
  const obj = parsed as Record<string, unknown>
  if (obj.error) {
    return (
      <div className="text-[11px] text-red-500 dark:text-red-400">
        Error: {String(obj.error)}
      </div>
    )
  }

  return (
    <div className="text-[11px]">
      {showRaw ? (
        <pre className="text-gray-400 dark:text-gray-500 whitespace-pre-wrap font-mono max-h-80 overflow-auto">
          {JSON.stringify(parsed, null, 2)}
        </pre>
      ) : (
        <FormattedResult data={obj} toolName={toolName} />
      )}
      <button
        onClick={() => setShowRaw(!showRaw)}
        className="mt-1.5 text-[10px] text-gray-400 hover:text-gray-500 dark:hover:text-gray-300 underline"
      >
        {showRaw ? 'Show formatted' : 'Show raw'}
      </button>
    </div>
  )
}
```

- [ ] **Step 2: Run lint**

```bash
cd frontend && npm run lint && npm run type-check
```

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/ToolResultDisplay.tsx
git commit -m "feat: add ToolResultDisplay component with formatted + raw views"
```

---

### Task 5: Frontend — Integrate into ToolCallCard (Chat)

**Files:**
- Modify: `frontend/src/pages/ChatPage.tsx:801-883`

- [ ] **Step 1: Add import**

At the top of `ChatPage.tsx`, add:

```typescript
import ToolResultDisplay from '../components/ToolResultDisplay'
```

- [ ] **Step 2: Replace the expanded body in ToolCallCard**

In the `ToolCallCard` component (around line 860), replace the expanded content block:

```tsx
{expanded && (
  <div className="border-t border-gray-100 dark:border-gray-700/50 px-2.5 py-1.5 text-[11px] bg-gray-50/50 dark:bg-gray-800/30 space-y-1">
    {tool.arguments && Object.keys(tool.arguments).length > 0 && (
      <div className="font-mono text-gray-400 dark:text-gray-500">{JSON.stringify(tool.arguments, null, 2)}</div>
    )}
    {tool.result && <div className="text-gray-500 dark:text-gray-400">{tool.result}</div>}
  </div>
)}
```

with:

```tsx
{expanded && (
  <div className="border-t border-gray-100 dark:border-gray-700/50 px-2.5 py-1.5 text-[11px] bg-gray-50/50 dark:bg-gray-800/30 space-y-2">
    {tool.arguments && Object.keys(tool.arguments).length > 0 && (
      <div>
        <div className="text-[10px] text-gray-400 uppercase tracking-wide mb-0.5">Arguments</div>
        <div className="font-mono text-gray-400 dark:text-gray-500">{JSON.stringify(tool.arguments, null, 2)}</div>
      </div>
    )}
    {tool.rawResult ? (
      <div>
        <div className="text-[10px] text-gray-400 uppercase tracking-wide mb-0.5">Result</div>
        <ToolResultDisplay rawResult={tool.rawResult} toolName={tool.name} />
      </div>
    ) : tool.result ? (
      <div className="text-gray-500 dark:text-gray-400">{tool.result}</div>
    ) : null}
  </div>
)}
```

This shows `ToolResultDisplay` when `rawResult` is available, falling back to the summary string for older data without raw results.

- [ ] **Step 3: Run lint and type-check**

```bash
cd frontend && npm run lint && npm run type-check
```

- [ ] **Step 4: Verify manually**

Open a chat conversation, ask a question. Expand a tool call card. Confirm:
- Arguments section shown with label
- Result section shows formatted view (search hits as a list with doc titles, scores, snippets)
- "Show raw" toggle switches to JSON
- "Show formatted" toggle switches back

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/ChatPage.tsx
git commit -m "feat: show full tool results in chat ToolCallCard disclosure"
```

---

### Task 6: Frontend — Convert ToolLogEntry to disclosure triangle (Research)

**Files:**
- Modify: `frontend/src/pages/ResearchPage.tsx:834-868`

- [ ] **Step 1: Add import**

At the top of `ResearchPage.tsx`, add:

```typescript
import ToolResultDisplay from '../components/ToolResultDisplay'
```

- [ ] **Step 2: Replace ToolLogEntry with disclosure triangle version**

Replace the `ToolLogEntry` component (lines 834-868) with:

```tsx
function ToolLogEntry({ tool, isLast }: { tool: ToolCallEntry; isLast: boolean }) {
  const isDone = !!tool.summary
  const hasResult = !!tool.rawResult

  const icon = !isDone && isLast ? (
    <svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M21 21l-5.197-5.197m0 0A7.5 7.5 0 105.196 5.196a7.5 7.5 0 0010.607 10.607z"
      />
    </svg>
  ) : (
    <svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 12.75l6 6 9-13.5" />
    </svg>
  )

  const iconColor = !isDone && isLast
    ? 'text-blue-500 dark:text-blue-400 animate-pulse'
    : 'text-emerald-500 dark:text-emerald-400'

  const summaryContent = (
    <div className="flex items-start gap-2 w-full">
      <span className={`shrink-0 mt-0.5 ${iconColor}`}>{icon}</span>
      <div className="min-w-0 flex-1">
        <span className="text-[12px] font-medium text-gray-600 dark:text-gray-300">{tool.name}</span>
        {tool.summary && (
          <span className="ml-1.5 text-[11px] text-gray-400 dark:text-gray-500 truncate">{tool.summary}</span>
        )}
      </div>
    </div>
  )

  if (!hasResult) {
    return <div className="px-3 py-2">{summaryContent}</div>
  }

  return (
    <details className="group">
      <summary className="px-3 py-2 cursor-pointer list-none flex items-center [&::-webkit-details-marker]:hidden">
        {summaryContent}
        <svg
          className="h-3 w-3 shrink-0 text-gray-300 dark:text-gray-600 transition-transform duration-150 group-open:rotate-90 ml-1"
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
          strokeWidth={2}
        >
          <path strokeLinecap="round" strokeLinejoin="round" d="M8.25 4.5l7.5 7.5-7.5 7.5" />
        </svg>
      </summary>
      <div className="px-3 pb-2 pl-8">
        <ToolResultDisplay rawResult={tool.rawResult!} toolName={tool.name} />
      </div>
    </details>
  )
}
```

- [ ] **Step 3: Run lint and type-check**

```bash
cd frontend && npm run lint && npm run type-check
```

- [ ] **Step 4: Verify manually**

Run a Research task. During live streaming:
- Tool calls without results yet show as flat rows (no triangle)
- Once a result arrives, the row becomes a disclosure triangle
- Expanding shows the formatted result
- After completion, reload the page — disclosure triangles still work with data from the API

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/ResearchPage.tsx
git commit -m "feat: show full tool results in research ToolLogEntry disclosure"
```

---

### Task 7: Final verification and PR

**Files:** None (verification only)

- [ ] **Step 1: Run full lint + type-check**

```bash
cd frontend && npm run lint && npm run type-check
```

- [ ] **Step 2: Run Python lint**

```bash
uv run ruff check . && uv run ruff format --check .
```

- [ ] **Step 3: End-to-end verification — Chat**

1. Open Harbor Clerk, start a new Ask conversation
2. Ask a question that triggers search + read passages
3. Verify tool call cards appear with disclosure triangles
4. Expand a search result card — confirm formatted hit list with titles, scores, snippets
5. Click "Show raw" — confirm JSON view with `max-height` scroll
6. Reload the page — confirm disclosures still work with API data

- [ ] **Step 4: End-to-end verification — Research**

1. Start a new Research task
2. Watch the activity log — tool calls appear as flat rows while in-flight
3. Once results arrive, rows become expandable disclosure triangles
4. Expand one — confirm formatted view
5. After completion, reload the page — confirm disclosures still work

- [ ] **Step 5: Push and create PR**

```bash
git push -u origin feat/tool-result-disclosure
gh pr create --title "feat: inline tool result disclosure triangles" --body "## Summary
- Add full tool results to SSE events and API responses (raw_result field)
- New ToolResultDisplay component with tool-specific formatted views + raw JSON toggle
- Chat ToolCallCard shows formatted results in disclosure body
- Research ToolLogEntry converts to disclosure triangle when result is available

Closes #TBD"
```

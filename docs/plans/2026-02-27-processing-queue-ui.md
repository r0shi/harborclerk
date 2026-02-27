# Processing Queue UI Redesign — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the per-stage queue tray with a per-document processing queue showing dual progress bars, expandable stage breakdown, and a completed section with stats and navigation links.

**Architecture:** Rewrite `useQueueTray` hook to group SSE events by `version_id` into `DocumentQueueItem` objects with computed overall progress. Rewrite panel components to render document rows with dual progress bars and expand/collapse. Add `doc_id`/`page_count`/`chunk_count` to the backend finalize event so completed items can show stats and link to the document page.

**Tech Stack:** React 19, TypeScript, Tailwind CSS 3.4, CSS @property for gradient transitions. Backend: Python, SQLAlchemy sync session, PostgreSQL NOTIFY.

**Design doc:** `docs/plans/2026-02-27-processing-queue-ui-design.md`

---

### Task 1: Backend — Enrich finalize done event

**Files:**
- Modify: `src/harbor_clerk/events.py` — add `doc_id`, `page_count`, `chunk_count` params
- Modify: `src/harbor_clerk/worker/stages/finalize.py` — query counts and pass to event
- Modify: `frontend/src/hooks/useJobEvents.ts` — extend `JobEvent` interface

**Step 1: Extend `publish_job_event` signature**

In `src/harbor_clerk/events.py`, add three optional params and include them in the payload:

```python
def publish_job_event(
    version_id: uuid.UUID,
    stage: str,
    status: str,
    progress: int | None = None,
    total: int | None = None,
    error: str | None = None,
    filename: str | None = None,
    doc_id: uuid.UUID | None = None,
    page_count: int | None = None,
    chunk_count: int | None = None,
) -> None:
```

Add to the payload dict (same pattern as existing optional fields):

```python
    if doc_id is not None:
        payload["doc_id"] = str(doc_id)
    if page_count is not None:
        payload["page_count"] = page_count
    if chunk_count is not None:
        payload["chunk_count"] = chunk_count
```

**Step 2: Query counts in finalize and pass to event**

In `src/harbor_clerk/worker/stages/finalize.py`, after the existing `session.commit()` (line 45) but before `session.close()`, query page and chunk counts:

```python
from sqlalchemy import func, select
from harbor_clerk.models import Document, DocumentPage, DocumentVersion, Chunk, Upload
```

After `session.commit()`, before `finally`:

```python
        page_count = session.execute(
            select(func.count()).select_from(DocumentPage).where(
                DocumentPage.version_id == version_id
            )
        ).scalar_one()
        chunk_count = session.execute(
            select(func.count()).select_from(Chunk).where(
                Chunk.version_id == version_id
            )
        ).scalar_one()
```

Then change `mark_stage_done` call — actually, `mark_stage_done` in `pipeline.py` calls `publish_job_event` itself. We need to pass these extra fields through. Instead, publish a *separate* enriched event right after `mark_stage_done` returns, or modify `mark_stage_done` to accept kwargs.

**Simpler approach:** Publish a supplementary event from `run_finalize` after `mark_stage_done` returns. The frontend merges both events (same version_id, stage=finalize, status=done):

```python
    mark_stage_done(version_id, JobStage.finalize)

    # Publish enriched event with stats for the queue UI
    publish_job_event(
        version_id,
        "finalize",
        "done",
        filename=filename,
        doc_id=doc.doc_id,
        page_count=page_count,
        chunk_count=chunk_count,
    )
```

Wait — `mark_stage_done` already publishes `finalize:done`. A second event would be redundant. Better: save the counts before `session.close()`, then pass them to `mark_stage_done` which passes through to `publish_job_event`.

**Cleanest approach:** Just override the event. Add the extra fields to `mark_stage_done`'s publish call by extending it. But `mark_stage_done` is generic for all stages. Let's keep it simple: in `run_finalize`, query the counts, save them, and after `mark_stage_done` publishes its event, publish a second "finalize_stats" event that the frontend merges. Actually that's messy.

**Final approach:** Query the counts in `run_finalize` before closing the session. Store them. After `mark_stage_done(version_id, JobStage.finalize)` completes (which publishes finalize:done), immediately publish a second event with just the stats. The frontend's `onEvent` handler merges both — the first creates the completed entry, the second enriches it with stats. Since both arrive on the same NOTIFY channel in rapid succession, this is reliable.

Actually simplest: just add `extra_fields: dict | None = None` to `mark_stage_done` and merge them into the publish call. Let's do that.

In `src/harbor_clerk/worker/pipeline.py`, modify `mark_stage_done`:

```python
def mark_stage_done(version_id: uuid.UUID, stage: JobStage, **extra_event_fields) -> None:
```

And in the publish call at the end:

```python
    publish_job_event(version_id, stage.value, "done", filename=filename, **extra_event_fields)
```

Then in `run_finalize`:

```python
    mark_stage_done(
        version_id, JobStage.finalize,
        doc_id=doc.doc_id, page_count=page_count, chunk_count=chunk_count,
    )
```

**Step 3: Extend frontend `JobEvent` interface**

In `frontend/src/hooks/useJobEvents.ts`, add to the interface:

```typescript
export interface JobEvent {
  version_id: string
  stage: string
  status: string
  progress?: number
  total?: number
  error?: string
  filename?: string
  doc_id?: string        // added
  page_count?: number    // added
  chunk_count?: number   // added
}
```

**Step 4: Commit**

```bash
git add src/harbor_clerk/events.py src/harbor_clerk/worker/stages/finalize.py \
  src/harbor_clerk/worker/pipeline.py frontend/src/hooks/useJobEvents.ts
git commit -m "Enrich finalize event with doc_id, page/chunk counts for queue UI"
```

---

### Task 2: Frontend — Rewrite useQueueTray hook

**Files:**
- Rewrite: `frontend/src/hooks/useQueueTray.ts`

The hook groups SSE events by `version_id` into document-level items with computed overall progress.

**Step 1: Define new types and rewrite the hook**

Replace the entire file. New types:

```typescript
export interface StageState {
  status: 'queued' | 'running' | 'done' | 'error' | 'skipped'
  progress?: number
  total?: number
}

export interface DocumentQueueItem {
  version_id: string
  filename: string
  stages: Map<string, StageState>
  current_stage: string
  overall_progress: number       // 0-100
  status: 'running' | 'queued' | 'error'
  updated_at: number
}

export interface CompletedItem {
  version_id: string
  doc_id?: string
  filename: string
  status: 'done' | 'error'
  error_stage?: string
  page_count?: number
  chunk_count?: number
  finished_at: number
}
```

Constants:

```typescript
const HISTORY_CAP = 20
const HISTORY_TTL = 3_600_000    // 1 hour (was 30s)
const TOAST_DURATION = 4_000
const TOAST_DEBOUNCE = 500
const PIPELINE_STAGES = ['extract', 'ocr', 'chunk', 'embed', 'summarize', 'finalize']
```

Key logic in `onEvent`:

1. Find or create `DocumentQueueItem` for `event.version_id`
2. Update `stages` map: set `event.stage` to `{status: event.status, progress: event.progress, total: event.total}`
3. If `event.stage === 'ocr'` and `event.status === 'done'` and event was instant (no running event before it), mark as `skipped`
4. Compute `current_stage`: the first stage in PIPELINE_STAGES that isn't `done`/`skipped`
5. Compute `overall_progress`: count completed stages + interpolate current stage's sub-progress, divide by total non-skipped stages
6. If `event.stage === 'finalize'` and `event.status === 'done'`: move item to completed list with `doc_id`, `page_count`, `chunk_count` from event

Overall progress formula:

```typescript
function computeOverallProgress(stages: Map<string, StageState>): number {
  const ordered = PIPELINE_STAGES.filter(s => {
    const state = stages.get(s)
    return !state || state.status !== 'skipped'
  })
  const total = ordered.length || 1
  let completed = 0
  for (const s of ordered) {
    const state = stages.get(s)
    if (!state || state.status === 'queued') break
    if (state.status === 'done') {
      completed += 1
      continue
    }
    if (state.status === 'running' && state.total && state.total > 0) {
      completed += (state.progress || 0) / state.total
    }
    break  // running stage is the current one
  }
  return Math.min(100, Math.round((completed / total) * 100))
}
```

Return value shape stays the same pattern but with new types:

```typescript
return {
  trayState,
  activeItems: Map<string, DocumentQueueItem>,
  completed: CompletedItem[],
  toggleExpanded,
  collapse,
}
```

**Step 2: Commit**

```bash
git add frontend/src/hooks/useQueueTray.ts
git commit -m "Rewrite useQueueTray: group by document, dual progress, 1hr TTL"
```

---

### Task 3: Frontend — New DocumentRow component

**Files:**
- Create: `frontend/src/components/queue-tray/DocumentRow.tsx`

**Step 1: Build the component**

Props:

```typescript
interface DocumentRowProps {
  item: DocumentQueueItem
}
```

Layout:
- Filename (truncated) + chevron button (right-aligned)
- Stage label: `stageLabel(item.current_stage)` + sub-progress text if available (e.g. "45 of 100")
- Stage progress bar: 3px, rounded-full, accent blue fill, smooth width transition
- Overall progress bar: 5px, rounded-full, gradient fill (blue→teal→green based on %), smooth transition
- Both bars have track: `bg-black/[0.04] dark:bg-white/[0.06]`

Expanded state (toggle via chevron):
- 6-item grid (2 columns, 3 rows) showing each pipeline stage
- `✓` green for done, `●` accent blue + label for running, `○` gray for queued, `–` gray for skipped
- Animate expand with `max-height` transition (grid-rows approach or max-height)

Stage progress bar fill color: `bg-[var(--color-accent)]`

Overall progress bar fill: use inline style with computed gradient. Since CSS @property may not work everywhere, use a simpler approach — pick from 3 color stops based on percentage:
- 0-60%: `#007aff` (accent blue)
- 60-90%: `#34aadc` (teal)
- 90-100%: `#30d158` (green)

Use `style={{ backgroundColor: progressColor(item.overall_progress) }}` with a helper that returns the interpolated color. Or simply: the bar itself is always a left-to-right gradient `linear-gradient(90deg, #007aff, #34aadc, #30d158)` and the width represents progress. This naturally shows more green as it fills. Simplest and looks great.

Queued items: both bars empty (just track), stage label shows "Queued".

**Step 2: Commit**

```bash
git add frontend/src/components/queue-tray/DocumentRow.tsx
git commit -m "Add DocumentRow component with dual progress bars and expand"
```

---

### Task 4: Frontend — New CompletedRow component

**Files:**
- Create: `frontend/src/components/queue-tray/CompletedRow.tsx`

**Step 1: Build the component**

Props:

```typescript
interface CompletedRowProps {
  item: CompletedItem
}
```

Layout:
- Row with: status icon (green checkmark or red X) + filename (truncated) + time ago (right-aligned)
- Second line: "N pages · M chunks" in secondary text (if available)
- Arrow link icon (→) on the right, links to `/docs/${item.doc_id}` if `doc_id` is present. Use `<Link>` from react-router.
- Error rows: add `border-l-2 border-red-500` and show "Error in {stage}" instead of page/chunk counts

Arrow hover: `hover:translate-x-1 hover:text-[var(--color-accent)]` with `transition-transform`

Reuse the `formatAge` function from the current QueuePanel (move it to a shared util or keep inline).

**Step 2: Commit**

```bash
git add frontend/src/components/queue-tray/CompletedRow.tsx
git commit -m "Add CompletedRow component with stats and document link"
```

---

### Task 5: Frontend — Rewrite QueuePanel

**Files:**
- Rewrite: `frontend/src/components/queue-tray/QueuePanel.tsx`

**Step 1: Rewrite the panel**

Update props to accept new types:

```typescript
interface QueuePanelProps {
  activeItems: Map<string, DocumentQueueItem>
  completed: CompletedItem[]
  onClose: () => void
}
```

Layout:
- Header: "Processing" title + active count badge + close button (same pattern as current)
- Active section: section header "Active" (only if items exist), render `DocumentRow` for each item. Sort: running first, then queued. Scrollable area.
- Divider: thin border between sections (only if both sections have items)
- Completed section: section header "Completed" + count, render `CompletedRow` for each. Scrollable.
- Empty state: "No items in queue" (same as current)

Keep the existing exit animation pattern (`panel-enter`/`panel-exit` classes).

**Step 2: Commit**

```bash
git add frontend/src/components/queue-tray/QueuePanel.tsx
git commit -m "Rewrite QueuePanel with document rows and completed section"
```

---

### Task 6: Frontend — Update QueueTray, QueuePill, QueueToastPopup

**Files:**
- Modify: `frontend/src/components/queue-tray/QueueTray.tsx`
- Modify: `frontend/src/components/queue-tray/QueuePill.tsx`
- Modify: `frontend/src/components/queue-tray/QueueToastPopup.tsx`

**Step 1: Update QueueTray**

Change destructured return from `useQueueTray` to use new names:

```typescript
const { trayState, activeItems, completed, toggleExpanded, collapse } = useQueueTray()
const activeCount = activeItems.size
const completedCount = completed.length
```

Pass `activeItems` and `completed` to `QueuePanel`. Pass `activeItems` to `QueueToastPopup`.

Update the "nothing to show" guard:

```typescript
if (activeCount === 0 && completedCount === 0 && trayState === 'collapsed') return null
```

**Step 2: Update QueuePill**

Change `historyCount` prop to `completedCount`. Update text from "N active" to "N processing":

```typescript
<span>{activeCount} processing</span>
```

History-only state shows completed count instead of generic history icon.

**Step 3: Update QueueToastPopup**

Change props to accept `DocumentQueueItem[]`. Show the latest running document's filename + current stage:

```typescript
interface QueueToastPopupProps {
  items: DocumentQueueItem[]
  onDismiss: () => void
}
```

Toast content: filename (bold) + stage label below. If multiple items, show "+N more".

**Step 4: Commit**

```bash
git add frontend/src/components/queue-tray/QueueTray.tsx \
  frontend/src/components/queue-tray/QueuePill.tsx \
  frontend/src/components/queue-tray/QueueToastPopup.tsx
git commit -m "Update QueueTray, Pill, Toast to use document-level items"
```

---

### Task 7: Frontend — CSS animations for progress bars and expand

**Files:**
- Modify: `frontend/src/index.css`

**Step 1: Add new animations after the existing queue tray section**

```css
/* ---- Queue progress bars ---- */

/* Overall progress bar gradient */
.queue-overall-bar {
  background: linear-gradient(90deg, #007aff 0%, #34aadc 50%, #30d158 100%);
  background-size: 200% 100%;
  transition: width 0.5s ease-out;
}

/* Stage progress bar */
.queue-stage-bar {
  transition: width 0.5s ease-out;
}

/* Indeterminate shimmer for stages without sub-progress */
.queue-shimmer {
  background: linear-gradient(
    90deg,
    var(--color-accent) 0%,
    rgba(0, 122, 255, 0.3) 50%,
    var(--color-accent) 100%
  );
  background-size: 200% 100%;
  animation: shimmer 1.5s ease-in-out infinite;
}
@keyframes shimmer {
  0% { background-position: 200% 0; }
  100% { background-position: -200% 0; }
}

/* Row expand/collapse */
.queue-row-detail {
  display: grid;
  grid-template-rows: 0fr;
  transition: grid-template-rows 0.2s ease-out;
}
.queue-row-detail.expanded {
  grid-template-rows: 1fr;
}
.queue-row-detail > div {
  overflow: hidden;
}

/* Chevron rotation */
.queue-chevron {
  transition: transform 0.2s ease-out;
}
.queue-chevron.expanded {
  transform: rotate(180deg);
}

/* Stage label crossfade */
.queue-stage-label {
  transition: opacity 0.15s ease;
}

/* Completed row fade-out before purge */
.queue-completed-exit {
  animation: completedFadeOut 0.3s ease-out forwards;
}
@keyframes completedFadeOut {
  to {
    opacity: 0;
    transform: translateX(-8px);
  }
}
```

**Step 2: Commit**

```bash
git add frontend/src/index.css
git commit -m "Add CSS animations for progress bars, expand, shimmer, fade-out"
```

---

### Task 8: Build verification and manual test

**Step 1: Build frontend**

```bash
cd frontend && npm run build
```

Expected: no TypeScript errors, clean build.

**Step 2: Verify no existing test regressions**

```bash
uv run pytest tests/test_heading_parser.py tests/test_extract_helpers.py -v
```

Expected: all pass (no backend test changes for the pipeline modification).

**Step 3: Manual test checklist**

Start the stack and upload a document. Verify:
- [ ] Pill shows "1 processing" with pulse
- [ ] Click pill → panel slides up
- [ ] Active section shows document filename with dual progress bars
- [ ] Stage bar fills as embed progresses
- [ ] Overall bar fills smoothly across stages with blue→teal→green gradient
- [ ] Click chevron → stage breakdown expands with checkmarks/dots
- [ ] On completion, item moves to Completed section with page/chunk counts
- [ ] Arrow link navigates to document detail page
- [ ] Completed items persist for up to 1 hour (cap 20)
- [ ] Click outside panel → collapses
- [ ] Dark mode: all elements render correctly

**Step 4: Final commit**

```bash
git add -A && git commit -m "Processing queue UI redesign: document-level progress tracking"
```

---

## Summary of changes

| # | Task | Files | Type |
|---|------|-------|------|
| 1 | Enrich finalize event | events.py, finalize.py, pipeline.py, useJobEvents.ts | Backend + interface |
| 2 | Rewrite useQueueTray | useQueueTray.ts | Hook rewrite |
| 3 | DocumentRow component | DocumentRow.tsx (new) | New component |
| 4 | CompletedRow component | CompletedRow.tsx (new) | New component |
| 5 | Rewrite QueuePanel | QueuePanel.tsx | Component rewrite |
| 6 | Update Tray/Pill/Toast | QueueTray.tsx, QueuePill.tsx, QueueToastPopup.tsx | Component updates |
| 7 | CSS animations | index.css | Styles |
| 8 | Build + verify | — | Verification |

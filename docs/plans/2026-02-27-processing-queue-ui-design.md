# Processing Queue UI Redesign

**Date:** 2026-02-27
**Status:** Approved

## Problem

The current queue tray tracks jobs per-stage (e.g. "extracting version X"), which becomes noisy with multiple documents in the pipeline. There's no document-level progress view, no overall pipeline progress, and completed items vanish after 30s with no stats.

## Design

### Core Change

Group by **document** (version_id) instead of by stage. Each document in the queue shows dual progress bars and an expandable stage breakdown.

### Data Model

```typescript
interface DocumentQueueItem {
  version_id: string
  filename: string
  stages: Map<string, StageState>   // stage -> {status, progress, total}
  current_stage: string             // e.g. "embed"
  overall_progress: number          // 0-100, smooth fill
  status: 'running' | 'queued' | 'error'
}

interface StageState {
  status: 'queued' | 'running' | 'done' | 'error' | 'skipped'
  progress?: number
  total?: number
}

interface CompletedItem {
  version_id: string
  doc_id?: string                   // for navigation link
  filename: string
  status: 'done' | 'error'
  error_stage?: string
  page_count?: number
  chunk_count?: number
  finished_at: number
}
```

### Overall Progress Calculation

Equal-weight stages with smooth interpolation:
- Pipeline has 5 or 6 stages (OCR may be skipped)
- Start assuming 6 stages; collapse to 5 when OCR completes as "skipped"
- Within a stage with sub-progress (e.g. embed 45/100): `(stage_index + 0.45) / total_stages * 100`
- Stages without sub-progress: 0% until done, then 100% of their share

### Component Hierarchy

```
QueueTray                          (orchestrator)
├── QueuePill                      (bottom-left trigger)
├── QueueToastPopup                (auto-dismiss, shows doc name + stage)
└── QueuePanel                     (slide-up, frosted glass)
    ├── PanelHeader                (title + active count + collapse)
    ├── ActiveSection              (scrollable)
    │   └── DocumentRow[]          (per version_id)
    │       ├── DocumentRowSummary (filename, stage label, dual bars)
    │       └── DocumentRowDetail  (stage breakdown, expandable)
    ├── SectionDivider
    └── CompletedSection           (scrollable, capped)
        └── CompletedRow[]         (filename, time, stats, link)
```

### Panel Layout

- **Width:** 360px fixed
- **Max height:** 60vh
- **Position:** bottom-16 left-4 (above pill)
- **Style:** rounded-2xl, backdrop-blur-xl, bg-vibrancy, shadow-mac-lg
- **Dismiss:** click outside or click pill

#### Active Section

- Running items first, then queued
- Each row: filename (truncated, 1 line), stage label ("Embedding 45 of 100")
- **Stage bar:** 3px, rounded-full, accent blue (#007aff), or indeterminate shimmer if no sub-progress
- **Overall bar:** 5px, rounded-full, gradient that shifts from blue (#007aff) through teal (#34aadc) to green (#30d158) as progress increases. Uses CSS @property for smooth gradient transitions.
- **Bar track:** rgba(0,0,0,0.04) light / rgba(255,255,255,0.06) dark
- **Expand:** click chevron -> stage-by-stage breakdown:
  - `✓` done, `● ` running (with count if available), `○` pending, `–` skipped
  - Two-column layout: 3 stages per column

#### Completed Section

- Section header: "Completed" with count
- Each row: green checkmark (or red X for error) + filename + "Nm ago" + "N pages, M chunks" + arrow link to /docs/{doc_id}
- Error rows: red left border (2px), error stage shown
- **Cap:** 20 items, 1 hour TTL
- **Purge:** fade-out animation (300ms) before DOM removal

### Pill

- "N items processing" with existing pillPulse animation
- Odometer-style count transition (vertical slide)
- Zero state: fades to secondary text, no pulse

### Micro-Interactions

| Element | Animation |
|---------|-----------|
| Panel open/close | Existing panelSlideUp/Down (0.25s) |
| Progress bars | `transition: width 0.5s ease-out` |
| Overall bar gradient | CSS @property transition on gradient colors (1s ease) |
| Stage label change | Opacity crossfade (150ms) |
| Row expand/collapse | max-height + chevron rotation (200ms) |
| Completion transition | Row shrinks + fades out, appears in Completed with highlight flash |
| New active item | Slide in from left (150ms stagger) |
| Completed link arrow | Slides right 4px on hover |
| History purge | Opacity fade-out (300ms) |

### Backend Change

The SSE `JobEvent` needs additional fields on the `finalize:done` event:
- `doc_id` — for navigation links
- `page_count` — from document_pages count
- `chunk_count` — from chunks count

Add these to `publish_job_event()` in `events.py`, populated in the finalize stage.

### Dark Mode

All colors use CSS variables. Progress bar tracks swap. Frosted glass uses `bg-[var(--bg-vibrancy)]` which is already dark-mode aware. Section headers, timestamps, and secondary text use `--color-text-secondary`.

### Files to Change

**Backend (1 file):**
- `src/harbor_clerk/worker/stages/finalize.py` — include doc_id, page_count, chunk_count in the done event

**Frontend (rewrite 4, new 3):**
- `frontend/src/hooks/useQueueTray.ts` — rewrite: group by version_id, compute overall progress, new data model
- `frontend/src/components/queue-tray/QueuePill.tsx` — update: odometer count transition
- `frontend/src/components/queue-tray/QueueToastPopup.tsx` — update: show doc name + stage
- `frontend/src/components/queue-tray/QueuePanel.tsx` — rewrite: new layout with sections
- `frontend/src/components/queue-tray/DocumentRow.tsx` — new: active item with dual bars + expand
- `frontend/src/components/queue-tray/CompletedRow.tsx` — new: completed item with stats + link
- `frontend/src/index.css` — add: gradient bar animations, expand transition

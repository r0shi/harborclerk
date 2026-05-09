# Summarize-as-Background-Stage UI Design

**Status:** Spec
**Date:** 2026-05-09
**Backend dependency:** [PR #327 — decouple summarize from finalize gate; yield-to-interactive](https://github.com/r0shi/harborclerk/pull/327)

## Goal

Surface the new architectural reality — summarize is a non-blocking background side-channel, not part of the ingest gate — across the four UI surfaces it touches. After this design lands, the user-visible "Done / Ready" badge reflects retrieval-readiness only; summary state is shown separately and clearly de-emphasised.

## Mental model

- **Pipeline** = `extract → ocr → chunk → {entities, embed} → finalize → Ready`. Six stages. Once Ready, the doc is fully retrievable (FTS, vector search, entity search). This is the "express line."
- **Summarize** = a background side-channel that branches off after `chunk`. Runs on the dedicated `llm` queue, yields to interactive chat/research, doesn't gate Ready. This is the "local line."
- The pill / chip / diagram / chart treat these as different categories. We never merge their counts unless a user explicitly drills in.

The colour contract is fixed:
- **Green** (`#30d158`) = Ready / Done. Reserved for "ingest complete, retrievable."
- **Purple** (`#c4a4ff`) = LLM / summary work. Already the diagram's `LLM-purple`; carry it through chips, bars, sections, pill, dashboard.
- **Yellow / amber** (`#ffd60a` / `#ff9f0a`) = warning states for summary specifically — Pending and Extractive Only.
- **Red** (`#ff453a`) = Failed.

## Surface 1: Per-doc row chip (DocumentsPage)

The document row keeps its existing layout (title, pipeline pill, status text, updated timestamp) but the pipeline pill drops from 7 segments to 6 (no summarize). A new chip appears in a fixed-width column to the right of the status, showing summary state.

### Layout

```
[ title — flex, ellipsis ] [ pill 110px ] [ status 70px ] [ chip 170px ]
                              6 segments      "Ready"        left-aligned chip
```

The chip column is **fixed at 170px** with the chip itself `justify-self: start`. This guarantees three vertical alignments across rows:
1. The "Ready" status text starts at the same X.
2. The chip's left edge starts at the same X.
3. The chip's coloured dot sits at the same X.
The right margin is uneven (chip text varies in width). This is intentional and matches the "chips line up on their dots" feel.

### Five chip states

| State | Trigger | Colour | Dot | Label |
|--|--|--|--|--|
| **Summary Generating** | Summarize stage running | Purple `#c4a4ff` | Pulsing | `Summary Generating` |
| **Summary Pending** | Queued, not yet started | Yellow `#ffd60a` | Solid | `Summary Pending` |
| **Extractive Only** | Fell back to extractive heuristic | Amber `#ff9f0a` | Solid | `Extractive Only` |
| **Summarized** | LLM summary persisted successfully | Green `#30d158` | Solid | `Summarized` |
| **Summary Failed** | Generation raised / bailed | Red `#ff453a` | Solid | `Summary Failed` |

### Data source

Three fields per doc determine the state:

| Field | Type | Meaning |
|--|--|--|
| `summary` | `string \| null` | Persisted summary text |
| `summary_model` | `string \| null` | Which LLM produced it. `"extractive"` is the heuristic-fallback sentinel. |
| `summarize_job_status` | `JobStatus` *(derived)* | Status of the most recent IngestionJob row for `stage='summarize'` |

Resolution order:
1. `summarize_job_status == 'running'` → **Summary Generating**
2. `summarize_job_status == 'queued'` → **Summary Pending**
3. `summarize_job_status == 'error'` → **Summary Failed**
4. `summary` is non-empty AND `summary_model == 'extractive'` → **Extractive Only**
5. `summary` is non-empty AND `summary_model != 'extractive'` → **Summarized**
6. Otherwise → no chip rendered (e.g. doc still mid-ingest, before summarize was enqueued)

The frontend already loads documents with `summary` and `summary_model`; it needs to also load the latest summarize-job status. The cleanest place is to extend `GET /api/docs` to include `summarize_job_status` for each row.

## Surface 2: Queue tray

The queue tray drawer gets a new **Summarizing** section between Active and Completed. The always-visible **QueuePill** gets a new "summarizing" rest state.

### Drawer layout

```
┌─ tabs (Active | Pipeline) ───────────────────────────────┐
│                                                          │
│  Active (N)                                              │
│    rows with 6-segment pipeline bars (existing)          │
│  ─────                                                   │
│  Summarizing (N)                                         │
│    rows with single purple progress bar                  │
│  ─────                                                   │
│  Completed (N)                                           │
│    existing                                              │
│  ─────                                                   │
│  Errors (N) — includes failed summary jobs               │
│    existing                                              │
└──────────────────────────────────────────────────────────┘
```

Section visibility rules:
- **Summarizing**: hidden when backlog is 0 (no empty header).
- Failed summary jobs land in **Errors**, not Summarizing.
- Sections render in the order: Active → Summarizing → Completed → Errors.

### Summarizing row

Each row shows: `name`, single-track purple progress bar (60×4px), and a state label (right-aligned, 60px column).

Progress bar mechanics:
- Container width 60px, height 4px, `display: inline-block`.
- Inner fill `display: block`, width = `chunks_done / total_chunks` where `total_chunks = map_count + 1` (reduce counted as one chunk, weighted equally with each map call).
- No visible segment dividers. The fill simply jumps to the next discrete percentage as each LLM call lands.
- `running` state adds a 1.6s opacity pulse to the fill; `queued` is static.
- Short / medium tier docs (single LLM call, no map-reduce) stay at 0% with pulse while running and jump to 100% on completion. They typically flash by; this is acceptable.

State labels: `queued`, `map N/M`, `reduce`, `running` (short/medium tier — no map step).

Virtualization: when `summarizing.length > 50`, render via `@tanstack/react-virtual` exactly like the Active section. No new infrastructure.

### QueuePill state machine

Priority: `Active > Summarizing > Idle`.

| Condition | Pill text | Colour | Animation |
|--|--|--|--|
| Active count > 0 | `N processing` | Amber dot | Pulse |
| Active count = 0, summarize backlog > 0 | `Summarizing N` | Purple text + soft purple glow | Pulse |
| Both empty | `Queue idle` | Muted dot | None |

When ingest is active, the summary count is **hidden** from the pill. Drilling into the drawer is how the user sees it. This keeps the always-visible button focused on the most attention-worthy work.

### Data source

The queue tray hook (`useQueueTray`) already fetches active/completed/errors. It needs to additionally fetch:
- A list of in-flight summarize jobs (queued + running) with per-job progress: doc id, filename, `total_chunks`, `chunks_done`, `state` (queued/running/map N/M/reduce).
- The `summarize_backlog_count` for the QueuePill.

The simplest API shape: extend the existing queue stream / snapshot endpoint to include a `summarizing` array alongside the existing `active`. Worker-side, summarize stage updates an existing field on its `IngestionJob` row to record map progress (e.g. `progress_current` / `progress_total`, which already exist on the row but are presently unused for summarize).

## Surface 3: Observatory — Pipeline Diagram

The diagram splits into two visually-separated canvases.

### Top canvas — main flow

A complete linear pipeline that ends at `Ready`:

```
Extract ──► OCR ──► Chunk ──► Entities ─┐
                              └─► Embed ┴─► Finalize (Ready)
```

Six nodes, no summarize. Existing colour palette (IO blue / CPU amber) for nodes; system-green edges; existing particle animation for throughput. Same SVG layout module, just with summarize removed from `NODE_POSITIONS` and the relevant edges dropped.

The Finalize node gets a `Ready` badge above it (the same green tag the user already sees on doc rows), explicitly cementing "this is the terminus of the express line."

### Bottom canvas — Background panel

A horizontally-aligned card below a dotted separator, labelled **BACKGROUND** in small purple capitals:

```
┌─ BACKGROUND ────────────────────────────────────────────┐
│   ●  Summarize    12 queued · ~3.4 docs/min              │
└──────────────────────────────────────────────────────────┘
```

A single circular node (purple, sized like the main-flow nodes) with the label `Summarize` and a one-line stat: `<queued> queued · <throughput> docs/min`. No connecting edge to the main flow. The "where does the work come from?" link is implicit — the BACKGROUND label and node colour are the contract.

### Particles

Particle animation on summarize keeps the existing visual language (purple particles flowing along edges). Particles in the bottom canvas flow along the implicit "in" arrow from the left edge of the panel to the Summarize node, then dissipate. No outgoing edge — summary terminates at the side panel.

## Surface 4: Observatory — Pipeline Timing Chart + Summary Backlog widget

### Avg Pipeline Timing chart

Existing horizontal bar chart, **summarize bar removed**. Six rows: Extract, OCR, Chunk, Entities, Embed, Finalize. Same hueing rules (IO blue / CPU amber). The chart now reads as honest "ingest timing" without the map-reduce outlier dragging the visual scale.

### Summary Backlog widget (new)

A separate panel rendered below the timing chart. Three slots arranged in a row:

```
┌─ Summary Backlog ─────────────────────────────────────────┐
│                                                           │
│   ┌──────────┐   ┌──────────┐   ┌────────────────────┐    │
│   │ 3.4/min  │   │   28s    │   │  12  ┃ trend chart │    │
│   │ Throughput│   │   p50    │   │ In Queue            │    │
│   └──────────┘   └──────────┘   └────────────────────┘    │
└───────────────────────────────────────────────────────────┘
```

- **Throughput** (left): docs-per-minute over the recent window. Single big number with `/min` suffix.
- **p50** (middle): median summary wall time. Single big number with `s` / `m` suffix.
- **In Queue** (right): a composite widget — current depth on the left, a sparkline of depth over the last hour on the right, separated by a thin purple vertical divider. Visually one card with two sections. Sparkline shows whether the backlog is growing or catching up; an inline label `↘ catching up` / `↗ falling behind` annotates the trend.

All three big numbers in purple (`#c4a4ff`). Section heading colour also purple to mark the lane.

### Data source

The Observatory page already fetches stage-timing snapshots. It needs to additionally fetch:
- `summary_throughput_per_min` (number, recent window)
- `summary_p50_seconds` (number, recent window)
- `summary_queue_depth` (number, current)
- `summary_queue_depth_history` (array of `[timestamp, depth]` pairs, last hour, sparkline-friendly resolution)

The simplest API shape: a single new endpoint `GET /api/system/summary-backlog` returning all four fields.

## Cross-cutting concerns

### Re-summarize-all

The existing `/api/system/resummarize-all` endpoint enqueues summarize jobs for every active Ready doc. With these UI changes:
- Doc rows immediately flip to **Summary Pending** chip (yellow). They stay Ready — pipeline_status doesn't regress (already implemented in PR #327).
- Queue tray's Summarizing section fills up; pill flips to `Summarizing N` if ingest happens to be idle.
- Observatory backlog widget shows the spike on the sparkline.

No additional UI work needed for this flow.

### Reprocess

Watcher reprocess deletes job rows and bumps `pipeline_seq`. UI behaviour:
- Row's pipeline pill resets to mid-ingest. Summary chip disappears (no `summarize_job_status` until summarize is re-enqueued).
- Once re-ingest reaches the post-chunk fan-out, summary re-enqueues and the chip cycles **Pending → Generating → Summarized** again.

### Failed summary

- Doc row shows red **Summary Failed** chip.
- Queue tray Errors section shows the failed summarize job (existing behaviour for any errored stage).
- Observatory: no special treatment. The failed job shows up in standard error metrics; throughput drops naturally.

### LLM not configured / deactivated

Summarize stage is enqueued but worker can't run it (model not loaded). Job sits in `queued` indefinitely. Doc row shows **Summary Pending** (yellow). No new edge case — this matches today's behaviour. Consideration: a future enhancement could surface "Summary blocked: no LLM model" but that's out of scope here.

## Files affected

| File | Change |
|--|--|
| `frontend/src/pages/DocumentsPage.tsx` | Add `SummaryChip` rendering; update row grid to fixed chip column |
| `frontend/src/components/SummaryChip.tsx` *(new)* | 5-state chip component |
| `frontend/src/components/queue-tray/StageBar.tsx` | 7 → 6 segments (drop summarize from `PIPELINE_STAGES`) |
| `frontend/src/components/queue-tray/QueuePanel.tsx` | Add Summarizing section between Active and Completed |
| `frontend/src/components/queue-tray/SummarizingRow.tsx` *(new)* | Single-track purple progress bar row |
| `frontend/src/components/queue-tray/QueuePill.tsx` | Add `summarizing N` rest state with priority logic |
| `frontend/src/hooks/useQueueTray.ts` | Fetch `summarizing[]` array; expose `summarizeBacklog` count |
| `frontend/src/components/stats/PipelineDiagram.tsx` | Drop summarize from main canvas; render BACKGROUND panel below |
| `frontend/src/components/stats/PipelineTimingChart.tsx` | Remove summarize from `STAGES` |
| `frontend/src/components/stats/SummaryBacklogPanel.tsx` *(new)* | Throughput · p50 · composite In Queue + sparkline |
| `frontend/src/pages/ObservatoryPage.tsx` | Render `SummaryBacklogPanel` below the timing chart |
| `src/harbor_clerk/api/routes/documents.py` | Add `summarize_job_status` to doc list response |
| `src/harbor_clerk/api/routes/system.py` | New `/system/summary-backlog` endpoint |
| `src/harbor_clerk/api/routes/jobs.py` | Add `summarizing[]` array to queue snapshot/SSE |
| `src/harbor_clerk/worker/stages/summarize.py` | Update `IngestionJob.progress_current/total` as map-reduce progresses |

## Out of scope

- A "retry summary" UI affordance on the failed-chip row. The existing `/api/docs/{id}/resummarize` endpoint exists; surfacing it in the UI is a follow-up.
- Per-doc detail page summary section — kept as-is for this round.
- Multi-language summary handling.
- "Summary blocked: no LLM" surfacing.

## Test plan

- Backend: extend existing `tests/test_documents.py` and `tests/test_system.py` with assertions for the new API fields.
- Frontend: visual regression by hand-eye on Documents / queue tray / Observatory pages with seeded states (Generating / Pending / Extractive Only / Summarized / Failed).
- Performance: the Documents page already paginates client-side; rendering 100 chips per page is negligible. The Summarizing section virtualizes at 50 rows, same threshold as Active.

# Summarize stage card on the Observatory pipeline diagram

**Date:** 2026-05-16
**Worktree:** `thirsty-swanson-b37bfc`
**Status:** Design approved, pending spec review

## Problem

The Observatory's "Live activity" pipeline diagram renders the six main-flow stages (Extract → OCR → Chunk → Entities/Embed → Finalize) as animated circles inside a 600×320 SVG. The seventh stage, **summarize**, is intentionally not part of the express line — it runs on the LLM queue at its own pace as a "background" stage. Today it's represented by a single thin horizontal strip below the SVG:

```
BACKGROUND  ○  Summarize                                 31 queued
```

The strip carries one number and disappears as a footnote next to a richly animated graph. Users have no view into **what** is summarizing, **how far along** each document is, or **which LLM** is doing the work — all of which would be valuable when summarize is the practical bottleneck of the ingestion pipeline.

The empty lower-left quadrant of the SVG (where the express line ends before the fan-out begins) is dead space. That's where the new card goes.

## Goals

1. Surface the summarize stage as a first-class element of the Live activity panel, not a footnote.
2. Reuse the existing visual vocabulary (ring + pulsing inner particle + queue-class colour) so the summarize stage reads as **a stage**, just one that runs independently.
3. Expose three new pieces of state: per-document queue/progress, the active LLM model name, and the running/queued split.
4. Make the "background, runs at its own pace" framing **more** visually explicit, not less, by enclosing summarize in its own purple-tinted bubble that is spatially separated from the main flow.

## Non-goals

- Connecting the summarize card back into the SVG with an edge. The existing diagram comment is explicit that "Ready is the terminus of the express line; summarize is independent" — adding an edge contradicts the framing.
- Per-document map-step durations. The existing `map N/M` label is enough granularity.
- Replacing the queue tray's `Summarizing` section. That's the info-dense per-doc list; the Observatory card is the at-a-glance status.
- Changes to the snapshot endpoint's payload — `summarizing[]` is already populated with the data we need.

## Layout

The card lives in the lower-left empty quadrant of the SVG, positioned via absolute overlay on a `relative` parent. The whole pipeline graph shifts up by ~30 viewBox units to give the card more vertical headroom.

```
┌────────────────────────────────────────────────────────────────────┐
│ Live activity                                                      │
│                                                                    │
│                                   ○                                │
│                                Entities          Ready             │
│   ○─────────○─────────○─────              ○                        │
│ Extract   OCR      Chunk    \    /     Finalize                    │
│                              ─ ─                                   │
│                                ○                                   │
│                              Embed                                 │
│ ┌──────────────────────────────────┐                              │
│ │  BACKGROUND                      │                              │
│ │                                  │                              │
│ │   ╭───╮   Model · Qwen3 8B       │                              │
│ │   │ ◉ │   3 queued · 1 running   │                              │
│ │   ╰───╯   ───────────────────    │                              │
│ │ Summarize report-quarterly.pdf … │                              │
│ │           minutes-2026.docx ▓░░  │                              │
│ │           proposal-v2.pdf  queued│                              │
│ │           + 1 more               │                              │
│ └──────────────────────────────────┘                              │
│ ● IO  ● CPU  ● LLM         Throughput · last 30s · ...            │
└────────────────────────────────────────────────────────────────────┘
```

### Container

- Wrap the existing `<svg>` and the new card in a single `<div className="relative">`.
- The bubble is `<div className="absolute bottom-0 left-0 w-[48%] rounded-xl p-4 ...">` with the same purple background tint (`rgba(186,140,255,0.06)`) and 1 px purple border that the current strip uses.
- The bubble's right edge (≈ viewBox x = 288) clears the fan-out stages (Entities/Embed at viewBox x = 390) with ~100 viewBox-units of margin.

### Existing footer strip

The narrow `BACKGROUND · Summarize · N queued` strip and its dotted separator are **removed**. The new bubble carries all of that content. The legend strip (IO/CPU/LLM swatches + throughput footnote) stays exactly where it is.

### Graph headroom

Shift all six stages up by 30 viewBox units to clear vertical space for the bubble:

| Stage    | y before | y after |
|----------|----------|---------|
| Extract  | 160      | 130     |
| OCR      | 160      | 130     |
| Chunk    | 160      | 130     |
| Entities | 100      | 70      |
| Embed    | 220      | 190     |
| Finalize | 160      | 130     |

`VIEW_HEIGHT` stays at 320 — only y values move. The `Ready` badge text at y=135 also shifts to y=105.

### Bubble internal layout

A flex row inside the bubble:

- **Top edge of the bubble:** `BACKGROUND` label (small, purple, uppercase, tracking-wider) at the top-left — matches the placement of the same label on the current strip.
- **Left column** (fixed ~88 px wide): inline `<svg viewBox="0 0 80 80">` rendering the same ring + heartbeat-particle treatment as the main-flow nodes, in LLM purple. Stage label "Summarize" sits directly underneath the ring, just like Extract/OCR/etc. underneath their rings.
- **Right column** (flex-1): vertical stack —
  1. Header row: `Model · <display_name>` on the left, `N queued · M running` on the right.
  2. Thin purple divider.
  3. Item list: up to four `SummarizingRow`-style rows, then a `+N more` row if more items exist. Empty state: list is hidden, header shows just `0 queued · 0 running`.

## Circle behaviour

Mirrors the main-flow stages exactly:

- Outline ring radius scales as `Math.min(26, 14 + Math.sqrt(queued + running) * 2.5)` — identical formula to `nodeRadius()`.
- When `running > 0`, an inner concentric particle appears with `pipeline-glow` filter and the 1.6 s heartbeat animation (radius `0.86×→1.04×`, opacity `0.78→1.0`).
- Outline stroke `1.2 px`, opacity `0.45` when idle and `0.75` when busy.
- Count badge above the ring when `total > 0`: `"M • +N"` (running • queued) or just `M` / `N` when only one is non-zero.

The reusable building block — currently inlined in `PipelineGraph` — gets extracted into a small `<StageNode>` component or a render helper so the summarize card and the main graph share the implementation without duplication.

## Model display

Source of truth: the existing `useLLMStatus()` hook ([frontend/src/hooks/useLLMStatus.ts](../../../frontend/src/hooks/useLLMStatus.ts)) which polls `/api/chat/models/status`.

Today the response is `{state, model_id}` — no display name. The frontend has an id→name map only via the full `/api/chat/models` listing, which is heavier than we need.

**Backend change:** add a `model_name` field to the `llm_status` endpoint. Lookup is a one-line `MODELS.get(model_id).name` against the existing `MODELS` table at [src/harbor_clerk/llm/models.py](../../../src/harbor_clerk/llm/models.py).

```python
# in src/harbor_clerk/api/routes/chat.py, around llm_status()
model_info = get_model(model_id) if model_id else None
model_name = model_info.name if model_info else None
return {"state": ..., "model_id": model_id, "model_name": model_name}
```

`LLMStatus` in `useLLMStatus.ts` gains `model_name: string | null`. All four states render distinctly:

| `state`        | `model_name`     | Render                                |
|----------------|------------------|---------------------------------------|
| `ready`        | `"Qwen3 8B"`     | `Model · Qwen3 8B`                    |
| `loading`      | `"Qwen3 8B"`     | `Model · Qwen3 8B (loading…)` muted   |
| `loading`      | `null` (orphan)  | `Model · <model_id> (loading…)` muted |
| `deactivated`  | `null`           | `Model · not loaded` muted italic     |
| `unknown`      | `null`           | `Model · …` muted                     |

If a stale `model_id` doesn't resolve in `MODELS` (downloaded GGUF that's been removed from the curated list), the endpoint returns `model_id` with `model_name = null` and the card falls back to displaying the raw id. The endpoint guarantees `model_name === null` whenever `model_id === null`; the inverse is not guaranteed.

## Item list

The list reads from `snapshot.summarizing[]` which is already returned by `/api/jobs/snapshot` ([src/harbor_clerk/api/routes/jobs.py:204-240](../../../src/harbor_clerk/api/routes/jobs.py)).

- Sort: `running` items first (descending by `progress_current` so the most-advanced run is on top), then `queued` items in FIFO order (matches the backend's `ORDER BY created_at`).
- Render up to **4 rows**. If `summarizing.length > 4`, render 3 rows + a single `+N more` row.
- Each row is a slim variant of `SummarizingRow` ([frontend/src/components/queue-tray/SummarizingRow.tsx](../../../frontend/src/components/queue-tray/SummarizingRow.tsx)):
  - Filename, `truncate` to fit
  - Inline progress bar (60 px wide, 4 px tall) only when `running`
  - State label on the right: `queued` / `map N/M` / `reduce` / `running`
- Empty state (`summarizing.length === 0`): hide the list and the divider; the bubble shrinks to just the circle column + header row.

A separate render of `SummarizingRow` is **not** required — the existing component is small enough that we can reuse it directly inside the bubble. Style differences (compact spacing inside the bubble vs the tray) are handled with a tailwind override on the parent.

## Empty / loading states

| Condition                                  | Bubble shows                                                          |
|--------------------------------------------|-----------------------------------------------------------------------|
| `summarizing.length === 0`, model `ready`  | Header line only: `0 queued · 0 running`. Circle: outline ring only.  |
| `summarizing.length > 0`, model `ready`    | Full header + sorted list + circle with heartbeat if any are running. |
| `summarizing.length === 0`, model `loading`| Header shows `Model · X (loading…)` muted, list hidden.               |
| `model_id === null` (deactivated)          | Bubble visible but muted. List can still render if jobs exist.        |
| `useQueueSnapshot` error                   | Bubble hidden entirely — the existing error path covers this.         |

## Files touched

| File                                                  | Change                                                                                       |
|-------------------------------------------------------|----------------------------------------------------------------------------------------------|
| `frontend/src/components/stats/PipelineDiagram.tsx`   | Shift `NODE_POSITIONS` up 30 units, drop old strip + separator, add `<SummarizeCard>`.       |
| `frontend/src/hooks/useLLMStatus.ts`                  | Add `model_name: string \| null` to `LLMStatus`.                                             |
| `src/harbor_clerk/api/routes/chat.py`                 | Populate `model_name` in `llm_status()`.                                                     |
| `tests/unit/test_chat_models_status.py` (or similar)  | New backend test asserting `model_name` populated for known model, `null` for unknown.       |
| `frontend/src/components/stats/__tests__/PipelineDiagram.test.tsx` (new) | Render test: empty / queued / running / loading / deactivated states. |

## Risks

1. **The bubble's right edge clipping into the SVG content at narrow viewports.** Mitigation: the bubble uses percentage width relative to its container, so it scales down with the SVG. Worst case at narrow width: the bubble overlaps Embed's left edge — acceptable because at narrow widths the SVG itself becomes cramped.
2. **`model_name` field returned by the status endpoint isn't backwards compatible.** Mitigation: it's additive only. Older frontends ignore unknown fields; newer frontend defaults to `null` when missing.
3. **`useLLMStatus` polls at 1.5–8 s; `useQueueSnapshot` polls every 3 s.** They aren't synchronised. Mitigation: this is fine — both updates are independent and the rendering is reactive.

## Testing

- **Backend:** assert `/api/chat/models/status` returns `model_name` matching `MODELS[id].name` when a known model is active, and `null` when the id is not in `MODELS` or the state is `deactivated`.
- **Frontend render tests** for `PipelineDiagram` covering the empty / running / queued+running / loading / deactivated state matrix above.
- **Manual verification** on the Observatory page with the dev stack running, with at least one document being summarized so the heartbeat + progress bar render live.

## Out of scope (deferred)

- Showing per-doc map-step *duration* (e.g. `map 4/8 · 12s`). Would require carrying step timing in `ingestion_jobs` — too much for this change.
- Click-through on a row to scroll to the document on the Documents page.
- Tooltip on the model name explaining what summarize uses the LLM for.

import type { QueueClass } from '../hooks/useQueueSnapshot'

/**
 * Single source of truth for queue-class colours. Used by:
 *  - PipelineDiagram (Observatory page) — node fills, edge strokes,
 *    particle colours, legend swatches.
 *  - WorkerStrip (drawer Pipeline tab) — chip background / text / ring.
 *  - StageHistogram (drawer Pipeline tab) — bar fill (running stacked
 *    under queued).
 *
 * Colours are picked from the macOS system palette so they read as
 * native and remain distinct from the in-app accent. Alpha variants go
 * through `color-mix(in srgb, ..., transparent)` rather than ad-hoc hex
 * parsing — better for dark/light mode and avoids string-math bugs.
 */
export function classColor(queue: QueueClass | undefined): string {
  if (queue === 'cpu') return '#f5a623' // system amber
  if (queue === 'llm') return '#bf5af2' // system purple
  return '#0a84ff' // accent blue (matches --color-accent on dark)
}

/** Translucent variant for tinted backgrounds, ring colours, etc. */
export function classColorAlpha(queue: QueueClass | undefined, alpha: number): string {
  return `color-mix(in srgb, ${classColor(queue)} ${Math.round(alpha * 100)}%, transparent)`
}

/**
 * Stage → queue-class mapping. Mirrors `STAGE_CONFIG` in
 * `src/harbor_clerk/worker/pipeline.py`. Mapping is stable so duplicating
 * it is safe and saves a backend round-trip in places that already have
 * a stage name (e.g. timing charts on the Observatory page).
 */
export function queueForStage(stage: string): QueueClass {
  if (stage === 'ocr' || stage === 'embed') return 'cpu'
  if (stage === 'summarize') return 'llm'
  return 'io'
}

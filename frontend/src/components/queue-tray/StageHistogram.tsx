import type { QueueSnapshot } from '../../hooks/useQueueSnapshot'
import { stageLabel } from '../../utils/stageLabel'

interface StageHistogramProps {
  snapshot: QueueSnapshot
}

/**
 * Per-stage queue depth, one bar per pipeline stage in pipeline order.
 *
 * Each bar shows running (saturated accent, bottom) stacked under
 * queued (lighter accent, top). The eye reads "running below the
 * waterline, waiting above" — a tall stack on a single stage screams
 * "this is the choke point". The total height of the tallest bar
 * normalises to a fixed pixel ceiling so long queues don't crush the
 * shorter bars into invisibility.
 */
const BAR_PIXEL_HEIGHT = 80
const SHORT_LABELS: Record<string, string> = {
  extract: 'Ext',
  ocr: 'OCR',
  chunk: 'Chk',
  entities: 'Ent',
  embed: 'Emb',
  summarize: 'Sum',
  finalize: 'Fin',
}

export default function StageHistogram({ snapshot }: StageHistogramProps) {
  const totals = snapshot.stage_order.map((stage) => {
    const info = snapshot.by_stage[stage]
    return (info?.queued ?? 0) + (info?.running ?? 0)
  })
  const max = Math.max(1, ...totals)

  return (
    <div>
      <div className="mb-1 flex items-end gap-1.5" style={{ height: BAR_PIXEL_HEIGHT + 16 }}>
        {snapshot.stage_order.map((stage, idx) => {
          const info = snapshot.by_stage[stage]
          const queued = info?.queued ?? 0
          const running = info?.running ?? 0
          const total = totals[idx]
          const barHeight = total > 0 ? Math.max(2, (total / max) * BAR_PIXEL_HEIGHT) : 0
          const runningHeight = total > 0 ? (running / total) * barHeight : 0
          const queuedHeight = barHeight - runningHeight
          const isEmpty = total === 0

          return (
            <div key={stage} className="flex flex-1 flex-col items-center justify-end" style={{ minWidth: 0 }}>
              {/* Numeric label above bar */}
              <div
                className={`mb-0.5 text-[10px] tabular-nums leading-none ${
                  isEmpty ? 'text-(--color-text-secondary)/50' : 'text-(--color-text-primary)'
                }`}
              >
                {total}
              </div>
              {/* Bar (running below, queued above) */}
              <div
                className="w-full overflow-hidden rounded-sm"
                style={{ height: BAR_PIXEL_HEIGHT }}
                title={
                  isEmpty
                    ? `${stageLabel(stage)} — idle`
                    : `${stageLabel(stage)} — ${running} running, ${queued} queued`
                }
              >
                <div className="flex h-full flex-col justify-end">
                  {queuedHeight > 0 && (
                    <div
                      className="w-full bg-(--color-accent)/30"
                      style={{ height: queuedHeight, transition: 'height 400ms ease-out' }}
                    />
                  )}
                  {runningHeight > 0 && (
                    <div
                      className="w-full bg-(--color-accent)"
                      style={{ height: runningHeight, transition: 'height 400ms ease-out' }}
                    />
                  )}
                </div>
              </div>
            </div>
          )
        })}
      </div>
      {/* X-axis labels */}
      <div className="flex gap-1.5">
        {snapshot.stage_order.map((stage) => (
          <div
            key={stage}
            className="flex-1 text-center text-[10px] text-(--color-text-secondary)"
            style={{ minWidth: 0 }}
            title={stageLabel(stage)}
          >
            {SHORT_LABELS[stage] ?? stage}
          </div>
        ))}
      </div>
      {/* Legend */}
      <div className="mt-3 flex items-center gap-3 text-[10px] text-(--color-text-secondary)">
        <span className="inline-flex items-center gap-1">
          <span className="h-2 w-2 rounded-sm bg-(--color-accent)" />
          Running
        </span>
        <span className="inline-flex items-center gap-1">
          <span className="h-2 w-2 rounded-sm bg-(--color-accent)/30" />
          Queued
        </span>
      </div>
    </div>
  )
}

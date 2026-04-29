import type { QueueClass, QueueSnapshot } from '../../hooks/useQueueSnapshot'
import { stageLabel } from '../../utils/stageLabel'
import { classColor, classColorAlpha } from '../../utils/queueColors'

interface WorkerStripProps {
  snapshot: QueueSnapshot
}

const QUEUE_ORDER: readonly QueueClass[] = ['io', 'cpu', 'llm'] as const

/**
 * Per-stage activity chips, grouped by queue class (IO vs CPU vs LLM).
 *
 * We don't have stable worker IDs — workers are inferred from the
 * count of running jobs at each stage. So chips are per-stage, not
 * per-worker: "Embed · 3" means three workers are currently running
 * embed jobs, regardless of which worker pids those are.
 *
 * Idle stages (no running jobs) render dimmer to draw the eye to
 * what's actually happening.
 */
export default function WorkerStrip({ snapshot }: WorkerStripProps) {
  // Group stages by their queue class while preserving pipeline order.
  const groups: Record<QueueClass, string[]> = { io: [], cpu: [], llm: [] }
  for (const stage of snapshot.stage_order) {
    const info = snapshot.by_stage[stage]
    if (!info) continue
    groups[info.queue].push(stage)
  }

  return (
    <div className="space-y-1.5">
      {QUEUE_ORDER.map((queue) => {
        const stages = groups[queue]
        if (stages.length === 0) return null
        const totalRunning = snapshot.queues[queue]?.running ?? 0
        return (
          <div key={queue} className="flex items-center gap-2">
            <span
              className={`shrink-0 w-7 text-[10px] font-semibold uppercase tracking-wider ${
                totalRunning > 0 ? 'text-(--color-text-primary)' : 'text-(--color-text-secondary)'
              }`}
            >
              {queue}
            </span>
            <div className="flex flex-wrap gap-1">
              {stages.map((stage) => {
                const info = snapshot.by_stage[stage]
                const running = info?.running ?? 0
                const idle = running === 0
                // Active chips are coloured by their queue class to
                // match the Observatory pipeline diagram (IO blue,
                // CPU amber, LLM purple). Idle chips stay neutral
                // grey so the eye is drawn to what's running.
                const activeStyle = idle
                  ? undefined
                  : {
                      background: classColorAlpha(info?.queue, 0.15),
                      color: classColor(info?.queue),
                      boxShadow: `inset 0 0 0 1px ${classColorAlpha(info?.queue, 0.3)}`,
                    }
                return (
                  <span
                    key={stage}
                    className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] transition-colors ${
                      idle ? 'bg-(--color-bg-tertiary) text-(--color-text-secondary)/70' : ''
                    }`}
                    style={activeStyle}
                    title={
                      idle
                        ? `${stageLabel(stage)} — idle`
                        : `${stageLabel(stage)} — ${running} worker${running === 1 ? '' : 's'} busy`
                    }
                  >
                    <span
                      className={`h-1.5 w-1.5 rounded-full ${idle ? 'bg-current/40' : ''}`}
                      style={idle ? undefined : { background: classColor(info?.queue) }}
                    />
                    {stageLabel(stage)}
                    {running > 0 && <span className="font-semibold">· {running}</span>}
                  </span>
                )
              })}
            </div>
          </div>
        )
      })}
    </div>
  )
}

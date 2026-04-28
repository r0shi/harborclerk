import type { QueueClass, QueueSnapshot } from '../../hooks/useQueueSnapshot'
import { stageLabel } from '../../utils/stageLabel'

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
                return (
                  <span
                    key={stage}
                    className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] transition-colors ${
                      idle
                        ? 'bg-(--color-bg-tertiary) text-(--color-text-secondary)/70'
                        : 'bg-(--color-accent)/15 text-(--color-accent) ring-1 ring-(--color-accent)/30'
                    }`}
                    title={
                      idle
                        ? `${stageLabel(stage)} — idle`
                        : `${stageLabel(stage)} — ${running} worker${running === 1 ? '' : 's'} busy`
                    }
                  >
                    <span className={`h-1.5 w-1.5 rounded-full ${idle ? 'bg-current/40' : 'bg-(--color-accent)'}`} />
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

import { useQueueSnapshot } from '../../hooks/useQueueSnapshot'
import WorkerStrip from './WorkerStrip'
import StageHistogram from './StageHistogram'

/**
 * "Pipeline" tab inside the drawer — info-dense at-a-glance view of
 * what every queue is doing right now.
 *
 * Top: per-stage activity chips grouped by queue class (IO/CPU).
 * Bottom: per-stage queue-depth histogram (running stacked under queued).
 *
 * The full graphical "panache" view of the pipeline DAG lives on the
 * Observatory page, not here — this tab is the practical
 * status-watcher.
 */
export default function PipelineTab() {
  const { snapshot, error } = useQueueSnapshot()

  if (!snapshot && !error) {
    return <div className="px-4 py-6 text-center text-[12px] text-(--color-text-secondary)">Loading…</div>
  }

  if (error) {
    return <div className="px-4 py-6 text-center text-[12px] text-red-500/80">{error}</div>
  }

  if (!snapshot) return null

  const totalActivity =
    snapshot.queues.io.queued + snapshot.queues.io.running + snapshot.queues.cpu.queued + snapshot.queues.cpu.running

  return (
    <div className="px-4 py-3 space-y-4">
      <div>
        <div className="text-[11px] font-medium uppercase tracking-wider text-(--color-text-secondary) mb-2">
          Workers
        </div>
        <WorkerStrip snapshot={snapshot} />
      </div>

      <div>
        <div className="text-[11px] font-medium uppercase tracking-wider text-(--color-text-secondary) mb-2">
          Queue depth by stage
        </div>
        <StageHistogram snapshot={snapshot} />
      </div>

      {totalActivity === 0 && (
        <div className="text-center text-[12px] text-(--color-text-secondary)">Pipeline is idle</div>
      )}
    </div>
  )
}

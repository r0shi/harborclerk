import { Link } from 'react-router-dom'
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
 * Observatory page, not here — this tab is the practical status-watcher.
 * The footer link deep-links to Observatory's Processing Pipeline tab so
 * users can jump from at-a-glance to the full diagram. The drawer stays
 * open after the navigation so the user can keep watching the queue
 * while inspecting the diagram.
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

      <div className="pt-2 border-t border-(--color-border)">
        <Link
          to="/stats?tab=pipeline"
          className="flex items-center justify-between text-[12px] text-(--color-text-secondary) hover:text-(--color-accent) transition-colors"
        >
          <span>Open full pipeline diagram in Observatory</span>
          <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M13.5 6H5.25A2.25 2.25 0 003 8.25v10.5A2.25 2.25 0 005.25 21h10.5A2.25 2.25 0 0018 18.75V10.5m-10.5 6L21 3m0 0h-5.25M21 3v5.25"
            />
          </svg>
        </Link>
      </div>
    </div>
  )
}

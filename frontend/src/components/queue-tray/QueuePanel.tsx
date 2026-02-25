import { useState } from 'react'
import { stageLabel } from '../../utils/stageLabel'
import type { ActiveJob, HistoryEntry } from '../../hooks/useQueueTray'

interface QueuePanelProps {
  activeJobs: Map<string, ActiveJob>
  history: HistoryEntry[]
  onClose: () => void
}

export default function QueuePanel({ activeJobs, history, onClose }: QueuePanelProps) {
  const [exiting, setExiting] = useState(false)

  const handleClose = () => {
    setExiting(true)
  }

  const handleAnimationEnd = (e: React.AnimationEvent) => {
    if (e.animationName === 'panelSlideDown') {
      setExiting(false)
      onClose()
    }
  }

  const active = Array.from(activeJobs.values())

  return (
    <div
      className={`mb-2 ${exiting ? 'panel-exit' : 'panel-enter'}`}
      onAnimationEnd={handleAnimationEnd}
    >
      <div className="w-80 max-h-[60vh] flex flex-col rounded-2xl bg-[var(--bg-vibrancy)] backdrop-blur-xl shadow-mac-lg ring-1 ring-[var(--color-border)] overflow-hidden">
        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-[var(--color-border)]">
          <span className="text-[13px] font-semibold text-[var(--color-text-primary)]">Queue</span>
          <button
            onClick={handleClose}
            className="rounded-md p-0.5 text-[var(--color-text-secondary)] hover:text-[var(--color-text-primary)] hover:bg-black/[0.04] dark:hover:bg-white/[0.06] transition-colors"
          >
            <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        {/* Body */}
        <div className="overflow-y-auto queue-panel-scroll flex-1">
          {/* Active section */}
          {active.length > 0 && (
            <div className="px-4 py-2">
              <div className="text-[11px] font-medium uppercase tracking-wider text-[var(--color-text-secondary)] mb-2">
                Active
              </div>
              <div className="space-y-2">
                {active.map((job) => (
                  <div key={`${job.version_id}:${job.stage}`} className="flex flex-col gap-1">
                    <div className="flex items-center space-x-2">
                      <div className="h-2 w-2 animate-pulse rounded-full bg-amber-500 shrink-0" />
                      <span className="text-[13px] font-medium text-[var(--color-text-primary)] truncate">
                        {stageLabel(job.stage)}
                        {job.filename && <span className="font-normal text-[var(--color-text-secondary)]"> · {job.filename}</span>}
                      </span>
                    </div>
                    {job.progress_total != null && job.progress_total > 0 && (
                      <div className="ml-4">
                        <div className="h-1.5 rounded-full bg-gray-200 dark:bg-gray-600">
                          <div
                            className="h-1.5 rounded-full bg-blue-500 transition-all"
                            style={{
                              width: `${Math.round(((job.progress_current || 0) / job.progress_total) * 100)}%`,
                            }}
                          />
                        </div>
                        <span className="text-[11px] text-[var(--color-text-secondary)]">
                          {job.progress_current}/{job.progress_total}
                        </span>
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Recent section */}
          {history.length > 0 && (
            <div className="px-4 py-2">
              <div className="text-[11px] font-medium uppercase tracking-wider text-[var(--color-text-secondary)] mb-2">
                Recent
              </div>
              <div className="space-y-1.5">
                {history.map((entry) => (
                  <div key={entry.key} className="flex items-center space-x-2">
                    {entry.status === 'done' ? (
                      <svg className="h-3.5 w-3.5 text-green-500 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
                        <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 12.75l6 6 9-13.5" />
                      </svg>
                    ) : (
                      <div className="h-2 w-2 rounded-full bg-red-500 shrink-0 ml-[3px] mr-[3px]" />
                    )}
                    <span className="text-[13px] text-[var(--color-text-secondary)] truncate">
                      {stageLabel(entry.stage)}
                      {entry.filename && <span> · {entry.filename}</span>}
                    </span>
                    <span className="text-[11px] text-[var(--color-text-secondary)] opacity-60 ml-auto shrink-0">
                      {formatAge(entry.finished_at)}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Empty state */}
          {active.length === 0 && history.length === 0 && (
            <div className="px-4 py-6 text-center text-[13px] text-[var(--color-text-secondary)]">
              No jobs in queue
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

function formatAge(timestamp: number): string {
  const seconds = Math.round((Date.now() - timestamp) / 1000)
  if (seconds < 5) return 'now'
  if (seconds < 60) return `${seconds}s ago`
  return `${Math.round(seconds / 60)}m ago`
}

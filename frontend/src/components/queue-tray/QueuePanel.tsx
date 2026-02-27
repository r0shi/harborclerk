import { useState } from 'react'
import type { DocumentQueueItem, CompletedItem } from '../../hooks/useQueueTray'
import DocumentRow from './DocumentRow'
import CompletedRow from './CompletedRow'

interface QueuePanelProps {
  activeItems: Map<string, DocumentQueueItem>
  completed: CompletedItem[]
  onClose: () => void
}

export default function QueuePanel({ activeItems, completed, onClose }: QueuePanelProps) {
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

  // Sort: running items first, then queued
  const active = Array.from(activeItems.values()).sort((a, b) => {
    if (a.status === 'running' && b.status !== 'running') return -1
    if (a.status !== 'running' && b.status === 'running') return 1
    return b.updated_at - a.updated_at
  })

  const hasActive = active.length > 0
  const hasCompleted = completed.length > 0

  return (
    <div
      className={`mb-2 ${exiting ? 'panel-exit' : 'panel-enter'}`}
      onAnimationEnd={handleAnimationEnd}
    >
      <div className="w-[360px] max-h-[60vh] flex flex-col rounded-2xl bg-[var(--bg-vibrancy)] backdrop-blur-xl shadow-mac-lg ring-1 ring-[var(--color-border)] overflow-hidden">
        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-[var(--color-border)]">
          <span className="text-[13px] font-semibold text-[var(--color-text-primary)]">Processing</span>
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
          {hasActive && (
            <div className="px-4 py-2">
              <div className="text-[11px] font-medium uppercase tracking-wider text-[var(--color-text-secondary)] mb-2">
                Active
              </div>
              <div className="space-y-1">
                {active.map((item) => (
                  <DocumentRow key={item.version_id} item={item} />
                ))}
              </div>
            </div>
          )}

          {/* Divider */}
          {hasActive && hasCompleted && (
            <div className="border-b border-[var(--color-border)]" />
          )}

          {/* Completed section */}
          {hasCompleted && (
            <div className="px-4 py-2">
              <div className="text-[11px] font-medium uppercase tracking-wider text-[var(--color-text-secondary)] mb-2">
                Completed ({completed.length})
              </div>
              <div className="space-y-2">
                {completed.map((item) => (
                  <CompletedRow key={item.version_id} item={item} />
                ))}
              </div>
            </div>
          )}

          {/* Empty state */}
          {!hasActive && !hasCompleted && (
            <div className="px-4 py-6 text-center text-[13px] text-[var(--color-text-secondary)]">
              No items in queue
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

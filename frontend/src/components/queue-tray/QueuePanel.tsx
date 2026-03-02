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

  // Sort active items by enqueue order (oldest first = stable ordering)
  const active = Array.from(activeItems.values()).sort((a, b) => a.enqueued_at - b.enqueued_at)

  // Split completed into done vs errors
  const completedDone = completed.filter((c) => c.status === 'done')
  const completedErrors = completed.filter((c) => c.status === 'error')

  const hasActive = active.length > 0
  const hasCompleted = completedDone.length > 0
  const hasErrors = completedErrors.length > 0

  return (
    <div className={`mb-2 ${exiting ? 'panel-exit' : 'panel-enter'}`} onAnimationEnd={handleAnimationEnd}>
      <div className="w-[360px] max-h-[60vh] flex flex-col rounded-2xl bg-(--bg-vibrancy) backdrop-blur-xl shadow-mac-lg ring-1 ring-(--color-border) overflow-hidden">
        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-(--color-border)">
          <span className="text-[13px] font-semibold text-(--color-text-primary)">Processing</span>
          <button
            onClick={handleClose}
            className="rounded-md p-0.5 text-(--color-text-secondary) hover:text-(--color-text-primary) hover:bg-black/4 dark:hover:bg-white/6 transition-colors"
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
              <div className="text-[11px] font-medium uppercase tracking-wider text-(--color-text-secondary) mb-2">
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
          {hasActive && hasCompleted && <div className="border-b border-(--color-border)" />}

          {/* Completed section (done items only) */}
          {hasCompleted && (
            <div className="px-4 py-2">
              <div className="text-[11px] font-medium uppercase tracking-wider text-(--color-text-secondary) mb-2">
                Completed ({completedDone.length})
              </div>
              <div className="space-y-2">
                {completedDone.map((item) => (
                  <CompletedRow key={item.version_id} item={item} />
                ))}
              </div>
            </div>
          )}

          {/* Divider */}
          {(hasActive || hasCompleted) && hasErrors && <div className="border-b border-(--color-border)" />}

          {/* Errors section */}
          {hasErrors && (
            <div className="px-4 py-2">
              <div className="text-[11px] font-medium uppercase tracking-wider text-red-500/80 mb-2">
                Errors ({completedErrors.length})
              </div>
              <div className="space-y-2">
                {completedErrors.map((item) => (
                  <CompletedRow key={item.version_id} item={item} />
                ))}
              </div>
            </div>
          )}

          {/* Empty state */}
          {!hasActive && !hasCompleted && !hasErrors && (
            <div className="px-4 py-6 text-center text-[13px] text-(--color-text-secondary)">No items in queue</div>
          )}
        </div>
      </div>
    </div>
  )
}

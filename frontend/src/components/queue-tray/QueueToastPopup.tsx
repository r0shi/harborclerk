import { useState } from 'react'
import { stageLabel } from '../../utils/stageLabel'
import type { DocumentQueueItem } from '../../hooks/useQueueTray'

interface QueueToastPopupProps {
  items: DocumentQueueItem[]
  onDismiss: () => void
}

export default function QueueToastPopup({ items, onDismiss }: QueueToastPopupProps) {
  const [exiting, setExiting] = useState(false)

  if (items.length === 0 && !exiting) return null

  // Show the latest running item
  const running = items.filter((i) => i.status === 'running')
  const latest = running.length > 0 ? running[running.length - 1] : items[items.length - 1]
  if (!latest && !exiting) return null

  const handleDismiss = () => {
    setExiting(true)
  }

  const handleAnimationEnd = (e: React.AnimationEvent) => {
    if (e.animationName === 'toastSlideDown') {
      setExiting(false)
      onDismiss()
    }
  }

  return (
    <div
      className={`mb-2 ${exiting ? 'toast-exit' : 'toast-enter'}`}
      onAnimationEnd={handleAnimationEnd}
      onClick={handleDismiss}
    >
      <div className="rounded-xl bg-[var(--bg-vibrancy)] backdrop-blur-xl px-4 py-3 shadow-mac-lg ring-1 ring-[var(--color-border)] cursor-pointer">
        {latest && (
          <>
            <div className="flex items-center space-x-2">
              <div className="h-2 w-2 animate-pulse rounded-full bg-amber-500" />
              <span className="text-sm font-medium text-[var(--color-text-primary)] truncate max-w-[200px]">
                {latest.filename}
              </span>
              {items.length > 1 && (
                <span className="text-xs text-[var(--color-text-secondary)]">+{items.length - 1} more</span>
              )}
            </div>
            <div className="text-xs text-[var(--color-text-secondary)] mt-0.5 ml-4">
              {latest.status === 'queued' ? 'Queued' : stageLabel(latest.current_stage)}
            </div>
          </>
        )}
      </div>
    </div>
  )
}

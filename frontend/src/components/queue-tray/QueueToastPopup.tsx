import { useState } from 'react'
import { stageLabel } from '../../utils/stageLabel'
import type { ActiveJob } from '../../hooks/useQueueTray'

interface QueueToastPopupProps {
  jobs: ActiveJob[]
  onDismiss: () => void
}

export default function QueueToastPopup({ jobs, onDismiss }: QueueToastPopupProps) {
  const [exiting, setExiting] = useState(false)

  if (jobs.length === 0 && !exiting) return null

  // Show latest job in toast
  const latest = jobs[jobs.length - 1]
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
              <span className="text-sm font-medium text-[var(--color-text-primary)]">
                {stageLabel(latest.stage)}
              </span>
              {jobs.length > 1 && (
                <span className="text-xs text-[var(--color-text-secondary)]">
                  +{jobs.length - 1} more
                </span>
              )}
            </div>
            {latest.progress_total != null && latest.progress_total > 0 && (
              <div className="mt-1.5">
                <div className="h-1.5 w-48 rounded-full bg-gray-200 dark:bg-gray-600">
                  <div
                    className="h-1.5 rounded-full bg-blue-500 transition-all"
                    style={{
                      width: `${Math.round(((latest.progress_current || 0) / latest.progress_total) * 100)}%`,
                    }}
                  />
                </div>
                <span className="text-xs text-[var(--color-text-secondary)]">
                  {latest.progress_current}/{latest.progress_total}
                </span>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}

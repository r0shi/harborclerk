import { useCallback, useEffect, useRef } from 'react'
import { useQueueTray } from '../../hooks/useQueueTray'
import QueuePill from './QueuePill'
import QueueToastPopup from './QueueToastPopup'
import QueuePanel from './QueuePanel'

export default function QueueTray() {
  const { trayState, activeJobs, history, toggleExpanded, collapse } = useQueueTray()
  const containerRef = useRef<HTMLDivElement>(null)

  const activeCount = activeJobs.size
  const historyCount = history.length

  // Click outside to collapse
  const handleClickOutside = useCallback(
    (e: MouseEvent) => {
      if (
        trayState === 'expanded' &&
        containerRef.current &&
        !containerRef.current.contains(e.target as Node)
      ) {
        collapse()
      }
    },
    [trayState, collapse],
  )

  // Escape to collapse
  const handleKeyDown = useCallback(
    (e: KeyboardEvent) => {
      if (e.key === 'Escape' && trayState === 'expanded') {
        collapse()
      }
    },
    [trayState, collapse],
  )

  useEffect(() => {
    if (trayState === 'expanded') {
      document.addEventListener('mousedown', handleClickOutside)
      document.addEventListener('keydown', handleKeyDown)
    }
    return () => {
      document.removeEventListener('mousedown', handleClickOutside)
      document.removeEventListener('keydown', handleKeyDown)
    }
  }, [trayState, handleClickOutside, handleKeyDown])

  // Don't render anything if nothing to show
  if (activeCount === 0 && historyCount === 0 && trayState === 'collapsed') {
    return null
  }

  return (
    <div ref={containerRef} className="fixed bottom-4 left-4 z-50 flex flex-col items-start">
      {/* Panel (expanded state) */}
      {trayState === 'expanded' && (
        <QueuePanel
          activeJobs={activeJobs}
          history={history}
          onClose={collapse}
        />
      )}

      {/* Toast (toasting state, hidden when expanded) */}
      {trayState === 'toasting' && (
        <QueueToastPopup
          jobs={Array.from(activeJobs.values())}
          onDismiss={collapse}
        />
      )}

      {/* Pill (always visible when there's content) */}
      <QueuePill
        activeCount={activeCount}
        historyCount={historyCount}
        isPulsing={trayState === 'toasting'}
        onClick={toggleExpanded}
      />
    </div>
  )
}

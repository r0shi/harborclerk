import { useCallback, useEffect } from 'react'
import { useQueueTray } from '../../hooks/useQueueTray'
import QueuePill from './QueuePill'
import QueueToastPopup from './QueueToastPopup'
import QueuePanel from './QueuePanel'

// Drawer behaviour: persists until the user dismisses it (chevron-down or
// Esc). Click-outside no longer collapses — the drawer is meant to stay
// open while the user works elsewhere in the app, so they can monitor
// the queue while uploading / browsing. Route changes do NOT auto-dismiss
// either. Structurally generic enough that other utility content could
// live here later; if a second tab/use lands, rename QueueTray /
// QueuePanel to Drawer / DrawerPanel and update this note.
export default function QueueTray() {
  const { trayState, activeItems, completed, toggleExpanded, collapse } = useQueueTray()

  const activeCount = activeItems.size
  const completedCount = completed.length

  // Escape collapses the drawer — keep this as a discoverable shortcut.
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
      document.addEventListener('keydown', handleKeyDown)
    }
    return () => {
      document.removeEventListener('keydown', handleKeyDown)
    }
  }, [trayState, handleKeyDown])

  // The pill is always rendered — even when the queue is empty — so the
  // user has a persistent affordance to open the drawer and see the
  // Pipeline tab or recently completed items. The pill itself swaps to a
  // muted "Queue idle" treatment in that case.

  return (
    <div className="fixed bottom-4 left-4 z-50 flex flex-col items-start">
      {/* Panel (expanded state) */}
      {trayState === 'expanded' && <QueuePanel activeItems={activeItems} completed={completed} onClose={collapse} />}

      {/* Toast (toasting state, hidden when expanded) */}
      {trayState === 'toasting' && <QueueToastPopup items={Array.from(activeItems.values())} onDismiss={collapse} />}

      {/* Pill (always visible when there's content) */}
      <QueuePill
        activeCount={activeCount}
        completedCount={completedCount}
        isPulsing={trayState === 'toasting'}
        onClick={toggleExpanded}
      />
    </div>
  )
}

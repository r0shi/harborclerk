interface QueuePillProps {
  activeCount: number
  completedCount: number
  summarizingCount: number
  isPulsing: boolean
  onClick: () => void
}

// Four rest states, each with its own visual treatment, in priority order:
//
// 1. Active       — amber pulse + "{N} processing"           (work in flight)
// 2. Summarizing  — purple dot + "Summarizing {N}" + glow   (only background summaries pending)
// 3. Recent       — list icon + "{N}"                        (only completed items)
// 4. Idle         — muted dot + "Queue idle"                 (nothing pending or recent)
//
// The idle state is intentionally quieter — no pulse, smaller dot, secondary
// text — so it reads as "ambient indicator" rather than "look at me". Users
// can still click to open the drawer for the Pipeline tab or to confirm
// nothing is queued.
export default function QueuePill({
  activeCount,
  completedCount,
  summarizingCount,
  isPulsing,
  onClick,
}: QueuePillProps) {
  const idle = activeCount === 0 && completedCount === 0 && summarizingCount === 0
  const summarizingOnly = activeCount === 0 && summarizingCount > 0

  return (
    <button
      onClick={onClick}
      aria-label={
        idle
          ? 'Processing queue is idle — open drawer'
          : summarizingOnly
            ? `Summarizing ${summarizingCount} documents in background — open drawer`
            : 'Open processing queue drawer'
      }
      className={`
        flex items-center space-x-1.5 rounded-full px-3 py-1.5
        bg-(--bg-vibrancy) backdrop-blur-xl shadow-mac-lg
        ring-1 ring-(--color-border)
        text-[13px] font-medium
        transition-all hover:shadow-mac active:scale-95
        ${idle ? 'text-(--color-text-secondary) opacity-80 hover:opacity-100' : 'text-(--color-text-primary)'}
        ${isPulsing ? 'pill-pulse' : ''}
        ${summarizingOnly ? 'shadow-[0_0_14px_rgba(186,140,255,0.18)]' : ''}
      `}
    >
      {activeCount > 0 && (
        <>
          <span className="relative flex h-2 w-2">
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-amber-400 opacity-75" />
            <span className="relative inline-flex h-2 w-2 rounded-full bg-amber-500" />
          </span>
          <span>{activeCount} processing</span>
        </>
      )}
      {activeCount === 0 && summarizingCount > 0 && (
        <>
          <span className="relative flex h-2 w-2 rounded-full animate-pulse" style={{ background: '#c4a4ff' }} />
          <span style={{ color: '#c4a4ff' }}>Summarizing {summarizingCount}</span>
        </>
      )}
      {activeCount === 0 && summarizingCount === 0 && completedCount > 0 && (
        <>
          <svg
            className="h-3.5 w-3.5 text-(--color-text-secondary)"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            strokeWidth={1.5}
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M3.75 12h16.5m-16.5 3.75h16.5M3.75 19.5h16.5M5.625 4.5h12.75a1.875 1.875 0 010 3.75H5.625a1.875 1.875 0 010-3.75z"
            />
          </svg>
          <span className="text-(--color-text-secondary)">{completedCount}</span>
        </>
      )}
      {idle && (
        <>
          <span className="inline-block h-1.5 w-1.5 rounded-full bg-(--color-text-secondary)/50" />
          <span>Queue idle</span>
        </>
      )}
    </button>
  )
}

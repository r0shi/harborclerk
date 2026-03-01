interface QueuePillProps {
  activeCount: number
  completedCount: number
  isPulsing: boolean
  onClick: () => void
}

export default function QueuePill({ activeCount, completedCount, isPulsing, onClick }: QueuePillProps) {
  if (activeCount === 0 && completedCount === 0) return null

  return (
    <button
      onClick={onClick}
      className={`
        flex items-center space-x-1.5 rounded-full px-3 py-1.5
        bg-(--bg-vibrancy) backdrop-blur-xl shadow-mac-lg
        ring-1 ring-(--color-border)
        text-[13px] font-medium text-(--color-text-primary)
        transition-all hover:shadow-mac active:scale-95
        ${isPulsing ? 'pill-pulse' : ''}
      `}
    >
      {activeCount > 0 ? (
        <>
          <span className="relative flex h-2 w-2">
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-amber-400 opacity-75" />
            <span className="relative inline-flex h-2 w-2 rounded-full bg-amber-500" />
          </span>
          <span>{activeCount} processing</span>
        </>
      ) : (
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
    </button>
  )
}

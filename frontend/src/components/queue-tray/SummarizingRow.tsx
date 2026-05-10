// frontend/src/components/queue-tray/SummarizingRow.tsx

export interface SummarizingItem {
  doc_id: string
  filename: string
  status: 'queued' | 'running'
  progress_current: number
  progress_total: number
}

interface SummarizingRowProps {
  item: SummarizingItem
}

function progressLabel(item: SummarizingItem): string {
  if (item.status === 'queued') return 'queued'
  if (item.progress_total <= 0) return 'running'
  // Long-tier: show map step number. progress_total = map_count + 1.
  // progress_current ∈ [0, map_count] during map; == map_count during reduce.
  const mapCount = item.progress_total - 1
  if (item.progress_current >= mapCount) return 'reduce'
  return `map ${item.progress_current + 1}/${mapCount}`
}

/**
 * One row of the queue tray's Summarizing section. Shows the doc
 * filename, a single-track purple progress bar (no visible segments),
 * and a state label (queued / map N/M / reduce / running).
 *
 * Bar mechanics: width = progress_current / progress_total. Discrete
 * jumps as the worker updates the IngestionJob row mid-flight.
 */
export default function SummarizingRow({ item }: SummarizingRowProps) {
  const fraction = item.progress_total > 0 ? Math.min(1, item.progress_current / item.progress_total) : 0
  const isRunning = item.status === 'running'
  const label = progressLabel(item)

  return (
    <div className="flex items-center gap-2.5 py-1.5 text-[11px]">
      <span className="flex-1 truncate">{item.filename}</span>
      <span
        className="inline-block overflow-hidden rounded-sm align-middle"
        style={{
          width: 60,
          height: 4,
          background: 'rgba(186,140,255,0.16)',
          flex: '0 0 auto',
        }}
      >
        <span
          className={`block h-full rounded-sm transition-[width] duration-500 ease-out ${
            isRunning ? 'animate-pulse' : ''
          }`}
          style={{ width: `${fraction * 100}%`, background: '#c4a4ff' }}
        />
      </span>
      <span
        className="text-[10px] tabular-nums text-right"
        style={{ color: '#c4a4ff', minWidth: 60, flex: '0 0 auto' }}
      >
        {label}
      </span>
    </div>
  )
}

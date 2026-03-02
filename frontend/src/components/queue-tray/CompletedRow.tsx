import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { stageLabel } from '../../utils/stageLabel'
import type { CompletedItem } from '../../hooks/useQueueTray'

interface CompletedRowProps {
  item: CompletedItem
}

export default function CompletedRow({ item }: CompletedRowProps) {
  const [, tick] = useState(0)
  useEffect(() => {
    const id = setInterval(() => tick((n) => n + 1), 30_000)
    return () => clearInterval(id)
  }, [])

  const isError = item.status === 'error'

  return (
    <div className={`flex items-start gap-2 ${isError ? 'border-l-2 border-red-500 pl-2' : ''}`}>
      {/* Status icon */}
      {isError ? (
        <svg
          className="h-3.5 w-3.5 text-red-500 shrink-0 mt-0.5"
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
          strokeWidth={2.5}
        >
          <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
        </svg>
      ) : (
        <svg
          className="h-3.5 w-3.5 text-green-500 shrink-0 mt-0.5"
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
          strokeWidth={2.5}
        >
          <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 12.75l6 6 9-13.5" />
        </svg>
      )}

      {/* Middle: filename + stats */}
      <div className="flex-1 min-w-0">
        <div className="text-sm font-medium text-(--color-text-primary) truncate">{item.filename}</div>
        <div className="text-xs text-(--color-text-secondary)">
          {isError ? (
            <span className="text-red-500">
              Error in {item.error_stage ? stageLabel(item.error_stage) : 'unknown stage'}
            </span>
          ) : (
            <>
              {item.page_count != null && item.chunk_count != null
                ? `${item.page_count} pages \u00b7 ${item.chunk_count} chunks`
                : item.page_count != null
                  ? `${item.page_count} pages`
                  : item.chunk_count != null
                    ? `${item.chunk_count} chunks`
                    : null}
            </>
          )}
        </div>
      </div>

      {/* Right: time ago + arrow link */}
      <div className="flex items-center gap-1.5 shrink-0">
        <span className="text-xs text-(--color-text-secondary) opacity-60">{formatAge(item.finished_at)}</span>
        {item.doc_id && (
          <Link
            to={`/docs/${item.doc_id}`}
            className="text-(--color-text-secondary) hover:translate-x-1 hover:text-(--color-accent) transition-transform"
          >
            <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M13.5 4.5L21 12m0 0l-7.5 7.5M21 12H3" />
            </svg>
          </Link>
        )}
      </div>
    </div>
  )
}

function formatAge(timestamp: number): string {
  const seconds = Math.round((Date.now() - timestamp) / 1000)
  if (seconds < 5) return 'now'
  if (seconds < 60) return `${seconds}s ago`
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.round(minutes / 60)
  return `${hours}h ago`
}

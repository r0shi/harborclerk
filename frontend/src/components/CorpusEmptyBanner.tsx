import { Link } from 'react-router-dom'
import type { CorpusBannerState } from '../hooks/useCorpusBannerState'

interface Props {
  state: CorpusBannerState
}

/**
 * Floating pill banner pinned at the top of the content area. Persistent
 * (not user-dismissible); auto-disappears when its state transitions to
 * null. Suppression by route is handled by the caller (Layout.tsx).
 *
 * Copy varies with the empty-corpus state:
 *   - 'no-folders'   → user has configured zero folders
 *   - 'no-documents' → folders exist but nothing has been ingested
 *
 * Visual treatment matches LLMStatusBanner's subtle rounded-pill aesthetic
 * but pinned at the top of the content area rather than the bottom-right.
 */
export default function CorpusEmptyBanner({ state }: Props) {
  if (state === null) return null

  const message =
    state === 'no-folders'
      ? 'No watched folders yet — add one to start indexing documents.'
      : 'Your watched folders are empty — drop documents into them to begin indexing.'
  const linkLabel = state === 'no-folders' ? 'Go to Folders' : 'View folders'

  return (
    <div className="mt-2 flex justify-center px-4">
      <div
        role="status"
        className="flex items-center gap-2 rounded-full bg-amber-50 dark:bg-amber-900/20 px-4 py-1.5 text-xs text-amber-800 dark:text-amber-200 shadow-sm ring-1 ring-amber-200/60 dark:ring-amber-700/40"
      >
        <span>{message}</span>
        <Link to="/folders" className="font-medium text-amber-700 dark:text-amber-300 underline hover:no-underline">
          {linkLabel}
        </Link>
      </div>
    </div>
  )
}

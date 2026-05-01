import { useEffect, useState } from 'react'
import { get } from '../api'
import { useAuth } from '../auth'

export type CorpusBannerState = 'no-folders' | 'no-documents' | null

const POLL_INTERVAL_MS = 30_000

interface FolderRow {
  folder_id: string
}
interface DocsCount {
  total: number
}

/**
 * Polls /api/watch/folders and /api/docs?limit=0 every 30 s and reduces
 * the result to a banner state:
 *
 *   - folder_count === 0                          → 'no-folders'
 *   - folder_count > 0  AND doc_count === 0       → 'no-documents'
 *   - folder_count > 0  AND doc_count > 0         → null (no banner)
 *
 * Also returns null while the auth context is still resolving (no token
 * yet) so the banner doesn't flash before login completes.
 */
export function useCorpusBannerState(): CorpusBannerState {
  const { token } = useAuth()
  const [state, setState] = useState<CorpusBannerState>(null)

  useEffect(() => {
    if (!token) return

    let cancelled = false

    async function poll() {
      try {
        const [folders, docs] = await Promise.all([
          get<FolderRow[]>('/api/watch/folders'),
          get<DocsCount>('/api/docs', { limit: 0 }),
        ])
        if (cancelled) return
        if (folders.length === 0) setState('no-folders')
        else if (docs.total === 0) setState('no-documents')
        else setState(null)
      } catch {
        // Network blip — keep prior state, next tick will retry.
      }
    }

    poll()
    const id = setInterval(poll, POLL_INTERVAL_MS)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [token])

  return state
}

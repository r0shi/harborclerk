import { useEffect, useState } from 'react'
import { Navigate } from 'react-router'
import { get } from '../api'
import { useAuth } from '../auth'

interface FolderRow {
  folder_id: string
}

interface DocsCount {
  total: number
}

export default function HomePage() {
  const { token } = useAuth()
  const [target, setTarget] = useState<'/folders' | '/search' | null>(null)

  useEffect(() => {
    if (!token) return

    let cancelled = false

    async function chooseTarget() {
      try {
        const [folders, docs] = await Promise.all([
          get<FolderRow[]>('/api/watch/folders'),
          get<DocsCount>('/api/docs', { limit: 0 }),
        ])
        if (cancelled) return
        setTarget(folders.length === 0 || docs.total === 0 ? '/folders' : '/search')
      } catch {
        if (!cancelled) setTarget('/search')
      }
    }

    chooseTarget()
    return () => {
      cancelled = true
    }
  }, [token])

  if (target) return <Navigate to={target} replace />
  return <div className="text-sm text-(--color-text-secondary)">Loading...</div>
}

import { useEffect, useState } from 'react'
import { useAuth } from '../auth'
import type { SummarizingItem } from '../components/queue-tray/SummarizingRow'

export type QueueClass = 'io' | 'cpu' | 'llm'

export interface StageSnapshot {
  queued: number
  running: number
  queue: QueueClass
  /** Jobs that completed at this stage within `throughput_window_seconds`. */
  recent_completed: number
}

export interface QueueSnapshot {
  by_stage: Record<string, StageSnapshot>
  queues: Record<QueueClass, { queued: number; running: number }>
  stage_order: string[]
  /** Window over which `recent_completed` is computed. */
  throughput_window_seconds: number
  /**
   * In-flight summarize jobs (queued + running) with map-reduce progress.
   * Drives the queue tray's Summarizing section + the QueuePill backlog
   * count. Always present in the response (may be empty array).
   */
  summarizing?: SummarizingItem[]
}

const POLL_INTERVAL_MS = 3000

/**
 * Polls /api/jobs/snapshot for per-stage queued/running counts.
 *
 * Polling (rather than SSE) is intentional: the snapshot is one indexed
 * COUNT GROUP BY per call, the frontend only needs a value every few
 * seconds for the histogram, and a separate live channel just for stage
 * counts isn't worth the operational complexity. The per-document SSE
 * stream from `useJobEvents` continues to drive the per-document state.
 *
 * Polling only runs while the hook is mounted, so this is dormant when
 * the Pipeline tab isn't on screen.
 */
export function useQueueSnapshot() {
  const { token } = useAuth()
  const [snapshot, setSnapshot] = useState<QueueSnapshot | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!token) return
    let cancelled = false

    async function fetchSnapshot() {
      try {
        const res = await fetch('/api/jobs/snapshot', {
          headers: { Authorization: `Bearer ${token}` },
        })
        if (!res.ok || cancelled) return
        const data: QueueSnapshot = await res.json()
        if (!cancelled) {
          setSnapshot(data)
          setError(null)
        }
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Failed to load snapshot')
      }
    }

    fetchSnapshot()
    const id = setInterval(fetchSnapshot, POLL_INTERVAL_MS)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [token])

  return { snapshot, error }
}

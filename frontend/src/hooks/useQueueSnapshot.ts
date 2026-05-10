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

// Module-level shared state. All consumers see the same snapshot
// without the browser polling /api/jobs/snapshot more than once
// per POLL_INTERVAL_MS, regardless of how many components mount
// the hook simultaneously (today: useQueueTray always-on, PipelineTab
// when the drawer is open, PipelineDiagram on the Observatory page).
type Subscriber = (snap: QueueSnapshot | null, err: string | null) => void

let _snapshot: QueueSnapshot | null = null
let _error: string | null = null
const _subscribers = new Set<Subscriber>()
let _intervalId: ReturnType<typeof setInterval> | null = null
let _currentToken: string | null = null

async function pollOnce(token: string) {
  try {
    const res = await fetch('/api/jobs/snapshot', {
      headers: { Authorization: `Bearer ${token}` },
    })
    if (!res.ok) return
    const data = (await res.json()) as QueueSnapshot
    _snapshot = data
    _error = null
    _subscribers.forEach((cb) => cb(_snapshot, _error))
  } catch (e) {
    _error = e instanceof Error ? e.message : 'Failed to load snapshot'
    _subscribers.forEach((cb) => cb(_snapshot, _error))
  }
}

function startPollingIfNeeded(token: string) {
  if (_intervalId !== null && _currentToken === token) return
  // Token changed (login/logout/refresh) — reset.
  if (_intervalId !== null) {
    clearInterval(_intervalId)
    _intervalId = null
  }
  _currentToken = token
  pollOnce(token)
  _intervalId = setInterval(() => pollOnce(token), POLL_INTERVAL_MS)
}

function stopPollingIfIdle() {
  if (_subscribers.size === 0 && _intervalId !== null) {
    clearInterval(_intervalId)
    _intervalId = null
    _currentToken = null
  }
}

/**
 * Polls /api/jobs/snapshot for per-stage queued/running counts.
 *
 * Polling (rather than SSE) is intentional: the snapshot is one indexed
 * COUNT GROUP BY per call, the frontend only needs a value every few
 * seconds for the histogram, and a separate live channel just for stage
 * counts isn't worth the operational complexity. The per-document SSE
 * stream from `useJobEvents` continues to drive the per-document state.
 *
 * Implementation note: the underlying poll is shared across all
 * subscribers via module-level state, so mounting this hook in multiple
 * components (queue tray, Pipeline tab, Observatory pipeline diagram)
 * does not multiply the request rate. The interval starts with the
 * first subscriber and stops when the last unmounts.
 */
export function useQueueSnapshot() {
  const { token } = useAuth()
  const [snapshot, setSnapshot] = useState<QueueSnapshot | null>(_snapshot)
  const [error, setError] = useState<string | null>(_error)

  useEffect(() => {
    if (!token) return
    const cb: Subscriber = (snap, err) => {
      setSnapshot(snap)
      setError(err)
    }
    _subscribers.add(cb)
    // useState(_snapshot) above already hydrated from the module cache,
    // so a fresh consumer sees the latest data immediately. The poller
    // pushes future updates through `cb`.
    startPollingIfNeeded(token)
    return () => {
      _subscribers.delete(cb)
      stopPollingIfIdle()
    }
  }, [token])

  return { snapshot, error }
}

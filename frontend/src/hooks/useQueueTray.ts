import { useCallback, useEffect, useRef, useState } from 'react'
import { useAuth } from '../auth'
import { useJobEvents, type JobEvent } from './useJobEvents'
import { useQueueSnapshot } from './useQueueSnapshot'
import type { SummarizingItem } from '../components/queue-tray/SummarizingRow'

// Re-exported so the QueueTray, QueuePanel, and QueuePill consumers can
// import everything they need from one place.
export type { SummarizingItem }

export type TrayState = 'collapsed' | 'toasting' | 'expanded'

export interface StageState {
  status: 'queued' | 'running' | 'done' | 'error' | 'skipped'
  progress?: number
  total?: number
}

export interface DocumentQueueItem {
  doc_id: string
  filename: string
  stages: Map<string, StageState>
  current_stage: string
  overall_progress: number // 0-100
  status: 'running' | 'queued' | 'error'
  enqueued_at: number
  updated_at: number
}

export interface CompletedItem {
  doc_id: string
  filename: string
  status: 'done' | 'error'
  error_stage?: string
  page_count?: number
  chunk_count?: number
  finished_at: number
}

const COMPLETED_TTL = 3_600_000 // 60 minutes
const ERROR_TTL = 10_800_000 // 3 hours
const TOAST_DURATION = 4_000 // 4s
const TOAST_DEBOUNCE = 500 // ms
export const PIPELINE_STAGES = ['extract', 'ocr', 'chunk', 'entities', 'embed', 'finalize']

function computeOverallProgress(stages: Map<string, StageState>): number {
  // Filter out skipped stages
  const active = PIPELINE_STAGES.filter((s) => {
    const state = stages.get(s)
    return !state || state.status !== 'skipped'
  })
  const total = active.length || 1
  let completed = 0
  for (const s of active) {
    const state = stages.get(s)
    if (!state || state.status === 'queued') break
    if (state.status === 'done') {
      completed += 1
      continue
    }
    if (state.status === 'running' && state.total && state.total > 0) {
      completed += (state.progress || 0) / state.total
    }
    break // running stage is the current one
  }
  return Math.min(100, Math.round((completed / total) * 100))
}

function computeCurrentStage(stages: Map<string, StageState>): string {
  for (const s of PIPELINE_STAGES) {
    const state = stages.get(s)
    if (!state || state.status === 'queued') return s
    if (state.status === 'running' || state.status === 'error') return s
  }
  return 'finalize'
}

function computeItemStatus(stages: Map<string, StageState>): 'running' | 'queued' | 'error' {
  let hasRunning = false
  for (const s of PIPELINE_STAGES) {
    const state = stages.get(s)
    if (state?.status === 'error') return 'error'
    if (state?.status === 'running') hasRunning = true
  }
  return hasRunning ? 'running' : 'queued'
}

export function useQueueTray() {
  const { token } = useAuth()
  const [trayState, setTrayState] = useState<TrayState>('collapsed')
  const [activeItems, setActiveItems] = useState<Map<string, DocumentQueueItem>>(new Map())
  const [completed, setCompleted] = useState<CompletedItem[]>([])

  // Piggyback on the snapshot poll already used by the Pipeline tab. The
  // backend includes a `summarizing` array (see GET /api/jobs/snapshot)
  // with per-doc map-reduce progress for in-flight summarize jobs. No
  // extra request — useQueueSnapshot's interval is the canonical source.
  // Derived (not stored in state) so we don't trip
  // react-hooks/set-state-in-effect by mirroring server data.
  const { snapshot } = useQueueSnapshot()
  const summarizing: SummarizingItem[] = snapshot?.summarizing ?? []

  const trayStateRef = useRef(trayState)
  const activeItemsRef = useRef(activeItems)
  useEffect(() => {
    trayStateRef.current = trayState
    activeItemsRef.current = activeItems
  })

  const toastTimerRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  const lastToastRef = useRef(0)
  const purgeTimersRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map())

  // Schedule purge of a completed item after TTL (different for done vs error)
  const schedulePurge = useCallback((docId: string, ttl: number) => {
    const existing = purgeTimersRef.current.get(docId)
    if (existing) clearTimeout(existing)

    const timer = setTimeout(() => {
      setCompleted((prev) => prev.filter((c) => c.doc_id !== docId))
      purgeTimersRef.current.delete(docId)
    }, ttl)
    purgeTimersRef.current.set(docId, timer)
  }, [])

  // Trigger toast (with debouncing)
  const triggerToast = useCallback(() => {
    const now = Date.now()
    if (now - lastToastRef.current < TOAST_DEBOUNCE) return
    lastToastRef.current = now

    if (trayStateRef.current === 'expanded') return

    setTrayState('toasting')
    if (toastTimerRef.current) clearTimeout(toastTimerRef.current)
    toastTimerRef.current = setTimeout(() => {
      if (trayStateRef.current === 'toasting') {
        setTrayState('collapsed')
      }
    }, TOAST_DURATION)
  }, [])

  const onEvent = useCallback(
    (event: JobEvent) => {
      const did = event.doc_id
      const stage = event.stage
      const status = event.status as StageState['status']

      // Handle finalize done -> move to completed
      if (stage === 'finalize' && status === 'done') {
        const existing = activeItemsRef.current.get(did)
        const filename = event.filename || existing?.filename || did

        const entry: CompletedItem = {
          doc_id: did,
          filename,
          status: 'done',
          page_count: event.page_count,
          chunk_count: event.chunk_count,
          finished_at: Date.now(),
        }

        setActiveItems((prev) => {
          const next = new Map(prev)
          next.delete(did)
          return next
        })
        setCompleted((prev) => {
          const filtered = prev.filter((c) => c.doc_id !== did)
          return [entry, ...filtered]
        })
        schedulePurge(did, COMPLETED_TTL)
        triggerToast()
        return
      }

      // Handle error -> move to completed
      if (status === 'error') {
        const existing = activeItemsRef.current.get(did)
        const filename = event.filename || existing?.filename || did

        const entry: CompletedItem = {
          doc_id: did,
          filename,
          status: 'error',
          error_stage: stage,
          finished_at: Date.now(),
        }

        setActiveItems((prev) => {
          const next = new Map(prev)
          next.delete(did)
          return next
        })
        setCompleted((prev) => {
          const filtered = prev.filter((c) => c.doc_id !== did)
          return [entry, ...filtered]
        })
        schedulePurge(did, ERROR_TTL)
        triggerToast()
        return
      }

      // If this doc is in the completed list, remove it (re-queued)
      setCompleted((prev) => {
        if (!prev.some((c) => c.doc_id === did)) return prev
        const timer = purgeTimersRef.current.get(did)
        if (timer) {
          clearTimeout(timer)
          purgeTimersRef.current.delete(did)
        }
        return prev.filter((c) => c.doc_id !== did)
      })

      // Active event: update or create DocumentQueueItem
      setActiveItems((prev) => {
        const next = new Map(prev)
        const existing = next.get(did)
        const stages = new Map(existing?.stages || [])
        const filename = event.filename || existing?.filename || did

        // If this is a new item and the event is for a stage beyond extract,
        // pre-fill earlier stages as done (e.g. resummarize only re-runs summarize)
        if (!existing && stage !== 'extract') {
          const stageIdx = PIPELINE_STAGES.indexOf(stage)
          for (let i = 0; i < stageIdx; i++) {
            if (!stages.has(PIPELINE_STAGES[i])) {
              stages.set(PIPELINE_STAGES[i], { status: 'done' })
            }
          }
        }

        // Handle OCR/entities skip: done without prior running event
        if ((stage === 'ocr' || stage === 'entities') && status === 'done' && !stages.has(stage)) {
          stages.set(stage, { status: 'skipped' })
        } else {
          stages.set(stage, {
            status,
            progress: event.progress,
            total: event.total,
          })
        }

        const current_stage = computeCurrentStage(stages)
        const overall_progress = computeOverallProgress(stages)
        const itemStatus = computeItemStatus(stages)

        next.set(did, {
          doc_id: did,
          filename,
          stages,
          current_stage,
          overall_progress,
          status: itemStatus,
          enqueued_at: existing?.enqueued_at || Date.now(),
          updated_at: Date.now(),
        })
        return next
      })

      triggerToast()
    },
    [schedulePurge, triggerToast],
  )

  useJobEvents(onEvent)

  // Backfill + periodic resync. Fetches /api/jobs/active to (a) populate
  // initial state on mount and (b) correct drift caused by missed SSE
  // events.
  //
  // Drift happens because the SSE connection auto-reconnects after a
  // disconnect (network blip, sleep/wake, idle TCP timeout, backend
  // hiccup during model switch) and starts streaming from "now" — every
  // job event that fired during the gap is lost. With long ingestion
  // runs the Active count can diverge from reality by hundreds of items.
  //
  // The fix runs the same backfill on a 15 s interval, and after each
  // response any vid in activeItems that is NOT in the response gets
  // removed silently (no toast, no completed-list entry — those events
  // already fired in the past, the user has moved on). SSE remains the
  // primary update channel; this is the truth-correction layer.
  useEffect(() => {
    if (!token) return
    let cancelled = false

    async function resync() {
      try {
        const res = await fetch('/api/jobs/active', {
          headers: { Authorization: `Bearer ${token}` },
        })
        if (!res.ok || cancelled) return
        const events: JobEvent[] = await res.json()

        // Apply each event through the normal handler so create/update
        // logic stays in one place. Backfill events are always for
        // queued/running stages, so they never trigger the finalize-done
        // or error branches in onEvent.
        for (const event of events) {
          if (cancelled) return
          onEvent(event)
        }

        // Ghost-removal: anything in activeItems that's not in the
        // backfill response has finished (or errored) and we missed the
        // notification. Drop it silently.
        const liveVids = new Set(events.map((e) => e.doc_id))
        setActiveItems((prev) => {
          let changed = false
          const next = new Map(prev)
          for (const vid of next.keys()) {
            if (!liveVids.has(vid)) {
              next.delete(vid)
              changed = true
            }
          }
          return changed ? next : prev
        })
      } catch {
        // non-fatal — next tick will retry; SSE may be carrying the load.
      }
    }

    resync()
    const id = setInterval(resync, 15_000)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [token, onEvent])

  // Cleanup timers on unmount
  useEffect(() => {
    return () => {
      if (toastTimerRef.current) clearTimeout(toastTimerRef.current)
      for (const t of purgeTimersRef.current.values()) clearTimeout(t)
    }
  }, [])

  const toggleExpanded = useCallback(() => {
    setTrayState((prev) => {
      if (prev === 'expanded') return 'collapsed'
      if (toastTimerRef.current) clearTimeout(toastTimerRef.current)
      return 'expanded'
    })
  }, [])

  const collapse = useCallback(() => {
    if (toastTimerRef.current) clearTimeout(toastTimerRef.current)
    setTrayState('collapsed')
  }, [])

  return {
    trayState,
    activeItems,
    completed,
    summarizing,
    summarizeBacklog: summarizing.length,
    toggleExpanded,
    collapse,
  }
}

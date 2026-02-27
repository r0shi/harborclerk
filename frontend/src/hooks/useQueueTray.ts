import { useCallback, useEffect, useRef, useState } from 'react'
import { useJobEvents, type JobEvent } from './useJobEvents'

export type TrayState = 'collapsed' | 'toasting' | 'expanded'

export interface StageState {
  status: 'queued' | 'running' | 'done' | 'error' | 'skipped'
  progress?: number
  total?: number
}

export interface DocumentQueueItem {
  version_id: string
  filename: string
  stages: Map<string, StageState>
  current_stage: string
  overall_progress: number // 0-100
  status: 'running' | 'queued' | 'error'
  updated_at: number
}

export interface CompletedItem {
  version_id: string
  doc_id?: string
  filename: string
  status: 'done' | 'error'
  error_stage?: string
  page_count?: number
  chunk_count?: number
  finished_at: number
}

const HISTORY_CAP = 20
const HISTORY_TTL = 3_600_000 // 1 hour
const TOAST_DURATION = 4_000 // 4s
const TOAST_DEBOUNCE = 500 // ms
const PIPELINE_STAGES = ['extract', 'ocr', 'chunk', 'embed', 'summarize', 'finalize']

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
  const [trayState, setTrayState] = useState<TrayState>('collapsed')
  const [activeItems, setActiveItems] = useState<Map<string, DocumentQueueItem>>(new Map())
  const [completed, setCompleted] = useState<CompletedItem[]>([])

  const trayStateRef = useRef(trayState)
  trayStateRef.current = trayState

  const toastTimerRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  const lastToastRef = useRef(0)
  const purgeTimersRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map())

  // Schedule purge of a completed item after TTL
  const schedulePurge = useCallback((versionId: string) => {
    const existing = purgeTimersRef.current.get(versionId)
    if (existing) clearTimeout(existing)

    const timer = setTimeout(() => {
      setCompleted((prev) => prev.filter((c) => c.version_id !== versionId))
      purgeTimersRef.current.delete(versionId)
    }, HISTORY_TTL)
    purgeTimersRef.current.set(versionId, timer)
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
      const vid = event.version_id
      const stage = event.stage
      const status = event.status as StageState['status']

      // Handle finalize done -> move to completed
      if (stage === 'finalize' && status === 'done') {
        setActiveItems((prev) => {
          const next = new Map(prev)
          const item = next.get(vid)
          const filename = event.filename || item?.filename || vid

          // Build completed entry
          const entry: CompletedItem = {
            version_id: vid,
            doc_id: event.doc_id,
            filename,
            status: 'done',
            page_count: event.page_count,
            chunk_count: event.chunk_count,
            finished_at: Date.now(),
          }

          setCompleted((prev) => {
            const filtered = prev.filter((c) => c.version_id !== vid)
            return [entry, ...filtered].slice(0, HISTORY_CAP)
          })
          schedulePurge(vid)

          next.delete(vid)
          return next
        })
        triggerToast()
        return
      }

      // Handle error -> move to completed
      if (status === 'error') {
        setActiveItems((prev) => {
          const next = new Map(prev)
          const item = next.get(vid)
          const filename = event.filename || item?.filename || vid

          const entry: CompletedItem = {
            version_id: vid,
            filename,
            status: 'error',
            error_stage: stage,
            finished_at: Date.now(),
          }

          setCompleted((prev) => {
            const filtered = prev.filter((c) => c.version_id !== vid)
            return [entry, ...filtered].slice(0, HISTORY_CAP)
          })
          schedulePurge(vid)

          next.delete(vid)
          return next
        })
        triggerToast()
        return
      }

      // Active event: update or create DocumentQueueItem
      setActiveItems((prev) => {
        const next = new Map(prev)
        const existing = next.get(vid)
        const stages = new Map(existing?.stages || [])
        const filename = event.filename || existing?.filename || vid

        // Handle OCR skip: done without prior running event
        if (stage === 'ocr' && status === 'done' && !stages.has('ocr')) {
          stages.set('ocr', { status: 'skipped' })
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

        next.set(vid, {
          version_id: vid,
          filename,
          stages,
          current_stage,
          overall_progress,
          status: itemStatus,
          updated_at: Date.now(),
        })
        return next
      })

      triggerToast()
    },
    [schedulePurge, triggerToast],
  )

  useJobEvents(onEvent)

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
    toggleExpanded,
    collapse,
  }
}

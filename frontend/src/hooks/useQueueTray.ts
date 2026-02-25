import { useCallback, useEffect, useRef, useState } from 'react'
import { useJobEvents, type JobEvent } from './useJobEvents'

export type TrayState = 'collapsed' | 'toasting' | 'expanded'

export interface ActiveJob {
  version_id: string
  stage: string
  status: string
  progress_current?: number
  progress_total?: number
  filename?: string
  updated_at: number
}

export interface HistoryEntry {
  key: string
  version_id: string
  stage: string
  status: 'done' | 'error'
  filename?: string
  finished_at: number
}

const HISTORY_CAP = 20
const HISTORY_TTL = 30_000 // 30s
const TOAST_DURATION = 4_000 // 4s
const TOAST_DEBOUNCE = 500 // ms

export function useQueueTray() {
  const [trayState, setTrayState] = useState<TrayState>('collapsed')
  const [activeJobs, setActiveJobs] = useState<Map<string, ActiveJob>>(new Map())
  const [history, setHistory] = useState<HistoryEntry[]>([])

  const trayStateRef = useRef(trayState)
  trayStateRef.current = trayState

  const toastTimerRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  const lastToastRef = useRef(0)
  const historyTimersRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map())

  // Auto-remove history entries after TTL
  const scheduleHistoryRemoval = useCallback((key: string) => {
    const existing = historyTimersRef.current.get(key)
    if (existing) clearTimeout(existing)

    const timer = setTimeout(() => {
      setHistory((prev) => prev.filter((h) => h.key !== key))
      historyTimersRef.current.delete(key)
    }, HISTORY_TTL)
    historyTimersRef.current.set(key, timer)
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
      const key = `${event.version_id}:${event.stage}`

      if (event.status === 'done' || event.status === 'error') {
        // Move to history
        setActiveJobs((prev) => {
          const next = new Map(prev)
          next.delete(key)
          return next
        })
        setHistory((prev) => {
          const entry: HistoryEntry = {
            key,
            version_id: event.version_id,
            stage: event.stage,
            status: event.status as 'done' | 'error',
            filename: event.filename,
            finished_at: Date.now(),
          }
          const next = [entry, ...prev.filter((h) => h.key !== key)]
          return next.slice(0, HISTORY_CAP)
        })
        scheduleHistoryRemoval(key)
      } else {
        setActiveJobs((prev) => {
          const next = new Map(prev)
          next.set(key, {
            version_id: event.version_id,
            stage: event.stage,
            status: event.status,
            progress_current: event.progress,
            progress_total: event.total,
            filename: event.filename || prev.get(key)?.filename,
            updated_at: Date.now(),
          })
          return next
        })
      }

      triggerToast()
    },
    [scheduleHistoryRemoval, triggerToast],
  )

  useJobEvents(onEvent)

  // Cleanup timers on unmount
  useEffect(() => {
    return () => {
      if (toastTimerRef.current) clearTimeout(toastTimerRef.current)
      for (const t of historyTimersRef.current.values()) clearTimeout(t)
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
    activeJobs,
    history,
    toggleExpanded,
    collapse,
  }
}

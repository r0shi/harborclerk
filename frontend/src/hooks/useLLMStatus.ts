import { useCallback, useEffect, useRef, useState } from 'react'
import { useAuth } from '../auth'

export type LLMState = 'deactivated' | 'loading' | 'ready' | 'unknown'

export interface LLMStatus {
  state: LLMState
  model_id: string | null
  model_name: string | null
}

const IDLE_INTERVAL_MS = 8000 // poll every 8s when nothing seems to be transitioning
const ACTIVE_INTERVAL_MS = 1500 // poll every 1.5s when a transition is in progress

/**
 * Polls /api/chat/models/status and exposes the current LLM
 * health-state. Self-tunes its polling rate:
 *  - 8 s when the state is `ready` or `deactivated` and the model id
 *    hasn't changed → cheap heartbeat.
 *  - 1.5 s while `loading` (we want the banner to flip to "ready"
 *    promptly when it does), or for ~90 s after a `markTransitioning()`
 *    call (to catch the brief window after activate where we wrote
 *    config but llama-server hasn't actually stopped serving the OLD
 *    model yet — `state` would still report `ready` and we'd miss the
 *    transition entirely).
 */
export function useLLMStatus() {
  const { token } = useAuth()
  const [status, setStatus] = useState<LLMStatus>({
    state: 'unknown',
    model_id: null,
    model_name: null,
  })
  const lastModelIdRef = useRef<string | null>(null)
  const transitionUntilRef = useRef<number>(0)

  const markTransitioning = useCallback((durationMs = 90_000) => {
    transitionUntilRef.current = Date.now() + durationMs
  }, [])

  useEffect(() => {
    if (!token) return
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | null = null

    async function tick() {
      if (cancelled) return
      try {
        const res = await fetch('/api/chat/models/status', {
          headers: { Authorization: `Bearer ${token}` },
        })
        if (res.ok && !cancelled) {
          const data: LLMStatus = await res.json()
          // Mark as transitioning if model_id changed under us — covers
          // the case where someone else (or another tab) flipped the
          // active model. We want the banner to do its job there too.
          if (lastModelIdRef.current !== null && lastModelIdRef.current !== data.model_id) {
            transitionUntilRef.current = Date.now() + 90_000
          }
          lastModelIdRef.current = data.model_id
          setStatus(data)
        }
      } catch {
        // Network blip — leave state as-is, next tick will retry.
      }

      if (cancelled) return
      const now = Date.now()
      const transitioning = now < transitionUntilRef.current || status.state === 'loading'
      const interval = transitioning ? ACTIVE_INTERVAL_MS : IDLE_INTERVAL_MS
      timer = setTimeout(tick, interval)
    }

    tick()
    return () => {
      cancelled = true
      if (timer) clearTimeout(timer)
    }
    // status.state is intentionally read inside tick (via closure on
    // the latest setState callback path); leaving deps minimal so we
    // don't re-create the timer on every state change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token])

  return { status, markTransitioning }
}

import { useCallback, useEffect, useRef, useState } from 'react'
import { useAuth } from '../auth'

export type LLMState = 'deactivated' | 'loading' | 'ready' | 'unknown'

// Per-stage summarize backend. Decoupled from the llama-server fields above
// because the summarize stage can run against AFM (Apple Intelligence) — or
// fall back to extractive — independently of the active llama-server model.
export interface SummarizeBackend {
  backend: string // 'apple-intelligence' | <llm_model_id> | 'extractive'
  name: string | null
  state: LLMState
}

// The active model's weights alone do not fit this Mac, so the launcher refuses to start it and `loading`
// never ends (#709). The figures are the Models page's: this Mac's memory and the tier the model needs.
export interface DoesNotFitHere {
  system_ram_gb: number
  min_ram_gb: number
}

export interface LLMStatus {
  state: LLMState
  model_id: string | null
  model_name: string | null
  summarize?: SummarizeBackend
  does_not_fit_here?: DoesNotFitHere | null
  // Docker Compose only: what to do when no model is active (download LLM_MODEL_FILE in Settings → Models).
  setup_hint?: string | null
  // Docker Compose only: the LLM_MODEL_FILE value (possibly empty). null or absent outside Compose, where models
  // are activated from the Models page.
  compose_model_file?: string | null
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

/**
 * Under Docker Compose the model is LLM_MODEL_FILE, active once its file is downloaded (#654): the setup step is
 * the server's own advice (`setup_hint`, which names the file or says why it cannot run), not "activate one".
 * With no hint, a model id or a loading state means the file is there and llama-server is loading it, or
 * crash-looping on it: "download it" would be wrong, so the step points at the server's log instead.
 * null outside Compose, or before the status says which.
 */
export const COMPOSE_LOADING_STEP =
  "The local model is loading; if this persists, check 'docker compose logs llama-server'."

export function composeSetupStep(status: LLMStatus | null, platform?: 'macos' | 'docker'): string | null {
  if (status && status.compose_model_file != null) {
    if (status.setup_hint != null) return status.setup_hint
    if (status.model_id != null || status.state === 'loading') return COMPOSE_LOADING_STEP
    return `The model is chosen by LLM_MODEL_FILE in .env${status.compose_model_file ? ` (${status.compose_model_file})` : ''}; download it in Settings → Models.`
  }
  // Platform 'docker' only means WATCH_ROOT is set, which a source run or a plain `docker run` also does, and
  // Activate works there. So the Compose wording stands in only until the status says which it is.
  const statusKnown = status != null && status.state !== 'unknown'
  if (!statusKnown && platform === 'docker')
    return 'Download the model LLM_MODEL_FILE in .env names, in Settings → Models.'
  return null
}

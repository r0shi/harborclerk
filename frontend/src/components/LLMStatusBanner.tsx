import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from 'react'
import { useLLMStatus, type LLMStatus } from '../hooks/useLLMStatus'

interface LLMStatusContextValue {
  status: LLMStatus
  markTransitioning: (durationMs?: number) => void
}

const LLMStatusContext = createContext<LLMStatusContextValue | null>(null)

/**
 * App-level provider for LLM status polling. Mount once near the top
 * of the tree (Layout). Components anywhere can call
 * `useLLMStatusContext()` to read the latest state or signal a
 * transition (e.g. ModelsPage's activate button).
 */
export function LLMStatusProvider({ children }: { children: ReactNode }) {
  const value = useLLMStatus()
  return <LLMStatusContext.Provider value={value}>{children}</LLMStatusContext.Provider>
}

export function useLLMStatusContext(): LLMStatusContextValue {
  const ctx = useContext(LLMStatusContext)
  if (!ctx) {
    throw new Error('useLLMStatusContext must be used inside LLMStatusProvider')
  }
  return ctx
}

/**
 * Floating banner that surfaces the LLM-loading transition to the
 * user. Shows up briefly during a model switch ("Loading X…") and
 * flashes a success state for ~2 s before tucking itself away.
 *
 * Visible globally because llama-server can take 30-60 s to load a
 * 22 GB model, during which the rest of the app is sluggish under
 * memory pressure (kernel pages out everything else to make room
 * for the new model). The banner says "yes, this is intentional;
 * we'll be back in a minute".
 */
export function LLMStatusBanner() {
  const { status } = useLLMStatusContext()
  const [showSuccess, setShowSuccess] = useState<{ modelId: string } | null>(null)
  // Tracked via ref (not state) so updating it inside the effect
  // doesn't trigger a synchronous setState-in-effect cascade — that
  // pattern is flagged by react-hooks/set-state-in-effect.
  const prevStateRef = useRef(status.state)

  // When the state transitions from loading → ready, briefly flash
  // the success variant so the user sees positive confirmation, not
  // just a banner that quietly disappears.
  useEffect(() => {
    if (prevStateRef.current === 'loading' && status.state === 'ready' && status.model_id) {
      setShowSuccess({ modelId: status.model_id })
      prevStateRef.current = status.state
      const t = setTimeout(() => setShowSuccess(null), 2000)
      return () => clearTimeout(t)
    }
    prevStateRef.current = status.state
  }, [status.state, status.model_id])

  const isLoading = status.state === 'loading'
  if (!isLoading && !showSuccess) return null

  return (
    <div
      role="status"
      aria-live="polite"
      className="fixed bottom-4 right-4 z-50 max-w-sm rounded-xl bg-(--bg-vibrancy) backdrop-blur-xl shadow-mac-lg ring-1 ring-(--color-border) px-4 py-3"
    >
      <div className="flex items-center gap-3">
        {isLoading ? (
          <>
            <Spinner />
            <div className="min-w-0">
              <div className="text-[13px] font-medium text-(--color-text-primary)">
                Loading model{status.model_id ? '…' : ''}
              </div>
              {status.model_id && (
                <div className="text-[11px] text-(--color-text-secondary) truncate">
                  {status.model_id} · the rest of the app may feel slow until weights finish loading
                </div>
              )}
            </div>
          </>
        ) : showSuccess ? (
          <>
            <CheckMark />
            <div className="min-w-0">
              <div className="text-[13px] font-medium text-(--color-text-primary)">Model ready</div>
              <div className="text-[11px] text-(--color-text-secondary) truncate">{showSuccess.modelId}</div>
            </div>
          </>
        ) : null}
      </div>
    </div>
  )
}

function Spinner() {
  return (
    <svg className="h-4 w-4 shrink-0 animate-spin text-(--color-accent)" viewBox="0 0 24 24" fill="none">
      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" />
      <path className="opacity-90" fill="currentColor" d="M4 12a8 8 0 0 1 8-8v3a5 5 0 0 0-5 5H4Z" />
    </svg>
  )
}

function CheckMark() {
  return (
    <svg
      className="h-4 w-4 shrink-0 text-green-500"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2.5}
    >
      <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
    </svg>
  )
}

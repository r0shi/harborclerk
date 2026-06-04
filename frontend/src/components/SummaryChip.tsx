// frontend/src/components/SummaryChip.tsx

import { useState } from 'react'

export type SummaryState = 'generating' | 'pending' | 'extractive' | 'summarized' | 'failed' | 'none'

interface SummaryChipProps {
  state: SummaryState
  /**
   * Optional retry handler. When provided AND state is 'failed', the chip
   * becomes a clickable button that re-runs the summarize stage. No-op for
   * every other state (the chip stays a passive label).
   */
  onRetry?: () => void | Promise<void>
}

const LABELS: Record<Exclude<SummaryState, 'none'>, string> = {
  generating: 'Summary Generating',
  pending: 'Summary Pending',
  extractive: 'Extractive Only',
  summarized: 'Summarized',
  failed: 'Summary Failed',
}

const TOOLTIPS: Partial<Record<Exclude<SummaryState, 'none'>, string>> = {
  extractive:
    'No language model was active when this summary was generated, so the system fell back to an extractive heuristic. Configure a local AI model in Settings → Models for higher-quality summaries on future documents.',
  failed: 'The summarize stage errored. The document is otherwise fully ingested and searchable.',
}

const STYLES: Record<Exclude<SummaryState, 'none'>, { bg: string; fg: string }> = {
  generating: { bg: 'rgba(186,140,255,0.16)', fg: '#c4a4ff' },
  pending: { bg: 'rgba(255,214,10,0.12)', fg: '#ffd60a' },
  extractive: { bg: 'rgba(255,159,10,0.14)', fg: '#ff9f0a' },
  summarized: { bg: 'rgba(48,209,88,0.13)', fg: '#30d158' },
  failed: { bg: 'rgba(255,69,58,0.13)', fg: '#ff453a' },
}

/**
 * Inline chip surfacing the latest summarize-stage state for a document.
 * Used in the DocumentsPage row's fixed-width chip column. Renders nothing
 * for state="none" so callers can pass derived state and let layout
 * collapse naturally. The chip is left-anchored within its <td>, so the
 * dot's X position is constant across rows.
 */
export default function SummaryChip({ state, onRetry }: SummaryChipProps) {
  const [retrying, setRetrying] = useState(false)
  if (state === 'none') return null
  const { bg, fg } = STYLES[state]
  const pulseDot = state === 'generating'
  const canRetry = state === 'failed' && !!onRetry

  if (canRetry) {
    const handleClick = async () => {
      if (retrying) return
      setRetrying(true)
      try {
        await onRetry!()
      } finally {
        setRetrying(false)
      }
    }
    return (
      <button
        type="button"
        onClick={handleClick}
        disabled={retrying}
        className="inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-[11px] font-medium whitespace-nowrap cursor-pointer transition-opacity hover:opacity-80 disabled:cursor-default disabled:opacity-60"
        style={{ background: bg, color: fg }}
        title="Click to re-run the summary for this document."
      >
        <span
          className={`h-1.5 w-1.5 rounded-full ${retrying ? 'animate-pulse' : ''}`}
          style={{ background: fg, flex: '0 0 auto' }}
        />
        {retrying ? 'Retrying…' : 'Summary Failed ↻'}
      </button>
    )
  }

  return (
    <span
      className="inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-[11px] font-medium whitespace-nowrap"
      style={{ background: bg, color: fg }}
      title={TOOLTIPS[state]}
    >
      <span
        className={`h-1.5 w-1.5 rounded-full ${pulseDot ? 'animate-pulse' : ''}`}
        style={{ background: fg, flex: '0 0 auto' }}
      />
      {LABELS[state]}
    </span>
  )
}

/**
 * Resolve the SummaryState from the doc's persisted fields + latest
 * job status. Order matters — running/queued/error take precedence
 * over terminal-state inferences from `summary` / `summary_model`.
 */
export function resolveSummaryState(
  summary: string | null | undefined,
  summary_model: string | null | undefined,
  summarize_job_status: string | null | undefined,
): SummaryState {
  if (summarize_job_status === 'running') return 'generating'
  if (summarize_job_status === 'queued') return 'pending'
  if (summarize_job_status === 'error') return 'failed'
  if (summary && summary.trim()) {
    if (summary_model === 'extractive') return 'extractive'
    return 'summarized'
  }
  return 'none'
}

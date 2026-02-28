import { useState } from 'react'
import { stageLabel } from '../../utils/stageLabel'
import { PIPELINE_STAGES, type DocumentQueueItem } from '../../hooks/useQueueTray'

interface DocumentRowProps {
  item: DocumentQueueItem
}

export default function DocumentRow({ item }: DocumentRowProps) {
  const [expanded, setExpanded] = useState(false)

  const currentState = item.stages.get(item.current_stage)
  const hasSubProgress =
    currentState?.status === 'running' && currentState.total != null && currentState.total > 0

  // Stage bar width
  let stageBarWidth = '0%'
  let stageBarClass = 'queue-stage-bar bg-[var(--color-accent)]'
  if (item.status === 'running' && currentState?.status === 'running') {
    if (hasSubProgress) {
      stageBarWidth = `${Math.round(((currentState.progress || 0) / currentState.total!) * 100)}%`
    } else {
      stageBarWidth = '100%'
      stageBarClass = 'queue-shimmer'
    }
  }

  return (
    <div className="py-1.5">
      {/* Header: filename + chevron */}
      <div className="flex items-center justify-between gap-2">
        <span className="text-sm font-medium text-[var(--color-text-primary)] truncate min-w-0">
          {item.filename}
        </span>
        <button
          onClick={() => setExpanded((p) => !p)}
          className="shrink-0 rounded-md p-0.5 text-[var(--color-text-secondary)] hover:text-[var(--color-text-primary)] hover:bg-black/[0.04] dark:hover:bg-white/[0.06] transition-colors"
        >
          <svg
            className={`h-3.5 w-3.5 queue-chevron ${expanded ? 'expanded' : ''}`}
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            strokeWidth={2}
          >
            <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 8.25l-7.5 7.5-7.5-7.5" />
          </svg>
        </button>
      </div>

      {/* Stage label + sub-progress */}
      <div className="text-xs text-[var(--color-text-secondary)] mt-0.5">
        {item.status === 'queued' ? (
          'Queued'
        ) : (
          <>
            {stageLabel(item.current_stage)}
            {hasSubProgress && (
              <span className="ml-1">
                {currentState.progress} of {currentState.total}
              </span>
            )}
          </>
        )}
      </div>

      {/* Dual progress bars */}
      <div className="flex flex-col gap-1.5 mt-1.5">
        {/* Stage bar (3px) */}
        <div className="h-[3px] rounded-full bg-black/[0.04] dark:bg-white/[0.06] overflow-hidden">
          {item.status !== 'queued' && (
            <div
              className={`h-full rounded-full ${stageBarClass}`}
              style={{ width: stageBarWidth }}
            />
          )}
        </div>
        {/* Overall bar (5px) */}
        <div className="h-[5px] rounded-full bg-black/[0.04] dark:bg-white/[0.06] overflow-hidden">
          <div
            className="h-full rounded-full queue-overall-bar"
            style={{ width: `${item.overall_progress}%` }}
          />
        </div>
      </div>

      {/* Expandable detail */}
      <div className={`queue-row-detail ${expanded ? 'expanded' : ''}`}>
        <div>
          <div className="grid grid-cols-2 gap-x-4 gap-y-1 pt-2 pb-1">
            {PIPELINE_STAGES.map((stage) => {
              const state = item.stages.get(stage)
              const status = state?.status || 'queued'
              return (
                <div key={stage} className="flex items-center gap-1.5 text-xs">
                  <StageIcon status={status} stage={stage} />
                  <span
                    className={
                      status === 'skipped'
                        ? 'text-[var(--color-text-secondary)] italic'
                        : status === 'queued'
                          ? 'text-[var(--color-text-secondary)]'
                          : 'text-[var(--color-text-primary)]'
                    }
                  >
                    {stageName(stage)}
                    {status === 'running' && state?.total != null && state.total > 0 && (
                      <span className="text-[var(--color-text-secondary)] ml-1">
                        {state.progress}/{state.total}
                      </span>
                    )}
                  </span>
                </div>
              )
            })}
          </div>
        </div>
      </div>
    </div>
  )
}

function StageIcon({ status, stage }: { status: string; stage?: string }) {
  switch (status) {
    case 'done':
      return <span className="text-green-500 font-medium leading-none">&check;</span>
    case 'running':
      return <span className="text-[var(--color-accent)] leading-none">&bull;</span>
    case 'error':
      return <span className="text-red-500 font-medium leading-none">&times;</span>
    case 'skipped':
      if (stage === 'entities')
        return <span className="text-amber-500 leading-none" title="NER not available">&#9888;</span>
      return <span className="text-[var(--color-text-secondary)] leading-none">&ndash;</span>
    default:
      return <span className="text-[var(--color-text-secondary)] leading-none">&#9675;</span>
  }
}

function stageName(stage: string): string {
  switch (stage) {
    case 'extract':
      return 'Extract'
    case 'ocr':
      return 'OCR'
    case 'chunk':
      return 'Chunk'
    case 'entities':
      return 'Entities'
    case 'embed':
      return 'Embed'
    case 'summarize':
      return 'Summarize'
    case 'finalize':
      return 'Finalize'
    default:
      return stage
  }
}

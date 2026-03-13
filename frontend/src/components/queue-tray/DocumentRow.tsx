import { useState } from 'react'
import { stageLabel } from '../../utils/stageLabel'
import { PIPELINE_STAGES, type DocumentQueueItem } from '../../hooks/useQueueTray'
import StageRing from './StageRing'

interface DocumentRowProps {
  item: DocumentQueueItem
}

export default function DocumentRow({ item }: DocumentRowProps) {
  const [expanded, setExpanded] = useState(false)

  const currentState = item.stages.get(item.current_stage)
  const hasSubProgress = currentState?.status === 'running' && currentState.total != null && currentState.total > 0

  // Count active (non-skipped) stages and current step
  const activeStages = PIPELINE_STAGES.filter((s) => {
    const st = item.stages.get(s)
    return !st || st.status !== 'skipped'
  })
  let doneCount = 0
  for (const s of activeStages) {
    const st = item.stages.get(s)
    if (st?.status === 'done') doneCount++
    else break
  }
  const runningStage = activeStages.find((s) => item.stages.get(s)?.status === 'running')
  const currentStep = runningStage ? doneCount + 1 : doneCount

  return (
    <div className="py-1.5">
      {/* Main row: ring + info + chevron */}
      <div className="flex items-center gap-2.5">
        <StageRing stages={item.stages} size={36} />

        <div className="min-w-0 flex-1">
          <div className="text-sm font-medium text-(--color-text-primary) truncate">{item.filename}</div>
          <div className="text-xs text-(--color-text-secondary)">
            {item.status === 'queued' ? (
              'Queued'
            ) : (
              <>
                {stageLabel(item.current_stage)}
                {hasSubProgress && (
                  <span className="ml-1">
                    — {currentState.progress}/{currentState.total} pages
                  </span>
                )}
                <span className="ml-1 text-(--color-text-secondary)">
                  (Step {currentStep}/{activeStages.length})
                </span>
              </>
            )}
          </div>
        </div>

        <button
          onClick={() => setExpanded((p) => !p)}
          className="shrink-0 rounded-md p-0.5 text-(--color-text-secondary) hover:text-(--color-text-primary) hover:bg-black/4 dark:hover:bg-white/6 transition-colors"
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
                        ? 'text-(--color-text-secondary) italic'
                        : status === 'queued'
                          ? 'text-(--color-text-secondary)'
                          : 'text-(--color-text-primary)'
                    }
                  >
                    {stageName(stage)}
                    {status === 'running' && state?.total != null && state.total > 0 && (
                      <span className="text-(--color-text-secondary) ml-1">
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
      return <span className="text-green-500 font-medium leading-none">{'\u2713'}</span>
    case 'running':
      return <span className="text-(--color-accent) leading-none">{'\u2022'}</span>
    case 'error':
      return <span className="text-red-500 font-medium leading-none">{'\u00d7'}</span>
    case 'skipped':
      if (stage === 'entities')
        return (
          <span className="text-amber-500 leading-none" title="NER not available">
            &#9888;
          </span>
        )
      return <span className="text-(--color-text-secondary) leading-none">{'\u2013'}</span>
    default:
      return <span className="text-(--color-text-secondary) leading-none">&#9675;</span>
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

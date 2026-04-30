import { classColor, queueForStage } from '../../utils/queueColors'
import type { StageTiming } from './PipelineTimingChart'

interface QueueWaitChartProps {
  pipelineTiming: Record<string, StageTiming>
}

const STAGES = ['extract', 'ocr', 'chunk', 'entities', 'embed', 'summarize', 'finalize'] as const
const STAGE_LABELS: Record<string, string> = {
  extract: 'Extract',
  ocr: 'OCR',
  chunk: 'Chunk',
  entities: 'Entities',
  embed: 'Embed',
  summarize: 'Summarize',
  finalize: 'Finalize',
}

function fmtSecs(secs: number): string {
  if (secs < 1) return `${Math.round(secs * 1000)}ms`
  if (secs < 60) return `${secs.toFixed(1)}s`
  const m = Math.floor(secs / 60)
  const s = Math.round(secs % 60)
  return `${m}m ${s}s`
}

const WAIT_FILL = 'rgba(128, 128, 128, 0.45)'

/**
 * Per-stage wait-vs-run breakdown as a stacked horizontal bar.
 *
 * Different signal from the timing-distribution chart: this answers
 * "where does a job spend its life" — sitting in the queue (waiting
 * for a free worker) vs actually being processed. A long grey wait
 * segment on a stage means workers are saturated upstream of it; a
 * long coloured run segment means the work itself is slow.
 */
export default function QueueWaitChart({ pipelineTiming }: QueueWaitChartProps) {
  const visibleStages = STAGES.filter((s) => pipelineTiming[s]?.count)
  if (visibleStages.length === 0) {
    return (
      <div className="text-[12px] text-(--color-text-secondary)">
        No completed jobs yet — process a document to populate.
      </div>
    )
  }
  const maxTotal = Math.max(
    0.001,
    ...visibleStages.map((s) => pipelineTiming[s].avg_wait_secs + pipelineTiming[s].avg_run_secs),
  )

  return (
    <div className="space-y-1.5">
      {visibleStages.map((stage) => {
        const t = pipelineTiming[stage]
        const queue = queueForStage(stage)
        const waitWidth = Math.min(100, (t.avg_wait_secs / maxTotal) * 100)
        const runWidth = Math.min(100, (t.avg_run_secs / maxTotal) * 100)
        return (
          <div key={stage} className="flex items-center gap-2 text-[12px]">
            <span className="w-16 shrink-0 text-(--color-text-secondary) text-right">{STAGE_LABELS[stage]}</span>
            <div
              className="relative flex-1 h-4 rounded-sm overflow-hidden"
              style={{ background: 'rgba(128,128,128,0.12)' }}
              title={`${STAGE_LABELS[stage]} — wait ${fmtSecs(t.avg_wait_secs)}, run ${fmtSecs(t.avg_run_secs)} (avg over n=${t.count})`}
            >
              <div className="flex h-full">
                {waitWidth > 0 && <div style={{ width: `${waitWidth}%`, background: WAIT_FILL }} />}
                {runWidth > 0 && <div style={{ width: `${runWidth}%`, background: classColor(queue) }} />}
              </div>
            </div>
            <span className="w-44 shrink-0 text-right tabular-nums text-(--color-text-secondary) text-[11px]">
              wait {fmtSecs(t.avg_wait_secs)} · run {fmtSecs(t.avg_run_secs)}
            </span>
          </div>
        )
      })}
      <div className="pt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-[10px] text-(--color-text-secondary)">
        <span className="inline-flex items-center gap-1">
          <span className="h-2 w-2 rounded-sm" style={{ background: WAIT_FILL }} />
          Time queued
        </span>
        <span className="inline-flex items-center gap-1">
          <span className="h-2 w-2 rounded-sm" style={{ background: classColor('io') }} />
          Processing (coloured by queue class)
        </span>
      </div>
    </div>
  )
}

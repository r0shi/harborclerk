import { classColor, classColorAlpha, queueForStage } from '../../utils/queueColors'

export interface StageTiming {
  avg_run_secs: number
  p50_run_secs: number
  p95_run_secs: number
  max_run_secs: number
  avg_wait_secs: number
  count: number
}

interface PipelineTimingChartProps {
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

/**
 * Per-stage processing time as a horizontal bar chart with a
 * within-bar p50 marker. The faded portion of each bar runs out to
 * p95; the saturated portion runs out to p50, so the eye reads
 * "median where it's filled, tail where it fades". Colour is the
 * stage's queue class for visual consistency with the diagram.
 *
 * The avg row's primary value is signalling spread, not centre — when
 * the saturated p50 bar is much shorter than the faded p95 bar, the
 * stage has a long tail (typical for OCR with mixed scan quality);
 * when they're close the stage is tightly clustered (chunk, finalize).
 */
export default function PipelineTimingChart({ pipelineTiming }: PipelineTimingChartProps) {
  const visibleStages = STAGES.filter((s) => pipelineTiming[s]?.count)
  if (visibleStages.length === 0) {
    return (
      <div className="text-[12px] text-(--color-text-secondary)">
        No completed jobs yet — process a document to populate.
      </div>
    )
  }
  const maxP95 = Math.max(0.001, ...visibleStages.map((s) => pipelineTiming[s].p95_run_secs))

  return (
    <div className="space-y-1.5">
      {visibleStages.map((stage) => {
        const t = pipelineTiming[stage]
        const queue = queueForStage(stage)
        const p50Width = Math.min(100, (t.p50_run_secs / maxP95) * 100)
        const p95Width = Math.min(100, (t.p95_run_secs / maxP95) * 100)
        return (
          <div key={stage} className="flex items-center gap-2 text-[12px]">
            <span className="w-16 shrink-0 text-(--color-text-secondary) text-right">{STAGE_LABELS[stage]}</span>
            <div
              className="relative flex-1 h-4 rounded-sm overflow-hidden"
              style={{ background: 'rgba(128,128,128,0.12)' }}
              title={`${STAGE_LABELS[stage]} — p50 ${fmtSecs(t.p50_run_secs)}, p95 ${fmtSecs(t.p95_run_secs)}, max ${fmtSecs(t.max_run_secs)}, avg ${fmtSecs(t.avg_run_secs)}, n=${t.count}`}
            >
              {/* p95 (faded outer reach) */}
              <div
                className="absolute left-0 top-0 h-full rounded-sm"
                style={{ width: `${p95Width}%`, background: classColorAlpha(queue, 0.3) }}
              />
              {/* p50 (saturated up to median) */}
              <div
                className="absolute left-0 top-0 h-full rounded-sm"
                style={{ width: `${p50Width}%`, background: classColor(queue) }}
              />
            </div>
            <span className="w-44 shrink-0 text-right tabular-nums text-(--color-text-secondary) text-[11px]">
              p50 {fmtSecs(t.p50_run_secs)} · p95 {fmtSecs(t.p95_run_secs)} · n={t.count}
            </span>
          </div>
        )
      })}
      <div className="pt-1 text-[10px] text-(--color-text-secondary)">
        Saturated bar = median (p50) · faded extension = p95 · numbers on the right show exact values
      </div>
    </div>
  )
}

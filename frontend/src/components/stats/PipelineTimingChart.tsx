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

const STAGES = ['extract', 'ocr', 'chunk', 'entities', 'embed', 'finalize'] as const
const STAGE_LABELS: Record<string, string> = {
  extract: 'Extract',
  ocr: 'OCR',
  chunk: 'Chunk',
  entities: 'Entities',
  embed: 'Embed',
  finalize: 'Finalize',
}

/**
 * Distribution-zone colours, fixed and identical on every stage so the
 * colour reads the metric (p50 / p95 / max) rather than the stage.
 * Traffic-light progression: green = typical, red = worst case.
 */
const METRIC_COLORS = {
  p50: '#34c759', // system green — median
  p95: '#ff9500', // system orange — 95th percentile
  max: '#ff3b30', // system red — slowest single run
}

function fmtSecs(secs: number): string {
  if (secs < 1) return `${Math.round(secs * 1000)}ms`
  if (secs < 60) return `${secs.toFixed(1)}s`
  const m = Math.floor(secs / 60)
  const s = Math.round(secs % 60)
  return `${m}m ${s}s`
}

/**
 * Per-stage processing time as a horizontal bar chart that breaks the
 * distribution into median / typical / outlier zones. Each zone has a
 * fixed colour (see METRIC_COLORS), the same on every stage, so the
 * colour reads the metric — not the stage. Reading guide:
 *
 *   green   : p50 (median) — half of jobs finished in this much time
 *   orange  : p95 — almost everyone is in by here
 *   red ┊   : max (tick mark) — the slowest single run we've seen
 *
 * When the green p50 zone fills most of the bar, the stage is tightly
 * clustered. When the green zone is short and the orange p95 extension
 * is long, the stage is bimodal — typical case is fast, but with a
 * heavy tail (this is what map-reduce summarize does on long docs:
 * each call is normal, but the *job* multiplies that by however many
 * groups were needed). The max tick gives you the actual worst-case
 * wall time so you can spot pathological outliers even when p95 looks
 * reasonable.
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
  // Scale the bar width by the largest max across visible stages
  // (rather than p95) so the max tick has a stable home on the
  // axis; otherwise a wild outlier in one stage would push the tick
  // off the right edge or compress everyone else into invisibility.
  const axisMax = Math.max(0.001, ...visibleStages.map((s) => pipelineTiming[s].max_run_secs))

  return (
    <div className="space-y-1.5">
      {visibleStages.map((stage) => {
        const t = pipelineTiming[stage]
        const p50Width = Math.min(100, (t.p50_run_secs / axisMax) * 100)
        const p95Width = Math.min(100, (t.p95_run_secs / axisMax) * 100)
        const maxLeft = Math.min(100, (t.max_run_secs / axisMax) * 100)
        // Flag a notably long tail — saturated bar way shorter than
        // the max, suggesting a bimodal distribution (typical run is
        // fine, worst run is wildly slower). Useful for spotting
        // map-reduce blow-ups on summarize.
        const longTail = t.max_run_secs > t.p50_run_secs * 5 && t.max_run_secs > 5
        return (
          <div key={stage} className="flex items-center gap-2 text-[12px]">
            <span className="w-16 shrink-0 text-(--color-text-secondary) text-right">{STAGE_LABELS[stage]}</span>
            <div
              className="relative flex-1 h-4 rounded-sm overflow-hidden"
              style={{ background: 'rgba(128,128,128,0.12)' }}
              title={`${STAGE_LABELS[stage]} — p50 ${fmtSecs(t.p50_run_secs)}, p95 ${fmtSecs(t.p95_run_secs)}, max ${fmtSecs(t.max_run_secs)}, avg ${fmtSecs(t.avg_run_secs)}, n=${t.count}`}
            >
              {/* p95 zone — orange. Runs the full length up to p95; the
                  p50 layer below paints over its left portion, so what
                  stays visible is the p50→p95 band. */}
              <div
                className="absolute left-0 top-0 h-full rounded-sm"
                style={{ width: `${p95Width}%`, background: METRIC_COLORS.p95 }}
              />
              {/* p50 zone — green, 0→median */}
              <div
                className="absolute left-0 top-0 h-full rounded-sm"
                style={{ width: `${p50Width}%`, background: METRIC_COLORS.p50 }}
              />
              {/* max tick — vertical line at the longest single-run
                  position. Positioned on the centre of the line so a
                  max equal to p95 sits cleanly at the p95 edge. */}
              <div
                className="absolute top-0 h-full"
                style={{
                  left: `calc(${maxLeft}% - 1px)`,
                  width: '2px',
                  background: METRIC_COLORS.max,
                }}
              />
            </div>
            <span
              className={`w-56 shrink-0 text-right tabular-nums text-[11px] ${
                longTail ? 'text-(--color-text-primary)' : 'text-(--color-text-secondary)'
              }`}
            >
              p50 {fmtSecs(t.p50_run_secs)} · p95 {fmtSecs(t.p95_run_secs)} ·{' '}
              <span className={longTail ? 'font-semibold' : ''}>max {fmtSecs(t.max_run_secs)}</span> · n={t.count}
            </span>
          </div>
        )
      })}
      <div className="pt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-[10px] text-(--color-text-secondary)">
        <span className="inline-flex items-center gap-1">
          <span className="h-2 w-2 rounded-sm" style={{ background: METRIC_COLORS.p50 }} />
          p50 (median)
        </span>
        <span className="inline-flex items-center gap-1">
          <span className="h-2 w-2 rounded-sm" style={{ background: METRIC_COLORS.p95 }} />
          p95
        </span>
        <span className="inline-flex items-center gap-1">
          <span className="h-3 w-[2px]" style={{ background: METRIC_COLORS.max }} />
          max single run
        </span>
        <span>max bolded when ≥ 5× p50</span>
      </div>
    </div>
  )
}

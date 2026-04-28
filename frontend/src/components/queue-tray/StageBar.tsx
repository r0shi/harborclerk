import { PIPELINE_STAGES, type StageState } from '../../hooks/useQueueTray'

interface StageBarProps {
  stages: Map<string, StageState>
  /** Bar height in px. Default 6. */
  height?: number
}

const SEGMENT_GAP_PX = 2

interface FillStyle {
  width: string
  background: string
}

function fillFor(status: string, fraction: number | null): FillStyle | null {
  switch (status) {
    case 'done':
      return { width: '100%', background: '#30d158' }
    case 'error':
      return { width: '100%', background: '#ff453a' }
    case 'running':
      // With quantitative progress: partial fill. Without: full segment +
      // pulse (handled by caller via class).
      return {
        width: fraction !== null ? `${fraction * 100}%` : '100%',
        background: 'var(--color-accent)',
      }
    default:
      return null
  }
}

/**
 * Horizontal 7-segment progress bar — one segment per pipeline stage.
 * Replaces the SVG ring; reads left-to-right which matches the user's
 * mental model of the pipeline, and leaves room for an explicit
 * stage-name label below it.
 *
 * Each segment is a "track" (light grey) with an optional coloured fill
 * showing status: full green for done, partial accent for an in-flight
 * stage with quantitative progress (e.g. embed 47/120), pulsing accent
 * for an in-flight stage without sub-progress, full red for errored.
 * Skipped stages render as a slightly dimmer track to be visibly
 * distinct from "queued but not yet started".
 */
export default function StageBar({ stages, height = 6 }: StageBarProps) {
  return (
    <div
      className="flex w-full items-stretch"
      style={{ height, gap: `${SEGMENT_GAP_PX}px` }}
      role="progressbar"
      aria-valuemin={0}
      aria-valuemax={PIPELINE_STAGES.length}
    >
      {PIPELINE_STAGES.map((stage) => {
        const st = stages.get(stage)
        const status = st?.status || 'queued'
        const isRunning = status === 'running'
        const hasFraction = isRunning && st?.total != null && st.total > 0
        const fraction = hasFraction ? Math.min(1, (st!.progress || 0) / st!.total!) : null
        const fill = fillFor(status, fraction)

        // Track distinguishes "skipped" (deliberately not run for this
        // doc — no NER, OCR not needed, etc.) from "queued" (will run,
        // not started yet) at a glance.
        const trackBg = status === 'skipped' ? 'rgba(128,128,128,0.12)' : 'rgba(128,128,128,0.2)'

        return (
          <div
            key={stage}
            className="relative flex-1 overflow-hidden rounded-full"
            style={{ minWidth: 0, background: trackBg }}
            title={stage}
          >
            {fill && (
              <div
                className={`absolute inset-y-0 left-0 transition-[width] duration-500 ease-out ${
                  isRunning && !hasFraction ? 'animate-pulse' : ''
                }`}
                style={fill}
              />
            )}
          </div>
        )
      })}
    </div>
  )
}

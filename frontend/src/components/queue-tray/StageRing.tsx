import { PIPELINE_STAGES, type StageState } from '../../hooks/useQueueTray'

interface StageRingProps {
  stages: Map<string, StageState>
  size?: number
}

// Approximate weight per stage (proportional sizing)
const STAGE_WEIGHTS: Record<string, number> = {
  extract: 1,
  ocr: 3,
  chunk: 1,
  entities: 2,
  embed: 2,
  summarize: 1,
  finalize: 0.5,
}

const GAP_DEG = 2

function segmentColor(status: string): string {
  switch (status) {
    case 'done':
      return '#30d158'
    case 'running':
      return 'var(--color-accent)'
    case 'error':
      return '#ff453a'
    case 'skipped':
      return 'rgba(128,128,128,0.25)'
    default:
      return 'rgba(128,128,128,0.2)'
  }
}

export default function StageRing({ stages, size = 36 }: StageRingProps) {
  const cx = size / 2
  const cy = size / 2
  const strokeWidth = 3.5
  const radius = (size - strokeWidth) / 2
  const circumference = 2 * Math.PI * radius

  const activeStages = PIPELINE_STAGES.filter((s) => {
    const st = stages.get(s)
    return !st || st.status !== 'skipped'
  })

  const totalWeight = activeStages.reduce((sum, s) => sum + (STAGE_WEIGHTS[s] || 1), 0)
  const totalGapDeg = activeStages.length * GAP_DEG
  const availableDeg = 360 - totalGapDeg

  // Count steps for center text
  let doneCount = 0
  for (const s of activeStages) {
    const st = stages.get(s)
    if (st?.status === 'done') doneCount++
    else break
  }
  const runningStage = activeStages.find((s) => stages.get(s)?.status === 'running')
  const currentStep = runningStage ? doneCount + 1 : doneCount

  // Helper: compute start angle for a given stage index
  function startAngleFor(idx: number): number {
    return (
      -90 +
      activeStages
        .slice(0, idx)
        .reduce((sum, s) => sum + ((STAGE_WEIGHTS[s] || 1) / totalWeight) * availableDeg + GAP_DEG, 0)
    )
  }

  return (
    <svg width={size} height={size} className="shrink-0">
      {activeStages.map((stage, idx) => {
        const st = stages.get(stage)
        const status = st?.status || 'queued'
        const weight = STAGE_WEIGHTS[stage] || 1
        const segDeg = (weight / totalWeight) * availableDeg
        const segLen = (segDeg / 360) * circumference
        const startAngle = startAngleFor(idx)
        const offset = -(((startAngle + 90) / 360) * circumference)

        // For running stages with progress data, render a background track + filled portion
        if (status === 'running' && st?.total && st.total > 0) {
          const fraction = Math.min(1, (st.progress || 0) / st.total)
          const filledLen = segLen * fraction
          return (
            <g key={stage}>
              {/* Background track (unfilled portion) */}
              <circle
                cx={cx}
                cy={cy}
                r={radius}
                fill="none"
                stroke="rgba(128,128,128,0.2)"
                strokeWidth={strokeWidth}
                strokeDasharray={`${segLen} ${circumference - segLen}`}
                strokeDashoffset={offset}
                strokeLinecap="round"
              />
              {/* Filled portion */}
              {filledLen > 0.5 && (
                <circle
                  cx={cx}
                  cy={cy}
                  r={radius}
                  fill="none"
                  stroke="var(--color-accent)"
                  strokeWidth={strokeWidth}
                  strokeDasharray={`${filledLen} ${circumference - filledLen}`}
                  strokeDashoffset={offset}
                  strokeLinecap="round"
                  className="transition-[stroke-dasharray] duration-500 ease-out"
                />
              )}
            </g>
          )
        }

        return (
          <circle
            key={stage}
            cx={cx}
            cy={cy}
            r={radius}
            fill="none"
            stroke={segmentColor(status)}
            strokeWidth={strokeWidth}
            strokeDasharray={`${segLen} ${circumference - segLen}`}
            strokeDashoffset={offset}
            strokeLinecap="round"
            className={status === 'running' ? 'animate-pulse' : ''}
          />
        )
      })}
      <text
        x={cx}
        y={cy}
        textAnchor="middle"
        dominantBaseline="central"
        className="fill-(--color-text-primary)"
        fontSize={size * 0.28}
        fontWeight={600}
      >
        {currentStep}/{activeStages.length}
      </text>
    </svg>
  )
}

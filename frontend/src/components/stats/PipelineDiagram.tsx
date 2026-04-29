import { useEffect, useRef, useState } from 'react'
import { useQueueSnapshot, type QueueClass, type QueueSnapshot, type StageSnapshot } from '../../hooks/useQueueSnapshot'

/**
 * Pipeline Overview — animated DAG showing live activity in the
 * ingestion pipeline.
 *
 * Visual encoding:
 *   - Node radius scales with sqrt(queued + running) — a bottleneck
 *     swells without crushing other nodes.
 *   - Node hue distinguishes IO / CPU / LLM queue classes.
 *   - Soft glow filter on nodes with at least one running job — the
 *     "I am alive" cue.
 *   - Edges thicken with recent throughput on the downstream stage.
 *   - Particles flow along edges at a rate proportional to recent
 *     throughput. Each particle inherits the upstream stage's colour.
 *
 * Polls the same /api/jobs/snapshot endpoint as the drawer's Pipeline
 * tab. The snapshot includes `recent_completed` per stage (last 30 s),
 * which drives both edge thickness and particle spawn rate.
 *
 * No filenames or per-document text — by design. This is the
 * graphical-flow view; the drawer's Pipeline tab is the
 * info-dense status-watcher.
 */

const VIEW_WIDTH = 600
const VIEW_HEIGHT = 300

// Hand-laid-out positions for the 7-stage pipeline.
// Sequential prefix flows left-to-right at y=150, then chunk fans out
// to entities/embed/summarize stacked vertically, then re-converges
// at finalize.
const NODE_POSITIONS: Record<string, { x: number; y: number }> = {
  extract: { x: 60, y: 150 },
  ocr: { x: 160, y: 150 },
  chunk: { x: 260, y: 150 },
  entities: { x: 390, y: 70 },
  embed: { x: 390, y: 150 },
  summarize: { x: 390, y: 230 },
  finalize: { x: 520, y: 150 },
}

const EDGES: { from: string; to: string }[] = [
  { from: 'extract', to: 'ocr' },
  { from: 'ocr', to: 'chunk' },
  { from: 'chunk', to: 'entities' },
  { from: 'chunk', to: 'embed' },
  { from: 'chunk', to: 'summarize' },
  { from: 'entities', to: 'finalize' },
  { from: 'embed', to: 'finalize' },
  { from: 'summarize', to: 'finalize' },
]

const STAGE_LABELS: Record<string, string> = {
  extract: 'Extract',
  ocr: 'OCR',
  chunk: 'Chunk',
  entities: 'Entities',
  embed: 'Embed',
  summarize: 'Summarize',
  finalize: 'Finalize',
}

// Distinct hues by queue class so the eye reads "this is a CPU stage"
// vs "this is an IO stage" at a glance. CPU stages (OCR, Embed) are
// the heavy / slow compute ones; warm hue makes them stand out. The
// LLM queue (Summarize) gets its own purple — visually distinct and
// reinforces that it's a serialised single-worker bottleneck, not
// just another flavour of CPU work.
function classColor(queue: QueueClass | undefined): string {
  if (queue === 'cpu') return '#f5a623' // amber
  if (queue === 'llm') return '#bf5af2' // system purple
  return '#0a84ff' // accent blue (matches --color-accent)
}

function edgePath(from: string, to: string): string {
  const a = NODE_POSITIONS[from]
  const b = NODE_POSITIONS[to]
  const dx = b.x - a.x
  // Cubic Bezier with horizontally-pulled control points → smooth
  // S-curve when y differs, near-straight when y matches.
  const c1x = a.x + dx * 0.5
  const c1y = a.y
  const c2x = b.x - dx * 0.5
  const c2y = b.y
  return `M ${a.x} ${a.y} C ${c1x} ${c1y}, ${c2x} ${c2y}, ${b.x} ${b.y}`
}

function nodeRadius(info: StageSnapshot | undefined): number {
  if (!info) return 14
  const total = info.queued + info.running
  // sqrt scale so a long queue grows but doesn't overwhelm. Cap is
  // chosen so a busy node + its count badge above and stage label
  // below still fit cleanly in the 80 px vertical gap between rows
  // of the fan-out (entities / embed / summarize).
  return Math.min(26, 14 + Math.sqrt(total) * 2.5)
}

interface Particle {
  id: string
  edgeKey: string
  color: string
}

const PARTICLE_DURATION_MS = 1100

function PipelineGraph({ snapshot }: { snapshot: QueueSnapshot }) {
  const [particles, setParticles] = useState<Particle[]>([])
  const idCounterRef = useRef(0)

  // Spawn particles per edge based on the downstream stage's
  // recent throughput. Inter-particle interval = window / count.
  useEffect(() => {
    const windowMs = snapshot.throughput_window_seconds * 1000
    const intervals: ReturnType<typeof setInterval>[] = []
    const timeouts: ReturnType<typeof setTimeout>[] = []

    for (const edge of EDGES) {
      const downstream = snapshot.by_stage[edge.to]
      if (!downstream || downstream.recent_completed <= 0) continue

      const intervalMs = Math.max(180, Math.round(windowMs / downstream.recent_completed))
      const upstream = snapshot.by_stage[edge.from]
      const color = classColor(upstream?.queue)
      const edgeKey = `${edge.from}-${edge.to}`

      const id = setInterval(() => {
        const p: Particle = {
          id: `p-${idCounterRef.current++}`,
          edgeKey,
          color,
        }
        setParticles((prev) => [...prev, p])
        const t = setTimeout(() => {
          setParticles((prev) => prev.filter((x) => x.id !== p.id))
        }, PARTICLE_DURATION_MS)
        timeouts.push(t)
      }, intervalMs)
      intervals.push(id)
    }

    return () => {
      intervals.forEach(clearInterval)
      timeouts.forEach(clearTimeout)
    }
  }, [snapshot])

  return (
    <svg
      viewBox={`0 0 ${VIEW_WIDTH} ${VIEW_HEIGHT}`}
      className="w-full"
      style={{ aspectRatio: `${VIEW_WIDTH} / ${VIEW_HEIGHT}` }}
    >
      <defs>
        {/* Glow filter for active nodes — dialled back to suit the
            smaller node size; bigger blur made tightly-stacked nodes
            (entities / embed / summarize) bleed into each other. */}
        <filter id="pipeline-glow" x="-50%" y="-50%" width="200%" height="200%">
          <feGaussianBlur stdDeviation="2.5" result="blur" />
          <feMerge>
            <feMergeNode in="blur" />
            <feMergeNode in="SourceGraphic" />
          </feMerge>
        </filter>
      </defs>

      {/* Edges */}
      {EDGES.map((edge) => {
        const downstream = snapshot.by_stage[edge.to]
        const throughput = downstream?.recent_completed ?? 0
        const baseWidth = 1.5
        const width = Math.min(8, baseWidth + throughput * 0.4)
        const color = classColor(downstream?.queue)
        const opacity = throughput > 0 ? 0.5 : 0.15
        return (
          <path
            key={`${edge.from}-${edge.to}`}
            id={`edge-${edge.from}-${edge.to}`}
            d={edgePath(edge.from, edge.to)}
            fill="none"
            stroke={color}
            strokeWidth={width}
            strokeOpacity={opacity}
            strokeLinecap="round"
            style={{ transition: 'stroke-width 500ms, stroke-opacity 500ms' }}
          />
        )
      })}

      {/* Particles flowing along edges */}
      {particles.map((p) => (
        <circle key={p.id} r={3.5} fill={p.color}>
          <animateMotion dur={`${PARTICLE_DURATION_MS}ms`} repeatCount="1" fill="freeze">
            <mpath href={`#edge-${p.edgeKey}`} />
          </animateMotion>
          {/* Fade in then out so spawn / arrival are visually soft */}
          <animate
            attributeName="opacity"
            values="0;1;1;0"
            keyTimes="0;0.15;0.85;1"
            dur={`${PARTICLE_DURATION_MS}ms`}
            repeatCount="1"
            fill="freeze"
          />
        </circle>
      ))}

      {/* Nodes */}
      {Object.entries(NODE_POSITIONS).map(([stage, pos]) => {
        const info = snapshot.by_stage[stage]
        const r = nodeRadius(info)
        const color = classColor(info?.queue)
        const total = info ? info.queued + info.running : 0
        const isBusy = (info?.running ?? 0) > 0

        return (
          <g key={stage}>
            <circle
              cx={pos.x}
              cy={pos.y}
              r={r}
              fill={color}
              fillOpacity={isBusy ? 0.85 : 0.2}
              stroke={color}
              strokeWidth={isBusy ? 2 : 1}
              style={{
                filter: isBusy ? 'url(#pipeline-glow)' : undefined,
                transition: 'r 400ms ease-out, fill-opacity 400ms',
              }}
            />
            {/* Count badge above the node, only when there's activity */}
            {total > 0 && (
              <g>
                <rect x={pos.x - 16} y={pos.y - r - 14} width={32} height={13} rx={6.5} fill="rgba(0,0,0,0.55)" />
                <text
                  x={pos.x}
                  y={pos.y - r - 5}
                  textAnchor="middle"
                  fontSize={9}
                  fontWeight={600}
                  fill="white"
                  style={{ pointerEvents: 'none' }}
                >
                  {info!.running > 0 && info!.queued > 0
                    ? `${info!.running} • +${info!.queued}`
                    : info!.running > 0
                      ? `${info!.running}`
                      : `${info!.queued}`}
                </text>
              </g>
            )}
            {/* Stage label below the node */}
            <text
              x={pos.x}
              y={pos.y + r + 12}
              textAnchor="middle"
              fontSize={10}
              fontWeight={500}
              className="fill-(--color-text-primary)"
              style={{ pointerEvents: 'none' }}
            >
              {STAGE_LABELS[stage]}
            </text>
          </g>
        )
      })}
    </svg>
  )
}

export default function PipelineDiagram() {
  const { snapshot, error } = useQueueSnapshot()

  return (
    <div className="rounded-xl bg-white dark:bg-[#2c2c2e] shadow-mac ring-1 ring-(--color-border) p-4">
      <div className="mb-2 flex items-baseline justify-between">
        <h3 className="text-[13px] font-semibold text-(--color-text-primary)">Pipeline Overview</h3>
        <span className="text-[11px] text-(--color-text-secondary)">
          {snapshot ? `Throughput over last ${snapshot.throughput_window_seconds}s` : ''}
        </span>
      </div>
      {error && <div className="text-[12px] text-red-500/80">{error}</div>}
      {!snapshot && !error && <div className="text-[12px] text-(--color-text-secondary)">Loading…</div>}
      {snapshot && <PipelineGraph snapshot={snapshot} />}
      {snapshot && (
        <div className="mt-2 flex items-center gap-4 text-[10px] text-(--color-text-secondary)">
          <span className="inline-flex items-center gap-1">
            <span className="h-2 w-2 rounded-full" style={{ background: classColor('io') }} />
            IO
          </span>
          <span className="inline-flex items-center gap-1">
            <span className="h-2 w-2 rounded-full" style={{ background: classColor('cpu') }} />
            CPU
          </span>
          <span className="inline-flex items-center gap-1">
            <span className="h-2 w-2 rounded-full" style={{ background: classColor('llm') }} />
            LLM
          </span>
          <span className="ml-auto">Glowing nodes have running jobs · Particles = recent throughput</span>
        </div>
      )}
    </div>
  )
}

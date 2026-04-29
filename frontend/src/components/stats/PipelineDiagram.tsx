import { useEffect, useRef, useState } from 'react'
import { useQueueSnapshot, type QueueSnapshot, type StageSnapshot } from '../../hooks/useQueueSnapshot'
import { classColor } from '../../utils/queueColors'

/**
 * Pipeline Overview — animated DAG showing live activity in the
 * ingestion pipeline.
 *
 * Visual encoding:
 *   - Each stage is rendered as a thin outline circle whose radius
 *     scales with sqrt(queued + running) — a bottleneck swells
 *     without crushing other nodes.
 *   - Outline hue = queue class (IO blue / CPU amber / LLM purple).
 *   - When the stage has running jobs, an inner concentric "particle"
 *     appears — same hue as the outline, with a soft glow and a slow
 *     heartbeat pulse (radius + opacity). Idle stages are bare rings
 *     so the eye is drawn to active work.
 *   - Edges are a fixed green so the connecting "pipes" read as their
 *     own visual layer. Edges thicken and brighten with recent
 *     throughput on the downstream stage.
 *   - Particles flow along edges at a rate proportional to recent
 *     throughput. Each particle inherits the upstream stage's colour
 *     (coloured payload moving through green plumbing).
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
// Bumped from 300 → 320 to fit the wider fan-out spread without the
// top badge and bottom label hugging the edges.
const VIEW_HEIGHT = 320

// Connecting edges all use the same neutral colour so the "pipes" read
// as their own visual layer, separate from the queue-class hues on the
// nodes. System green reads as flow / activity without competing with
// the IO-blue / CPU-amber / LLM-purple node palette.
const EDGE_COLOR = '#30d158'

// Hand-laid-out positions for the 7-stage pipeline.
// Sequential prefix flows left-to-right at y=150, then chunk fans out
// to entities/embed/summarize stacked vertically, then re-converges
// at finalize. The fan-out spacing is 100 px (50 ↔ 150 ↔ 250) rather
// than the original 80 — with the active node radius cap at 26 plus
// the count badge above (~14 px) and stage label below (~12 px), 80
// wasn't enough vertical room and labels collided with neighbours'
// badges.
const NODE_POSITIONS: Record<string, { x: number; y: number }> = {
  extract: { x: 60, y: 160 },
  ocr: { x: 160, y: 160 },
  chunk: { x: 260, y: 160 },
  entities: { x: 390, y: 60 },
  embed: { x: 390, y: 160 },
  summarize: { x: 390, y: 260 },
  finalize: { x: 520, y: 160 },
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

      {/* Edges — fixed system green so the connecting "pipes" read as
          their own visual layer, separate from the nodes' queue-class
          hues. Particles flowing through them still inherit the
          upstream node's colour, which gives the impression of
          coloured payload moving through a green plumbing graph. */}
      {EDGES.map((edge) => {
        const downstream = snapshot.by_stage[edge.to]
        const throughput = downstream?.recent_completed ?? 0
        const baseWidth = 1.5
        const width = Math.min(8, baseWidth + throughput * 0.4)
        const opacity = throughput > 0 ? 0.55 : 0.2
        return (
          <path
            key={`${edge.from}-${edge.to}`}
            id={`edge-${edge.from}-${edge.to}`}
            d={edgePath(edge.from, edge.to)}
            fill="none"
            stroke={EDGE_COLOR}
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

        // Inner "particle" radius — proportional to the outer ring so
        // both grow together as the queue depth scales the node size.
        const innerR = r * 0.55
        // Heartbeat amplitude: small expansion + slight opacity dip,
        // ~1.6 s cycle for a calm 'breathing' rather than an urgent
        // alarm.
        const innerRMin = innerR * 0.86
        const innerRMax = innerR * 1.04
        return (
          <g key={stage}>
            {/* Outer ring — always rendered, same thin outline whether
                idle or busy. The activity signal is the inner particle,
                not a fill colour change. */}
            <circle
              cx={pos.x}
              cy={pos.y}
              r={r}
              fill="none"
              stroke={color}
              strokeWidth={1.2}
              strokeOpacity={isBusy ? 0.75 : 0.45}
              style={{ transition: 'r 400ms ease-out, stroke-opacity 400ms' }}
            />
            {/* Inner particle — only when busy. Concentric with the
                ring, glow filter applied, and a slow heartbeat
                (radius + opacity) so the eye reads "this stage is
                alive" without the diagram feeling frantic. */}
            {isBusy && (
              <circle cx={pos.x} cy={pos.y} fill={color} style={{ filter: 'url(#pipeline-glow)' }}>
                <animate
                  attributeName="r"
                  values={`${innerRMin};${innerRMax};${innerRMin}`}
                  dur="1.6s"
                  repeatCount="indefinite"
                  calcMode="spline"
                  keySplines="0.4 0 0.6 1; 0.4 0 0.6 1"
                />
                <animate
                  attributeName="opacity"
                  values="0.78;1;0.78"
                  dur="1.6s"
                  repeatCount="indefinite"
                  calcMode="spline"
                  keySplines="0.4 0 0.6 1; 0.4 0 0.6 1"
                />
              </circle>
            )}
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

/**
 * Animated pipeline diagram. Renders just the SVG + legend strip; the
 * caller is expected to provide its own layout container (e.g. a tab
 * panel on the Observatory page). `useQueueSnapshot` polls only while
 * this component is mounted, so unmounting the tab pauses polling
 * automatically.
 */
export default function PipelineDiagram() {
  const { snapshot, error } = useQueueSnapshot()

  if (error) return <div className="text-[12px] text-red-500/80">{error}</div>
  if (!snapshot) return <div className="text-[12px] text-(--color-text-secondary)">Loading…</div>

  return (
    <div>
      <PipelineGraph snapshot={snapshot} />
      <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[10px] text-(--color-text-secondary)">
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
        <span className="ml-auto">
          Throughput · last {snapshot.throughput_window_seconds}s · pulsing particles inside a node = running jobs
        </span>
      </div>
    </div>
  )
}

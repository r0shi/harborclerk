import { useEffect, useRef, useState } from 'react'
import { useQueueSnapshot, type QueueSnapshot, type StageSnapshot } from '../../hooks/useQueueSnapshot'
import { classColor } from '../../utils/queueColors'
import { useLLMStatusContext } from '../LLMStatusBanner'
import { type LLMState } from '../../hooks/useLLMStatus'
import SummarizingRow from '../queue-tray/SummarizingRow'

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

// Hand-laid-out positions for the 6-stage main flow.
// Sequential prefix flows left-to-right at y=130, then chunk fans out
// to entities/embed (two lanes at y=70 / y=190), then re-converges at
// finalize. Summarize runs separately as a BACKGROUND stage rendered
// in a card overlaying the lower-left empty quadrant — it is intentionally
// NOT part of the main flow so the diagram visually communicates "Ready
// is the terminus of the express line; summarize is independent."
const NODE_POSITIONS: Record<string, { x: number; y: number }> = {
  extract: { x: 60, y: 130 },
  ocr: { x: 160, y: 130 },
  chunk: { x: 260, y: 130 },
  entities: { x: 390, y: 70 },
  embed: { x: 390, y: 190 },
  finalize: { x: 520, y: 130 },
}

const EDGES: { from: string; to: string }[] = [
  { from: 'extract', to: 'ocr' },
  { from: 'ocr', to: 'chunk' },
  { from: 'chunk', to: 'entities' },
  { from: 'chunk', to: 'embed' },
  { from: 'entities', to: 'finalize' },
  { from: 'embed', to: 'finalize' },
]

const STAGE_LABELS: Record<string, string> = {
  extract: 'Extract',
  ocr: 'OCR',
  chunk: 'Chunk',
  entities: 'Entities',
  embed: 'Embed',
  finalize: 'Finalize',
}

function edgePath(from: string, to: string, rFrom: number, rTo: number): string {
  const a = NODE_POSITIONS[from]
  const b = NODE_POSITIONS[to]
  const dx = b.x - a.x
  // Cubic Bezier with horizontally-pulled control points → smooth
  // S-curve when y differs, near-straight when y matches. Because
  // the control points are pulled purely horizontally, the curve's
  // tangent at each endpoint is also purely horizontal — which is
  // why we can trim the endpoints by ±r along the x axis to land
  // them exactly on each node's perimeter without bending the curve.
  const sign = Math.sign(dx) || 1
  const startX = a.x + sign * rFrom
  const endX = b.x - sign * rTo
  const c1x = a.x + dx * 0.5
  const c1y = a.y
  const c2x = b.x - dx * 0.5
  const c2y = b.y
  return `M ${startX} ${a.y} C ${c1x} ${c1y}, ${c2x} ${c2y}, ${endX} ${b.y}`
}

function nodeRadius(info: StageSnapshot | undefined): number {
  if (!info) return 14
  const total = info.queued + info.running
  // sqrt scale so a long queue grows but doesn't overwhelm. Cap is
  // chosen so a busy node + its count badge above and stage label
  // below still fit cleanly in the 120 px vertical gap between the
  // entities (y=70) and embed (y=190) fan-out lanes.
  return Math.min(26, 14 + Math.sqrt(total) * 2.5)
}

interface Particle {
  id: string
  edgeKey: string
  color: string
}

const PARTICLE_DURATION_MS = 1100

const SUMMARIZE_COLOR = '#c4a4ff'

interface SummarizeCardProps {
  queued: number
  running: number
  summarizing: NonNullable<QueueSnapshot['summarizing']>
}

function modelLabel(
  state: LLMState,
  modelName: string | null,
  modelId: string | null,
): { text: string; muted: boolean; italic: boolean } {
  if (state === 'ready' && modelName) return { text: `Model · ${modelName}`, muted: false, italic: false }
  if (state === 'loading') {
    const label = modelName ?? modelId ?? 'loading…'
    return { text: `Model · ${label} (loading…)`, muted: true, italic: false }
  }
  if (state === 'deactivated') return { text: 'Model · not loaded', muted: true, italic: true }
  return { text: 'Model · …', muted: true, italic: false }
}

function SummarizeCircle({ queued, running }: { queued: number; running: number }) {
  // Same sqrt scale + heartbeat treatment as the main-flow nodes, in its
  // own 80×80 viewBox so the coordinate system stays decoupled.
  const total = queued + running
  const r = Math.min(26, 14 + Math.sqrt(total) * 2.5)
  const isBusy = running > 0
  const innerR = r * 0.55
  const innerRMin = innerR * 0.86
  const innerRMax = innerR * 1.04
  return (
    <svg viewBox="0 0 80 80" width={64} height={64} aria-hidden>
      <defs>
        <filter id="summarize-glow" x="-50%" y="-50%" width="200%" height="200%">
          <feGaussianBlur stdDeviation="2.5" result="blur" />
          <feMerge>
            <feMergeNode in="blur" />
            <feMergeNode in="SourceGraphic" />
          </feMerge>
        </filter>
      </defs>
      <circle
        cx={40}
        cy={40}
        r={r}
        fill="none"
        stroke={SUMMARIZE_COLOR}
        strokeWidth={1.2}
        strokeOpacity={isBusy ? 0.75 : 0.45}
        style={{ transition: 'r 400ms ease-out, stroke-opacity 400ms' }}
      />
      {isBusy && (
        <circle cx={40} cy={40} fill={SUMMARIZE_COLOR} style={{ filter: 'url(#summarize-glow)' }}>
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
    </svg>
  )
}

function SummarizeCard({ queued, running, summarizing }: SummarizeCardProps) {
  const { status } = useLLMStatusContext()
  const model = modelLabel(status.state, status.model_name, status.model_id)

  // Running first (most-advanced map step on top), then queued in FIFO.
  // Backend already orders by created_at; we resort here just for the
  // running-first split.
  const sortedItems = [...summarizing].sort((a, b) => {
    if (a.status !== b.status) return a.status === 'running' ? -1 : 1
    if (a.status === 'running') return b.progress_current - a.progress_current
    return 0
  })
  const visible = sortedItems.slice(0, 4)
  const overflow = sortedItems.length - visible.length
  const hasItems = sortedItems.length > 0

  return (
    <div
      className="absolute bottom-0 left-0 w-[48%] rounded-xl p-3"
      style={{
        background: 'rgba(186,140,255,0.06)',
        border: '1px solid rgba(186,140,255,0.4)',
      }}
    >
      <div className="text-[9px] font-semibold tracking-wider" style={{ color: SUMMARIZE_COLOR }}>
        BACKGROUND
      </div>
      <div className="mt-1 flex gap-3">
        <div className="flex flex-col items-center" style={{ flex: '0 0 72px' }}>
          <SummarizeCircle queued={queued} running={running} />
          <div className="mt-1 text-[10px] font-medium" style={{ color: SUMMARIZE_COLOR }}>
            Summarize
          </div>
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-baseline justify-between gap-2 text-[11px]">
            <span
              className="truncate"
              style={{
                color: SUMMARIZE_COLOR,
                opacity: model.muted ? 0.7 : 1,
                fontStyle: model.italic ? 'italic' : 'normal',
              }}
            >
              {model.text}
            </span>
            <span className="tabular-nums text-[10px] text-(--color-text-secondary)" style={{ flex: '0 0 auto' }}>
              {queued} queued · {running} running
            </span>
          </div>
          {hasItems && (
            <>
              <div className="mt-1.5 mb-1 border-t" style={{ borderColor: 'rgba(186,140,255,0.25)' }} />
              <div className="space-y-0">
                {visible.map((item) => (
                  <SummarizingRow key={item.doc_id} item={item} />
                ))}
                {overflow > 0 && (
                  <div className="pt-0.5 text-[10px] text-(--color-text-secondary)">+ {overflow} more</div>
                )}
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  )
}

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
    <div className="relative">
      <svg
        viewBox={`0 0 ${VIEW_WIDTH} ${VIEW_HEIGHT}`}
        className="w-full"
        style={{ aspectRatio: `${VIEW_WIDTH} / ${VIEW_HEIGHT}` }}
      >
        <defs>
          {/* Glow filter for active nodes — dialled back to suit the
            smaller node size; bigger blur made tightly-stacked nodes
            (entities / embed) bleed into each other. */}
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
          coloured payload moving through a green plumbing graph.
          Endpoints are trimmed to each node's current perimeter so
          the line stops at the ring rather than passing through the
          disc. */}
        {EDGES.map((edge) => {
          const downstream = snapshot.by_stage[edge.to]
          const throughput = downstream?.recent_completed ?? 0
          const baseWidth = 1.5
          const width = Math.min(8, baseWidth + throughput * 0.4)
          const opacity = throughput > 0 ? 0.55 : 0.2
          const rFrom = nodeRadius(snapshot.by_stage[edge.from])
          const rTo = nodeRadius(snapshot.by_stage[edge.to])
          return (
            <path
              key={`${edge.from}-${edge.to}`}
              id={`edge-${edge.from}-${edge.to}`}
              d={edgePath(edge.from, edge.to, rFrom, rTo)}
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

        {/* "Ready" badge above finalize — drives home that finalize is
          the terminus of the express line. Summarize is rendered below
          the SVG as its own BACKGROUND stage. */}
        <text x={520} y={105} textAnchor="middle" fontSize={10} fill="#30d158" fontWeight={600}>
          Ready
        </text>
      </svg>
      <SummarizeCard
        queued={snapshot.queues.llm?.queued ?? 0}
        running={snapshot.queues.llm?.running ?? 0}
        summarizing={snapshot.summarizing ?? []}
      />
    </div>
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

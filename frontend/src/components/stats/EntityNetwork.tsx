import { useCallback, useEffect, useRef, useState } from 'react'
import { get } from '../../api'
import {
  forceSimulation,
  forceLink,
  forceManyBody,
  forceCenter,
  forceCollide,
  type SimulationNodeDatum,
  type SimulationLinkDatum,
} from 'd3-force'
import { scaleLinear } from 'd3-scale'
import { drag as d3Drag } from 'd3-drag'
import { select } from 'd3-selection'
import { InfoTip } from '../InfoTip'
import { ENTITY_COLORS, ENTITY_TYPE_LABELS } from './CorpusCharts'

interface EntityNode extends SimulationNodeDatum {
  id: string
  text: string
  type: string
  mentions: number
}

interface EntityEdge extends SimulationLinkDatum<EntityNode> {
  weight: number
}

interface NetworkData {
  nodes: { id: string; text: string; type: string; mentions: number }[]
  edges: { source: string; target: string; weight: number }[]
}

function typeColor(type: string): string {
  return ENTITY_COLORS[type] || '#98989d'
}

export default function EntityNetwork() {
  const svgRef = useRef<SVGSVGElement>(null)
  const simRef = useRef<ReturnType<typeof forceSimulation<EntityNode>> | null>(null)
  const [limit, setLimit] = useState(50)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [data, setData] = useState<NetworkData | null>(null)
  const [hoveredNode, setHoveredNode] = useState<EntityNode | null>(null)
  const [selectedNode, setSelectedNode] = useState<string | null>(null)
  const [dimensions, setDimensions] = useState({ width: 800, height: 500 })
  const containerRef = useRef<HTMLDivElement>(null)
  // Store selected node in a ref so the D3 tick function can read it without re-creating the simulation
  const selectedNodeRef = useRef<string | null>(null)

  // Responsive sizing
  useEffect(() => {
    const container = containerRef.current
    if (!container) return
    const obs = new ResizeObserver((entries) => {
      const { width } = entries[0].contentRect
      setDimensions({ width: Math.max(400, width), height: Math.max(400, Math.min(600, width * 0.6)) })
    })
    obs.observe(container)
    return () => obs.disconnect()
  }, [])

  // Fetch data
  useEffect(() => {
    let cancelled = false
    const fetchData = async () => {
      try {
        const d = await get<NetworkData>(`/api/stats/entity-network?limit=${limit}`)
        if (!cancelled) {
          setData(d)
          setError(null)
        }
      } catch (e: unknown) {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Failed to load')
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    fetchData()
    return () => {
      cancelled = true
    }
  }, [limit])

  // D3 simulation
  const renderGraph = useCallback(() => {
    if (!data || !svgRef.current) return
    const { width, height } = dimensions

    // Clean up previous simulation
    if (simRef.current) {
      simRef.current.stop()
      simRef.current = null
    }

    // Clear stale selection when graph is re-rendered (e.g. data or dimensions changed)
    selectedNodeRef.current = null
    setSelectedNode(null)

    const nodes: EntityNode[] = data.nodes.map((n) => ({ ...n }))
    const edges: EntityEdge[] = data.edges.map((e) => ({ ...e }))

    if (nodes.length === 0) return

    const mentionExtent = [Math.min(...nodes.map((n) => n.mentions)), Math.max(...nodes.map((n) => n.mentions))]
    const radiusScale = scaleLinear().domain(mentionExtent).range([5, 20]).clamp(true)

    const sim = forceSimulation<EntityNode>(nodes)
      .force(
        'link',
        forceLink<EntityNode, EntityEdge>(edges)
          .id((d) => d.id)
          .distance(80)
          .strength(0.3),
      )
      .force('charge', forceManyBody().strength(-120))
      .force('center', forceCenter(width / 2, height / 2))
      .force(
        'collide',
        forceCollide<EntityNode>().radius((d) => radiusScale(d.mentions) + 4),
      )

    simRef.current = sim

    const svg = select(svgRef.current)

    // Clear existing
    svg.selectAll('*').remove()

    // Background rect for click-to-deselect
    svg
      .append('rect')
      .attr('width', width)
      .attr('height', height)
      .attr('fill', 'transparent')
      .on('click', () => {
        selectedNodeRef.current = null
        setSelectedNode(null)
        applyHighlight()
      })

    const g = svg.append('g')

    // Build adjacency set for neighbor lookup
    const neighborSet = new Set<string>()
    function addNeighborKey(a: string, b: string) {
      neighborSet.add(`${a}|${b}`)
      neighborSet.add(`${b}|${a}`)
    }
    edges.forEach((e) => {
      const src = typeof e.source === 'object' ? (e.source as EntityNode).id : (e.source as string)
      const tgt = typeof e.target === 'object' ? (e.target as EntityNode).id : (e.target as string)
      addNeighborKey(src, tgt)
    })
    function isNeighbor(a: string, b: string) {
      return neighborSet.has(`${a}|${b}`)
    }

    // Edges
    const link = g
      .append('g')
      .selectAll('line')
      .data(edges)
      .join('line')
      .attr('stroke', 'var(--color-text-secondary)')
      .attr('stroke-opacity', 0.2)
      .attr('stroke-width', (d: EntityEdge) => Math.min(4, Math.max(1, d.weight / 3)))

    // Track drag to suppress click after drag
    let wasDragged = false

    // Nodes
    const node = g
      .append('g')
      .selectAll<SVGCircleElement, EntityNode>('circle')
      .data(nodes)
      .join('circle')
      .attr('r', (d: EntityNode) => radiusScale(d.mentions))
      .attr('fill', (d: EntityNode) => typeColor(d.type))
      .attr('stroke', 'var(--color-bg-primary)')
      .attr('stroke-width', 1.5)
      .style('cursor', 'pointer')
      .on('mouseenter', (_event: MouseEvent, d: EntityNode) => setHoveredNode(d))
      .on('mouseleave', () => setHoveredNode(null))
      .on('click', (_event: MouseEvent, d: EntityNode) => {
        if (wasDragged) {
          wasDragged = false
          return
        }
        _event.stopPropagation()
        const newSelection = selectedNodeRef.current === d.id ? null : d.id
        selectedNodeRef.current = newSelection
        setSelectedNode(newSelection)
        applyHighlight()
      })

    // Labels for larger nodes
    const label = g
      .append('g')
      .selectAll('text')
      .data(nodes.filter((n) => radiusScale(n.mentions) > 10))
      .join('text')
      .text((d: EntityNode) => (d.text.length > 12 ? d.text.slice(0, 10) + '...' : d.text))
      .attr('text-anchor', 'middle')
      .attr('dy', (d: EntityNode) => radiusScale(d.mentions) + 12)
      .attr('font-size', 10)
      .attr('fill', 'var(--color-text-secondary)')
      .attr('pointer-events', 'none')

    // Apply highlight based on selectedNodeRef
    function applyHighlight() {
      const sel = selectedNodeRef.current
      if (!sel) {
        // Reset to normal
        node.attr('opacity', 1).attr('r', (d: EntityNode) => radiusScale(d.mentions))
        link.attr('stroke-opacity', 0.2).attr('stroke-width', (d: EntityEdge) => Math.min(4, Math.max(1, d.weight / 3)))
        label.attr('opacity', 1)
      } else {
        node
          .attr('opacity', (d: EntityNode) => {
            if (d.id === sel) return 1
            if (isNeighbor(d.id, sel)) return 1
            return 0.15
          })
          .attr('r', (d: EntityNode) => {
            const base = radiusScale(d.mentions)
            return d.id === sel ? base + 2 : base
          })

        link
          .attr('stroke-opacity', (d: EntityEdge) => {
            const src = (d.source as EntityNode).id
            const tgt = (d.target as EntityNode).id
            return src === sel || tgt === sel ? 0.6 : 0.08
          })
          .attr('stroke-width', (d: EntityEdge) => {
            const src = (d.source as EntityNode).id
            const tgt = (d.target as EntityNode).id
            const base = Math.min(4, Math.max(1, d.weight / 3))
            return src === sel || tgt === sel ? base + 1 : base
          })

        label.attr('opacity', (d: EntityNode) => {
          if (d.id === sel) return 1
          if (isNeighbor(d.id, sel)) return 1
          return 0.15
        })
      }
    }

    // Drag behavior
    const dragBehavior = d3Drag<SVGCircleElement, EntityNode>()
      .on('start', (event, d) => {
        wasDragged = false
        if (!event.active) sim.alphaTarget(0.3).restart()
        d.fx = d.x
        d.fy = d.y
      })
      .on('drag', (event, d) => {
        wasDragged = true
        d.fx = event.x
        d.fy = event.y
      })
      .on('end', (event, d) => {
        if (!event.active) sim.alphaTarget(0)
        d.fx = null
        d.fy = null
      })

    node.call(dragBehavior)

    sim.on('tick', () => {
      link
        .attr('x1', (d: EntityEdge) => (d.source as EntityNode).x!)
        .attr('y1', (d: EntityEdge) => (d.source as EntityNode).y!)
        .attr('x2', (d: EntityEdge) => (d.target as EntityNode).x!)
        .attr('y2', (d: EntityEdge) => (d.target as EntityNode).y!)

      node.attr('cx', (d: EntityNode) => d.x!).attr('cy', (d: EntityNode) => d.y!)

      label.attr('x', (d: EntityNode) => d.x!).attr('y', (d: EntityNode) => d.y!)
    })

    return () => {
      sim.stop()
    }
  }, [data, dimensions])

  useEffect(() => {
    renderGraph()
    return () => {
      if (simRef.current) {
        simRef.current.stop()
        simRef.current = null
      }
    }
  }, [renderGraph])

  const types = data ? [...new Set(data.nodes.map((n) => n.type))] : []

  return (
    <div ref={containerRef} className="rounded-xl bg-white dark:bg-[#2c2c2e] shadow-mac p-4">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="text-[13px] font-semibold text-(--color-text-primary)">
          Entity Network
          <InfoTip text="This network shows how entities appear together in your documents. Lines connect entities that are mentioned in the same text segments. Thicker lines mean they co-occur more often. Click a node to highlight its connections." />
        </h3>
        <div className="flex items-center gap-2">
          <label className="text-[11px] text-(--color-text-secondary)">Entities:</label>
          <input
            type="range"
            min={20}
            max={100}
            step={10}
            value={limit}
            onChange={(e) => setLimit(Number(e.target.value))}
            className="h-1 w-24 accent-(--color-accent)"
          />
          <span className="min-w-[2ch] text-[11px] text-(--color-text-secondary)">{limit}</span>
        </div>
      </div>

      {/* Legend */}
      {types.length > 0 && (
        <div className="mb-2 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-(--color-text-secondary)">
          {types.map((type) => (
            <span key={type} className="flex items-center gap-1">
              <span className="inline-block h-2 w-2 rounded-full" style={{ backgroundColor: typeColor(type) }} />
              <span title={ENTITY_TYPE_LABELS[type] ?? type}>{type}</span>
            </span>
          ))}
        </div>
      )}

      <div className="relative overflow-hidden rounded-lg bg-(--color-bg-secondary)">
        {loading && (
          <div className="absolute inset-0 z-10 flex items-center justify-center bg-(--color-bg-secondary)/80">
            <p className="text-sm text-(--color-text-secondary)">Loading...</p>
          </div>
        )}
        {error && (
          <div className="flex h-[300px] items-center justify-center">
            <p className="text-sm text-red-500">{error}</p>
          </div>
        )}
        {!error && data?.nodes.length === 0 && !loading && (
          <div className="flex h-[300px] items-center justify-center">
            <p className="text-sm text-(--color-text-secondary)">No entity data available</p>
          </div>
        )}
        {!error && (data?.nodes.length ?? 0) > 0 && (
          <svg ref={svgRef} width={dimensions.width} height={dimensions.height} />
        )}

        {/* Hover tooltip */}
        {hoveredNode && (
          <div className="pointer-events-none absolute left-3 top-3 rounded-lg bg-white dark:bg-[#3a3a3c] shadow-mac-lg px-3 py-1.5 text-[12px] text-(--color-text-primary) ring-1 ring-(--color-border)">
            <span className="font-medium">{hoveredNode.text}</span>
            <span className="ml-1 text-(--color-text-secondary)">
              ({ENTITY_TYPE_LABELS[hoveredNode.type] ?? hoveredNode.type}) — {hoveredNode.mentions.toLocaleString()}{' '}
              mentions
            </span>
          </div>
        )}

        {/* Selected node indicator */}
        {selectedNode && (
          <div className="absolute right-3 top-3 rounded-md bg-white/90 dark:bg-[#3a3a3c]/90 px-2 py-1 text-[11px] text-(--color-text-secondary)">
            Click node or background to deselect
          </div>
        )}
      </div>
    </div>
  )
}

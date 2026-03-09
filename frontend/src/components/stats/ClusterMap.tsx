import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { get } from '../../api'
import { UMAP } from 'umap-js'

interface ClusterDoc {
  doc_id: string
  title: string
  mime_type: string
  centroid: number[]
}

interface ClusterData {
  documents: ClusterDoc[]
}

const MIME_COLORS: Record<string, string> = {
  'application/pdf': '#ff3b30',
  'image/jpeg': '#ff9500',
  'image/png': '#ffcc00',
  'image/tiff': '#ff9500',
  'text/plain': '#34c759',
  'text/csv': '#30d158',
  'text/markdown': '#5ac8fa',
  'text/html': '#007aff',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document': '#af52de',
  'application/msword': '#af52de',
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': '#64d2ff',
  'application/epub+zip': '#ff2d55',
  'message/rfc822': '#5856d6',
}

function mimeColor(mime: string): string {
  return MIME_COLORS[mime] || '#98989d'
}

function shortMime(mime: string): string {
  const map: Record<string, string> = {
    'application/pdf': 'PDF',
    'image/jpeg': 'JPEG',
    'image/png': 'PNG',
    'text/plain': 'TXT',
    'text/csv': 'CSV',
    'text/markdown': 'MD',
    'text/html': 'HTML',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'DOCX',
    'application/msword': 'DOC',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 'XLSX',
    'application/epub+zip': 'EPUB',
    'message/rfc822': 'EML',
  }
  return map[mime] || mime.split('/').pop()?.toUpperCase() || mime
}

export default function ClusterMap() {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const containerRef = useRef<HTMLDivElement>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [docs, setDocs] = useState<ClusterDoc[]>([])
  const [positions, setPositions] = useState<[number, number][]>([])
  const [hoveredIdx, setHoveredIdx] = useState<number | null>(null)
  const [dimensions, setDimensions] = useState({ width: 800, height: 500 })
  const navigate = useNavigate()

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

  // Fetch + UMAP
  useEffect(() => {
    let cancelled = false
    const fetchAndProject = async () => {
      try {
        const data = await get<ClusterData>('/api/stats/clusters')
        if (cancelled) return
        const documents = data.documents
        if (documents.length < 2) {
          setDocs(documents)
          setPositions(documents.map(() => [0.5, 0.5]))
          return
        }

        const centroids = documents.map((d) => d.centroid)
        const nNeighbors = Math.min(15, Math.max(2, Math.floor(documents.length / 3)))
        const umap = new UMAP({ nComponents: 2, nNeighbors, minDist: 0.1, spread: 1.0 })
        const embedding = umap.fit(centroids) as number[][]

        // Normalize to 0..1
        const xs = embedding.map((p) => p[0])
        const ys = embedding.map((p) => p[1])
        const xMin = Math.min(...xs)
        const xMax = Math.max(...xs)
        const yMin = Math.min(...ys)
        const yMax = Math.max(...ys)
        const xRange = xMax - xMin || 1
        const yRange = yMax - yMin || 1

        const normalized = embedding.map((p) => [(p[0] - xMin) / xRange, (p[1] - yMin) / yRange] as [number, number])

        setDocs(documents)
        setPositions(normalized)
        setError(null)
      } catch (e: unknown) {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Failed to load')
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    fetchAndProject()
    return () => {
      cancelled = true
    }
  }, [])

  // Draw canvas
  const draw = useCallback(() => {
    const canvas = canvasRef.current
    if (!canvas || docs.length === 0 || positions.length === 0) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    const { width, height } = dimensions
    const dpr = window.devicePixelRatio || 1
    canvas.width = width * dpr
    canvas.height = height * dpr
    canvas.style.width = `${width}px`
    canvas.style.height = `${height}px`
    ctx.scale(dpr, dpr)

    const pad = 40

    ctx.clearRect(0, 0, width, height)

    // Draw dots
    docs.forEach((doc, i) => {
      const [nx, ny] = positions[i]
      const x = pad + nx * (width - 2 * pad)
      const y = pad + ny * (height - 2 * pad)
      const r = hoveredIdx === i ? 7 : 5

      ctx.beginPath()
      ctx.arc(x, y, r, 0, Math.PI * 2)
      ctx.fillStyle = mimeColor(doc.mime_type)
      ctx.fill()

      if (hoveredIdx === i) {
        ctx.strokeStyle = 'var(--color-text-primary)'
        ctx.lineWidth = 2
        ctx.stroke()
      }
    })
  }, [docs, positions, dimensions, hoveredIdx])

  useEffect(() => {
    draw()
  }, [draw])

  // Mouse interaction
  const handleMouseMove = useCallback(
    (e: React.MouseEvent<HTMLCanvasElement>) => {
      const canvas = canvasRef.current
      if (!canvas || docs.length === 0) return

      const rect = canvas.getBoundingClientRect()
      const mx = e.clientX - rect.left
      const my = e.clientY - rect.top
      const { width, height } = dimensions
      const pad = 40

      let closest = -1
      let closestDist = Infinity

      docs.forEach((_doc, i) => {
        const [nx, ny] = positions[i]
        const x = pad + nx * (width - 2 * pad)
        const y = pad + ny * (height - 2 * pad)
        const dist = Math.hypot(mx - x, my - y)
        if (dist < 15 && dist < closestDist) {
          closest = i
          closestDist = dist
        }
      })

      setHoveredIdx(closest >= 0 ? closest : null)
      canvas.style.cursor = closest >= 0 ? 'pointer' : 'default'
    },
    [docs, positions, dimensions],
  )

  const handleClick = useCallback(() => {
    if (hoveredIdx !== null && docs[hoveredIdx]) {
      navigate(`/docs/${docs[hoveredIdx].doc_id}`)
    }
  }, [hoveredIdx, docs, navigate])

  const mimeTypes = [...new Set(docs.map((d) => d.mime_type))]

  return (
    <div ref={containerRef} className="rounded-xl bg-white dark:bg-[#2c2c2e] shadow-mac ring-1 ring-(--color-border) p-4">
      <h3 className="mb-3 text-[13px] font-semibold text-(--color-text-primary)">Document Clusters</h3>

      {/* Legend */}
      {mimeTypes.length > 0 && (
        <div className="mb-2 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-(--color-text-secondary)">
          {mimeTypes.map((mime) => (
            <span key={mime} className="flex items-center gap-1">
              <span className="inline-block h-2 w-2 rounded-full" style={{ backgroundColor: mimeColor(mime) }} />
              {shortMime(mime)}
            </span>
          ))}
        </div>
      )}

      <div className="relative overflow-hidden rounded-lg bg-(--color-bg-secondary)">
        {loading && (
          <div className="absolute inset-0 z-10 flex items-center justify-center bg-(--color-bg-secondary)/80">
            <p className="text-sm text-(--color-text-secondary)">Computing clusters...</p>
          </div>
        )}
        {error && (
          <div className="flex h-[300px] items-center justify-center">
            <p className="text-sm text-red-500">{error}</p>
          </div>
        )}
        {!error && docs.length === 0 && !loading && (
          <div className="flex h-[300px] items-center justify-center">
            <p className="text-sm text-(--color-text-secondary)">Not enough documents for clustering</p>
          </div>
        )}
        {!error && docs.length > 0 && (
          <canvas
            ref={canvasRef}
            onMouseMove={handleMouseMove}
            onMouseLeave={() => setHoveredIdx(null)}
            onClick={handleClick}
          />
        )}

        {/* Hover tooltip */}
        {hoveredIdx !== null && docs[hoveredIdx] && (
          <div className="pointer-events-none absolute left-3 top-3 max-w-xs rounded-lg bg-white dark:bg-[#3a3a3c] shadow-mac-lg px-3 py-1.5 text-[12px] text-(--color-text-primary) ring-1 ring-(--color-border)">
            <p className="font-medium">{docs[hoveredIdx].title}</p>
            <p className="text-(--color-text-secondary)">{shortMime(docs[hoveredIdx].mime_type)}</p>
          </div>
        )}
      </div>
    </div>
  )
}

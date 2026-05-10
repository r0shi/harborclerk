import { useEffect, useState } from 'react'
import { useAuth } from '../../auth'

interface BacklogResponse {
  queue_depth: number
  throughput_per_min: number
  p50_seconds: number
  depth_history: [number, number][]
}

function formatSeconds(s: number): string {
  if (s < 1) return '<1s'
  if (s < 60) return `${Math.round(s)}s`
  return `${(s / 60).toFixed(1)}m`
}

function trendLabel(history: [number, number][]): string {
  if (history.length < 4) return ''
  const quarter = Math.max(1, Math.floor(history.length / 4))
  const first = history.slice(0, quarter).reduce((a, [, d]) => a + d, 0) / quarter
  const last = history.slice(-quarter).reduce((a, [, d]) => a + d, 0) / quarter
  if (last < first - 1) return '↘ catching up'
  if (last > first + 1) return '↗ falling behind'
  return '→ steady'
}

function Sparkline({ history }: { history: [number, number][] }) {
  if (history.length < 2) return null
  const values = history.map(([, d]) => d)
  const max = Math.max(...values, 1)
  const points = history
    .map(([, d], i) => {
      const x = (i / (history.length - 1)) * 200
      const y = 30 - (d / max) * 28
      return `${x},${y}`
    })
    .join(' L ')
  return (
    <svg viewBox="0 0 200 30" preserveAspectRatio="none" className="w-full" style={{ height: 28 }}>
      <path d={`M ${points} L 200,30 L 0,30 Z`} fill="rgba(186,140,255,0.12)" />
      <path
        d={`M ${points}`}
        fill="none"
        stroke="#c4a4ff"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}

/**
 * Summary Backlog widget for the Observatory page. Three slots:
 * Throughput · p50 · composite (In Queue + sparkline).
 *
 * Polls /api/system/summary-backlog every 30 seconds.
 */
export default function SummaryBacklogPanel() {
  const { token } = useAuth()
  const [data, setData] = useState<BacklogResponse | null>(null)

  useEffect(() => {
    if (!token) return
    let cancelled = false
    const fetchData = async () => {
      try {
        const res = await fetch('/api/system/summary-backlog', {
          headers: { Authorization: `Bearer ${token}` },
        })
        if (!res.ok) return
        const json = (await res.json()) as BacklogResponse
        if (!cancelled) setData(json)
      } catch {
        // silently retry on next interval
      }
    }
    fetchData()
    const interval = setInterval(fetchData, 30_000)
    return () => {
      cancelled = true
      clearInterval(interval)
    }
  }, [token])

  if (!data) return null

  return (
    <div>
      <h3 className="text-[13px] mb-3" style={{ color: '#c4a4ff' }}>
        Summary Backlog
      </h3>
      <div className="grid items-stretch gap-6" style={{ gridTemplateColumns: 'auto auto 1fr' }}>
        {/* Throughput */}
        <div>
          <div className="text-[28px] font-semibold leading-none tabular-nums" style={{ color: '#c4a4ff' }}>
            {data.throughput_per_min.toFixed(1)}
            <span className="text-[13px] text-(--color-text-secondary) ml-0.5">/min</span>
          </div>
          <div className="text-[10px] uppercase tracking-wider mt-1.5 text-(--color-text-secondary)">Throughput</div>
        </div>
        {/* p50 */}
        <div>
          <div className="text-[28px] font-semibold leading-none tabular-nums" style={{ color: '#c4a4ff' }}>
            {formatSeconds(data.p50_seconds)}
          </div>
          <div className="text-[10px] uppercase tracking-wider mt-1.5 text-(--color-text-secondary)">p50</div>
        </div>
        {/* In Queue (composite: number + sparkline) */}
        <div
          className="rounded-lg p-2.5 grid gap-3 items-stretch"
          style={{
            gridTemplateColumns: 'auto 1fr',
            background: 'rgba(186,140,255,0.06)',
            border: '1px solid rgba(186,140,255,0.18)',
          }}
        >
          <div className="pr-3 flex flex-col justify-center" style={{ borderRight: '1px solid rgba(186,140,255,0.2)' }}>
            <div className="text-[28px] font-semibold leading-none tabular-nums" style={{ color: '#c4a4ff' }}>
              {data.queue_depth}
            </div>
            <div className="text-[10px] uppercase tracking-wider mt-1.5 text-(--color-text-secondary)">In Queue</div>
          </div>
          <div className="flex flex-col justify-between min-w-0">
            <div className="text-[10px] text-(--color-text-secondary)">
              queue depth · last hour {trendLabel(data.depth_history)}
            </div>
            <Sparkline history={data.depth_history} />
          </div>
        </div>
      </div>
    </div>
  )
}

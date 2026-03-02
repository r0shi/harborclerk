import { useEffect, useState } from 'react'
import { get } from '../api'
import CorpusCharts from '../components/stats/CorpusCharts'
import EntityNetwork from '../components/stats/EntityNetwork'
import ClusterMap from '../components/stats/ClusterMap'

interface CorpusStats {
  document_count: number
  total_chunks: number
  total_pages: number
  languages: Record<string, number>
  mime_types: Record<string, number>
  ocr_breakdown: { born_digital: number; ocr_used: number; unknown: number }
  size_buckets: { label: string; count: number }[]
  growth_timeline: { month: string; count: number }[]
  pipeline_timing: Record<string, { avg_secs: number; count: number }>
  entity_type_counts: Record<string, number>
  top_entities: { text: string; type: string; mentions: number }[]
}

function StatBadge({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="rounded-xl bg-white dark:bg-[#2c2c2e] shadow-mac px-4 py-3">
      <p className="text-[11px] font-medium text-(--color-text-secondary) uppercase tracking-wide">{label}</p>
      <p className="mt-0.5 text-xl font-semibold text-(--color-text-primary) tabular-nums">
        {typeof value === 'number' ? value.toLocaleString() : value}
      </p>
    </div>
  )
}

export default function StatsPage() {
  const [stats, setStats] = useState<CorpusStats | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    get<CorpusStats>('/api/stats')
      .then((d) => {
        if (!cancelled) setStats(d)
      })
      .catch((e) => {
        if (!cancelled) setError(e.message || 'Failed to load stats')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [])

  if (loading) {
    return (
      <div className="flex min-h-[300px] items-center justify-center">
        <p className="text-sm text-(--color-text-secondary)">Loading statistics...</p>
      </div>
    )
  }

  if (error) {
    return (
      <div className="flex min-h-[300px] items-center justify-center">
        <p className="text-sm text-red-500">{error}</p>
      </div>
    )
  }

  if (!stats) return null

  return (
    <div className="space-y-6">
      <h1 className="text-lg font-semibold text-(--color-text-primary)">Corpus Statistics</h1>

      {/* Summary badges */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <StatBadge label="Documents" value={stats.document_count} />
        <StatBadge label="Chunks" value={stats.total_chunks} />
        <StatBadge label="Pages" value={stats.total_pages} />
        <StatBadge label="Entities" value={Object.values(stats.entity_type_counts).reduce((a, b) => a + b, 0)} />
      </div>

      {/* Charts */}
      <CorpusCharts stats={stats} />

      {/* Entity Network */}
      <EntityNetwork />

      {/* Document Clusters */}
      <ClusterMap />
    </div>
  )
}

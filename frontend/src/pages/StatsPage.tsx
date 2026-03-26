import { useEffect, useState } from 'react'
import { get } from '../api'
import CorpusCharts from '../components/stats/CorpusCharts'
import EntityNetwork from '../components/stats/EntityNetwork'
import ClusterMap from '../components/stats/ClusterMap'
import TopicTreemap from '../components/stats/TopicTreemap'
import TopicBarChart from '../components/stats/TopicBarChart'
import TopicKeywords from '../components/stats/TopicKeywords'
import { InfoTip } from '../components/InfoTip'

interface TopicCluster {
  cluster_id: number
  name: string
  keywords: string[]
  doc_count: number
  representative_doc_ids: string[]
}

interface TopicsData {
  clusters: TopicCluster[]
  doc_count: number
}

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

function StatBadge({ label, value, tip }: { label: string; value: string | number; tip?: string }) {
  return (
    <div className="rounded-xl bg-white dark:bg-[#2c2c2e] shadow-mac ring-1 ring-(--color-border) px-4 py-3">
      <p className="text-[11px] font-medium text-(--color-text-secondary) uppercase tracking-wide">
        {label}
        {tip && <InfoTip text={tip} />}
      </p>
      <p className="mt-0.5 text-xl font-semibold text-(--color-text-primary) tabular-nums">
        {typeof value === 'number' ? value.toLocaleString() : value}
      </p>
    </div>
  )
}

export default function StatsPage() {
  const [stats, setStats] = useState<CorpusStats | null>(null)
  const [topics, setTopics] = useState<TopicsData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    const fetchStats = get<CorpusStats>('/api/stats').then((d) => {
      if (!cancelled) setStats(d)
    })
    const fetchTopics = get<TopicsData>('/api/stats/topics')
      .then((d) => {
        if (!cancelled) setTopics(d)
      })
      .catch(() => {
        /* topics are optional */
      })
    Promise.all([fetchStats, fetchTopics])
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
      <h1 className="text-lg font-semibold text-(--color-text-primary)">
        Corpus Statistics
        <InfoTip text="These are facts and statistics about your entire document collection — how many documents, pages, and text segments (chunks) have been processed." />
      </h1>

      {/* Summary badges */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <StatBadge label="Documents" value={stats.document_count} />
        <StatBadge
          label="Chunks"
          value={stats.total_chunks}
          tip="Your documents are split into overlapping text segments called chunks (~1,000 characters each) for search and analysis."
        />
        <StatBadge
          label="Pages"
          value={stats.total_pages}
          tip="Total pages across all documents. PDFs use their real page breaks; plain text files get synthetic pages at regular intervals."
        />
        <StatBadge
          label="Entities"
          value={Object.values(stats.entity_type_counts).reduce((a, b) => a + b, 0)}
          tip="Named entities — people, organizations, places, dates, etc. — automatically extracted from your documents using natural language processing."
        />
      </div>

      {/* Charts */}
      <CorpusCharts stats={stats} />

      {/* Topics */}
      {topics && topics.clusters.length > 0 && (
        <div className="space-y-4">
          <h2 className="text-[15px] font-semibold text-(--color-text-primary)">Topics</h2>
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            <div className="rounded-xl bg-white dark:bg-[#2c2c2e] shadow-mac ring-1 ring-(--color-border) p-4">
              <h3 className="mb-3 text-[13px] font-semibold text-(--color-text-primary)">Topic Distribution</h3>
              <TopicTreemap topics={topics.clusters} />
            </div>
            <div className="rounded-xl bg-white dark:bg-[#2c2c2e] shadow-mac ring-1 ring-(--color-border) p-4">
              <h3 className="mb-3 text-[13px] font-semibold text-(--color-text-primary)">Documents by Topic</h3>
              <TopicBarChart topics={topics.clusters} />
            </div>
          </div>
          <div className="rounded-xl bg-white dark:bg-[#2c2c2e] shadow-mac ring-1 ring-(--color-border) p-4">
            <h3 className="mb-3 text-[13px] font-semibold text-(--color-text-primary)">Topic Keywords</h3>
            <div className="max-h-80 overflow-y-auto">
              <TopicKeywords topics={topics.clusters} />
            </div>
          </div>
        </div>
      )}

      {/* Entity Network */}
      <EntityNetwork />

      {/* Document Clusters */}
      <ClusterMap />
    </div>
  )
}

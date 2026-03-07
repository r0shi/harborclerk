import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer } from 'recharts'
import { get } from '../api'
import { ENTITY_TYPE_LABELS } from '../components/stats/CorpusCharts'

interface EntityItem {
  entity_text: string
  doc_count: number
}

interface TopicCluster {
  cluster_id: number
  name: string
  doc_count: number
  doc_ids: string[]
  sample_titles: string[]
}

interface TimelinePoint {
  month: string
  count: number
}

const ENTITY_FOLDERS: { type: string; label: string; icon: string }[] = [
  { type: 'PERSON', label: 'People', icon: 'person' },
  { type: 'GPE', label: 'Places', icon: 'place' },
  { type: 'ORG', label: 'Organizations', icon: 'org' },
]

const TICK_STYLE = { fontSize: 10, fill: 'var(--color-chart-tick)' }

export default function ExplorePage() {
  const [entities, setEntities] = useState<Record<string, EntityItem[]>>({})
  const [clusters, setClusters] = useState<TopicCluster[]>([])
  const [timeline, setTimeline] = useState<TimelinePoint[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [firstOpenType, setFirstOpenType] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false

    async function fetchAll() {
      try {
        const [personRes, gpeRes, orgRes, topicsRes, timelineRes] = await Promise.all([
          get<EntityItem[]>('/api/docs/entities/top', { entity_type: 'PERSON', limit: 30 }),
          get<EntityItem[]>('/api/docs/entities/top', { entity_type: 'GPE', limit: 30 }),
          get<EntityItem[]>('/api/docs/entities/top', { entity_type: 'ORG', limit: 30 }),
          get<{ clusters: TopicCluster[]; doc_count: number }>('/api/stats/topics'),
          get<TimelinePoint[]>('/api/stats/timeline'),
        ])

        if (cancelled) return

        const entityMap: Record<string, EntityItem[]> = {
          PERSON: personRes,
          GPE: gpeRes,
          ORG: orgRes,
        }
        setEntities(entityMap)
        setClusters(topicsRes.clusters)
        setTimeline(timelineRes)

        // Determine which folder to open first
        const first = ENTITY_FOLDERS.find((f) => entityMap[f.type]?.length > 0)
        if (first) setFirstOpenType(first.type)
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Failed to load data')
      } finally {
        if (!cancelled) setLoading(false)
      }
    }

    fetchAll()
    return () => {
      cancelled = true
    }
  }, [])

  if (loading) {
    return (
      <div className="flex min-h-[300px] items-center justify-center">
        <p className="text-sm text-(--color-text-secondary)">Loading...</p>
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

  const timelineData = timeline.map((t) => ({
    month: new Date(t.month).toLocaleDateString(undefined, { year: '2-digit', month: 'short' }),
    count: t.count,
  }))

  return (
    <div className="space-y-6">
      <h1 className="text-lg font-semibold text-(--color-text-primary)">Explore</h1>

      {/* Entity Folders */}
      <section>
        <h2 className="text-base font-semibold text-(--color-text-primary) mb-3">Entity Folders</h2>
        <div className="space-y-3">
          {ENTITY_FOLDERS.map((folder) => {
            const items = entities[folder.type] || []
            return (
              <details
                key={folder.type}
                className="rounded-xl bg-white dark:bg-[#2c2c2e] shadow-mac"
                open={folder.type === firstOpenType}
              >
                <summary className="cursor-pointer select-none px-5 py-3 text-[13px] font-semibold text-(--color-text-primary) list-none flex items-center justify-between">
                  <span>
                    {folder.label}
                    <span className="ml-2 text-[11px] font-normal text-(--color-text-secondary)">
                      {ENTITY_TYPE_LABELS[folder.type] ?? folder.type}
                    </span>
                  </span>
                  <span className="text-[11px] font-normal text-(--color-text-secondary)">
                    {items.length} {items.length === 1 ? 'entity' : 'entities'}
                  </span>
                </summary>
                {items.length === 0 ? (
                  <p className="px-5 pb-4 text-sm text-(--color-text-secondary)">No entities found.</p>
                ) : (
                  <div className="px-5 pb-4">
                    <div className="flex flex-wrap gap-2">
                      {items.map((item) => (
                        <Link
                          key={item.entity_text}
                          to={`/docs?entity=${encodeURIComponent(item.entity_text)}&entity_type=${folder.type}`}
                          className="inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-[12px] text-(--color-text-primary) bg-(--color-bg-secondary) hover:bg-black/6 dark:hover:bg-white/10 transition-colors"
                        >
                          <span className="font-medium">{item.entity_text}</span>
                          <span className="rounded-full bg-black/8 dark:bg-white/12 px-1.5 py-0.5 text-[10px] text-(--color-text-secondary) tabular-nums">
                            {item.doc_count}
                          </span>
                        </Link>
                      ))}
                    </div>
                  </div>
                )}
              </details>
            )
          })}
        </div>
      </section>

      {/* Topic Clusters */}
      <section>
        <h2 className="text-base font-semibold text-(--color-text-primary) mb-3">Topic Clusters</h2>
        {clusters.length === 0 ? (
          <div className="rounded-xl bg-white dark:bg-[#2c2c2e] shadow-mac px-5 py-8 text-center">
            <p className="text-sm text-(--color-text-secondary)">
              No topic clusters yet. Clusters appear after enough documents have been processed.
            </p>
          </div>
        ) : (
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {clusters.map((cluster) => (
              <Link
                key={cluster.cluster_id}
                to={`/docs?doc_type=${encodeURIComponent(cluster.name)}`}
                className="rounded-xl bg-white dark:bg-[#2c2c2e] shadow-mac px-5 py-4 hover:shadow-mac-lg transition-shadow block"
              >
                <h3 className="text-[13px] font-semibold text-(--color-text-primary) mb-1">{cluster.name}</h3>
                <p className="text-[11px] text-(--color-text-secondary) mb-2">
                  {cluster.doc_count} {cluster.doc_count === 1 ? 'document' : 'documents'}
                </p>
                <ul className="space-y-0.5">
                  {cluster.sample_titles.slice(0, 5).map((title, i) => (
                    <li key={i} className="text-[12px] text-(--color-text-secondary) truncate">
                      {title}
                    </li>
                  ))}
                </ul>
              </Link>
            ))}
          </div>
        )}
      </section>

      {/* Timeline */}
      <section>
        <h2 className="text-base font-semibold text-(--color-text-primary) mb-3">Timeline</h2>
        <div className="rounded-xl bg-white dark:bg-[#2c2c2e] shadow-mac px-5 py-4">
          {timelineData.length <= 2 ? (
            timelineData.length === 0 ? (
              <p className="py-4 text-center text-sm text-(--color-text-secondary)">No timeline data yet.</p>
            ) : (
              <ul className="space-y-2">
                {timelineData.map((d) => (
                  <li key={d.month} className="flex items-center justify-between text-sm">
                    <span className="font-medium text-(--color-text-primary)">{d.month}</span>
                    <span className="text-(--color-text-secondary)">
                      {d.count} {d.count === 1 ? 'document' : 'documents'}
                    </span>
                  </li>
                ))}
              </ul>
            )
          ) : (
            <ResponsiveContainer width="100%" height={220}>
              <BarChart data={timelineData} margin={{ left: -10, right: 8, top: 0, bottom: 0 }}>
                <XAxis dataKey="month" tick={TICK_STYLE} axisLine={false} tickLine={false} />
                <YAxis tick={TICK_STYLE} axisLine={false} tickLine={false} />
                <Tooltip
                  cursor={false}
                  content={({ active, payload, label }) => {
                    if (!active || !payload?.length) return null
                    return (
                      <div className="rounded-lg bg-white dark:bg-[#3a3a3c] shadow-mac-lg px-3 py-1.5 text-[12px] text-(--color-text-primary) ring-1 ring-(--color-border)">
                        <span className="font-medium">{label}</span>: {Number(payload[0].value).toLocaleString()}{' '}
                        documents
                      </div>
                    )
                  }}
                />
                <Bar dataKey="count" fill="var(--color-accent)" radius={[4, 4, 0, 0]} barSize={20} />
              </BarChart>
            </ResponsiveContainer>
          )}
        </div>
      </section>
    </div>
  )
}

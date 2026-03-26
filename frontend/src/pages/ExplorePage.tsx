import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer } from 'recharts'
import { get } from '../api'

interface EntityItem {
  entity_text: string
  doc_count: number
}

interface TopicCluster {
  cluster_id: number
  name: string
  keywords: string[]
  doc_count: number
  representative_doc_ids: string[]
}

interface TimelinePoint {
  month: string
  count: number
}

interface SubPaneState {
  type: 'entity' | 'cluster'
  label: string
  entityText?: string
  entityType?: string
  clusterId?: number
  docIds?: string[]
}

interface DocSummary {
  doc_id: string
  title: string
  canonical_filename?: string
  status: string
  latest_version_status?: string
  version_count: number
  updated_at: string
  summary?: string
  summary_model?: string
  doc_type?: string
  topic_id?: number | null
}

interface PaginatedDocs {
  items: DocSummary[]
  total: number
}

const ENTITY_SECTIONS = [
  { type: 'PERSON', label: 'People' },
  { type: 'GPE', label: 'Places' },
  { type: 'ORG', label: 'Organizations' },
]

const TICK_STYLE = { fontSize: 10, fill: 'var(--color-chart-tick)' }
const SUB_PANE_KEY = 'explore-sub-pane'

const PROCESSING_STATUSES = new Set([
  'queued',
  'extracting',
  'extracted',
  'ocr_running',
  'ocr_done',
  'chunking',
  'chunked',
  'extracting_entities',
  'entities_done',
  'embedding',
  'embedded',
  'finalizing',
  'summarizing',
  'summarized',
])

function StatusBadge({ status }: { status: string }) {
  const display = !status ? 'unknown' : PROCESSING_STATUSES.has(status) ? 'processing' : status
  let cls = 'bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300'
  if (display === 'ready') cls = 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400'
  else if (display === 'error') cls = 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400'
  else if (display === 'processing') cls = 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400'
  return <span className={`inline-block rounded-md px-2 py-0.5 text-[11px] font-medium ${cls}`}>{display}</span>
}

/** Compute topics sharing the most keywords with a given topic */
function getRelatedTopics(topic: TopicCluster, allTopics: TopicCluster[], max: number = 3): TopicCluster[] {
  const myKeywords = new Set(topic.keywords)
  const scored = allTopics
    .filter((t) => t.cluster_id !== topic.cluster_id)
    .map((t) => {
      const overlap = t.keywords.filter((kw) => myKeywords.has(kw)).length
      return { topic: t, overlap }
    })
    .filter((s) => s.overlap > 0)
    .sort((a, b) => b.overlap - a.overlap)
  return scored.slice(0, max).map((s) => s.topic)
}

export default function ExplorePage() {
  const [entities, setEntities] = useState<Record<string, EntityItem[]>>({})
  const [clusters, setClusters] = useState<TopicCluster[]>([])
  const [timeline, setTimeline] = useState<TimelinePoint[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  // Sub-pane state with sessionStorage persistence + browser history integration
  const [subPane, setSubPaneRaw] = useState<SubPaneState | null>(() => {
    try {
      const saved = sessionStorage.getItem(SUB_PANE_KEY)
      return saved ? JSON.parse(saved) : null
    } catch {
      return null
    }
  })

  // Wrap setSubPane to push/pop browser history entries
  const openSubPane = useCallback((state: SubPaneState) => {
    setSubPaneRaw(state)
    window.history.pushState({ exploreSubPane: true }, '')
  }, [])

  // Listen for browser back (swipe gesture, keyboard shortcut, etc.)
  useEffect(() => {
    function onPopState() {
      // If we're showing a sub-pane, close it on back navigation
      setSubPaneRaw((current) => {
        if (current) {
          // Sub-pane was open, close it — this consumes the back action
          return null
        }
        return current
      })
    }
    window.addEventListener('popstate', onPopState)
    return () => window.removeEventListener('popstate', onPopState)
  }, [])

  // If component mounts with a restored sub-pane from sessionStorage,
  // push a history entry so back gesture works
  const didPushRestoredRef = useRef(false)
  useEffect(() => {
    if (subPane && !didPushRestoredRef.current) {
      didPushRestoredRef.current = true
      window.history.pushState({ exploreSubPane: true }, '')
    }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (subPane) {
      sessionStorage.setItem(SUB_PANE_KEY, JSON.stringify(subPane))
    } else {
      sessionStorage.removeItem(SUB_PANE_KEY)
    }
  }, [subPane])

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
        setEntities({ PERSON: personRes, GPE: gpeRes, ORG: orgRes })
        setClusters(topicsRes.clusters)
        setTimeline(timelineRes)
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

  if (subPane) {
    return (
      <ExploreDocList
        subPane={subPane}
        clusters={clusters}
        onBack={() => window.history.back()}
        onOpenSubPane={openSubPane}
      />
    )
  }

  return <ExploreMain entities={entities} clusters={clusters} timeline={timeline} onOpenSubPane={openSubPane} />
}

/* ──────────────────────────────── Main explore view ──────────────────────────────── */

function ExploreMain({
  entities,
  clusters,
  timeline,
  onOpenSubPane,
}: {
  entities: Record<string, EntityItem[]>
  clusters: TopicCluster[]
  timeline: TimelinePoint[]
  onOpenSubPane: (state: SubPaneState) => void
}) {
  const [topicSearch, setTopicSearch] = useState('')

  const timelineData = timeline.map((t) => ({
    month: new Date(t.month).toLocaleDateString(undefined, { year: '2-digit', month: 'short' }),
    count: t.count,
  }))

  const lowerSearch = topicSearch.toLowerCase().trim()

  // Filter clusters by search
  const filteredClusters = useMemo(() => {
    if (!lowerSearch) return clusters
    return clusters.filter(
      (c) =>
        c.name.toLowerCase().includes(lowerSearch) || c.keywords.some((kw) => kw.toLowerCase().includes(lowerSearch)),
    )
  }, [clusters, lowerSearch])

  // Filter entities by search (highlight matching ones)
  const filteredEntities = useMemo(() => {
    if (!lowerSearch) return entities
    const result: Record<string, EntityItem[]> = {}
    for (const [type, items] of Object.entries(entities)) {
      const matched = items.filter((item) => item.entity_text.toLowerCase().includes(lowerSearch))
      result[type] = matched.length > 0 ? matched : items
    }
    return result
  }, [entities, lowerSearch])

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between gap-3">
        <h1 className="text-lg font-semibold text-(--color-text-primary)">Explore</h1>

        {/* Improvement #5: Topic search */}
        <div className="relative">
          <svg
            className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-(--color-text-secondary)"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            strokeWidth={2}
          >
            <path strokeLinecap="round" strokeLinejoin="round" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
          </svg>
          <input
            type="text"
            placeholder="Search topics & entities..."
            value={topicSearch}
            onChange={(e) => setTopicSearch(e.target.value)}
            className="w-64 rounded-lg border-0 bg-(--color-bg-secondary) dark:bg-(--color-bg-tertiary) shadow-mac pl-8 pr-3 py-1.5 text-xs text-(--color-text-primary) placeholder-(--color-text-secondary) focus:outline-hidden focus:ring-2 focus:ring-(--color-accent)/30 focus:shadow-md transition-shadow"
          />
          {topicSearch && (
            <button
              onClick={() => setTopicSearch('')}
              className="absolute right-2 top-1/2 -translate-y-1/2 text-(--color-text-secondary) hover:text-(--color-text-primary)"
            >
              <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          )}
        </div>
      </div>

      {/* Entity sections — People / Places / Organizations */}
      <div className="rounded-xl bg-white dark:bg-[#2c2c2e] shadow-mac ring-1 ring-(--color-border) overflow-hidden divide-y divide-(--color-border)">
        {ENTITY_SECTIONS.map((section) => (
          <EntitySection
            key={section.type}
            label={section.label}
            items={filteredEntities[section.type] || []}
            defaultOpen
            onSelect={(entityText) =>
              onOpenSubPane({
                type: 'entity',
                label: `${section.label}: ${entityText}`,
                entityText,
                entityType: section.type,
              })
            }
          />
        ))}
      </div>

      {/* Topic Clusters */}
      {clusters.length > 0 && (
        <section>
          <h2 className="text-[13px] font-semibold text-(--color-text-secondary) uppercase tracking-wider mb-3">
            Topic Clusters
            {lowerSearch && filteredClusters.length !== clusters.length && (
              <span className="ml-2 font-normal normal-case tracking-normal">
                ({filteredClusters.length} of {clusters.length})
              </span>
            )}
          </h2>
          {filteredClusters.length === 0 ? (
            <div className="rounded-xl bg-white dark:bg-[#2c2c2e] shadow-mac ring-1 ring-(--color-border) px-5 py-8 text-center">
              <p className="text-sm text-(--color-text-secondary)">No topics match your search.</p>
            </div>
          ) : (
            <div className="max-h-96 overflow-y-auto rounded-xl ring-1 ring-(--color-border) p-3 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {filteredClusters.map((cluster) => (
                <button
                  key={cluster.cluster_id}
                  onClick={() =>
                    onOpenSubPane({
                      type: 'cluster',
                      label: cluster.name,
                      clusterId: cluster.cluster_id,
                    })
                  }
                  className="rounded-xl bg-white dark:bg-[#2c2c2e] shadow-mac ring-1 ring-(--color-border) px-5 py-4 hover:shadow-mac-lg hover:ring-(--color-border)/80 transition-all text-left group"
                >
                  <div className="flex items-center justify-between mb-1">
                    <h3 className="text-[13px] font-semibold text-(--color-text-primary)">{cluster.name}</h3>
                    <svg
                      className="h-3.5 w-3.5 text-(--color-text-secondary) opacity-0 group-hover:opacity-100 transition-opacity"
                      fill="none"
                      viewBox="0 0 24 24"
                      stroke="currentColor"
                      strokeWidth={2}
                    >
                      <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
                    </svg>
                  </div>
                  {/* Improvement #1: Keyword pills */}
                  {cluster.keywords.length > 0 && (
                    <div className="flex flex-wrap gap-1 mt-1.5">
                      {cluster.keywords.slice(0, 5).map((kw) => (
                        <span
                          key={kw}
                          className="text-[10px] px-1.5 py-0.5 rounded-full bg-gray-100 dark:bg-gray-700 text-gray-500 dark:text-gray-400"
                        >
                          {kw}
                        </span>
                      ))}
                    </div>
                  )}
                  <p className="text-[11px] text-(--color-text-secondary) mt-2">
                    {cluster.doc_count} {cluster.doc_count === 1 ? 'document' : 'documents'}
                  </p>
                </button>
              ))}
            </div>
          )}
        </section>
      )}

      {/* Timeline */}
      <section>
        <h2 className="text-[13px] font-semibold text-(--color-text-secondary) uppercase tracking-wider mb-3">
          Timeline
        </h2>
        <div className="rounded-xl bg-white dark:bg-[#2c2c2e] shadow-mac ring-1 ring-(--color-border) px-5 py-4">
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

/* ──────────────────────────────── Entity disclosure section ──────────────────────────────── */

function EntitySection({
  label,
  items,
  defaultOpen,
  onSelect,
}: {
  label: string
  items: EntityItem[]
  defaultOpen: boolean
  onSelect: (entityText: string) => void
}) {
  const [open, setOpen] = useState(defaultOpen)

  return (
    <div>
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex items-center px-5 py-3 text-left hover:bg-black/3 dark:hover:bg-white/3 transition-colors"
      >
        <svg
          className={`h-3 w-3 mr-2.5 text-(--color-text-secondary) transition-transform duration-200 ${open ? 'rotate-90' : ''}`}
          viewBox="0 0 16 16"
          fill="currentColor"
        >
          <path d="M6 3l6 5-6 5V3z" />
        </svg>
        <span className="text-[13px] font-semibold text-(--color-text-primary) flex-1">{label}</span>
        <span className="text-[11px] text-(--color-text-secondary) tabular-nums">
          {items.length} {items.length === 1 ? 'entity' : 'entities'}
        </span>
      </button>
      {open && (
        <div className="px-5 pb-4 pt-1">
          {items.length === 0 ? (
            <p className="text-sm text-(--color-text-secondary)">No entities found.</p>
          ) : (
            <div className="flex flex-wrap gap-1.5">
              {items.map((item) => (
                <button
                  key={item.entity_text}
                  onClick={() => onSelect(item.entity_text)}
                  className="inline-flex items-center gap-1 rounded-lg px-2.5 py-1.5 text-[12px] text-(--color-text-primary) bg-(--color-bg-secondary) hover:bg-black/6 dark:hover:bg-white/10 transition-colors"
                >
                  <span className="font-medium">{item.entity_text}</span>
                  <span className="rounded-full bg-black/8 dark:bg-white/12 px-1.5 py-0.5 text-[10px] text-(--color-text-secondary) tabular-nums">
                    {item.doc_count}
                  </span>
                </button>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

/* ──────────────────────────────── Sub-pane: filtered document list ──────────────────────────────── */

function ExploreDocList({
  subPane,
  clusters,
  onBack,
  onOpenSubPane,
}: {
  subPane: SubPaneState
  clusters: TopicCluster[]
  onBack: () => void
  onOpenSubPane: (state: SubPaneState) => void
}) {
  const [docs, setDocs] = useState<DocSummary[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [currentPage, setCurrentPage] = useState(1)
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const pageSize = 25

  // Filter & sort state (subset of Documents page — no entity filter, no upload)
  const [filterOptions, setFilterOptions] = useState<{
    mime_types: { value: string; count: number }[]
  }>({ mime_types: [] })
  const [filterInput, setFilterInput] = useState('')
  const [filter, setFilter] = useState('')
  const [mimeFilter, setMimeFilter] = useState('')
  const [topicFilter, setTopicFilter] = useState('')
  const [sortField, setSortField] = useState<'updated' | 'created' | 'title'>('updated')
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('desc')

  const filterTimerRef = useRef<ReturnType<typeof setTimeout>>(undefined)

  function handleFilterChange(value: string) {
    setFilterInput(value)
    clearTimeout(filterTimerRef.current)
    filterTimerRef.current = setTimeout(() => {
      setFilter(value)
      setCurrentPage(1)
    }, 300)
  }

  // Load filter options on mount
  useEffect(() => {
    get<{ mime_types: { value: string; count: number }[] }>('/api/docs/filters')
      .then((data) => setFilterOptions({ mime_types: data.mime_types }))
      .catch(() => {})
  }, [])

  // Determine effective doc_ids for the query, considering topic filter override
  const effectiveDocIds = useMemo(() => {
    if (topicFilter) {
      const tc = clusters.find((c) => String(c.cluster_id) === topicFilter)
      if (tc) return tc.representative_doc_ids
    }
    return subPane.docIds
  }, [topicFilter, clusters, subPane.docIds])

  const loadDocs = useCallback(
    (page: number) => {
      const params: Record<string, string | number> = {
        limit: pageSize,
        offset: (page - 1) * pageSize,
        sort: sortField,
        sort_dir: sortDir,
      }

      if (filter) params.q = filter
      if (mimeFilter) params.mime_type = mimeFilter

      if (topicFilter) {
        params.topic_id = topicFilter
      } else if (subPane.type === 'entity' && subPane.entityText) {
        params.entity = subPane.entityText
        if (subPane.entityType) params.entity_type = subPane.entityType
      } else if (subPane.type === 'cluster' && subPane.clusterId != null) {
        params.topic_id = subPane.clusterId
      }

      return get<PaginatedDocs>('/api/docs', params)
        .then((data) => {
          setDocs(data.items)
          setTotal(data.total)
        })
        .catch(() => {})
        .finally(() => setLoading(false))
    },
    [subPane, pageSize, sortField, sortDir, filter, mimeFilter, topicFilter, effectiveDocIds],
  )

  useEffect(() => {
    loadDocs(currentPage)
  }, [loadDocs, currentPage])

  const totalPages = Math.max(1, Math.ceil(total / pageSize))

  // Improvement #2: Related topics (only for cluster sub-pane)
  const activeTopic = useMemo(() => {
    if (subPane.type === 'cluster' && subPane.clusterId != null) {
      return clusters.find((c) => c.cluster_id === subPane.clusterId) || null
    }
    return null
  }, [subPane, clusters])

  const relatedTopics = useMemo(() => {
    if (!activeTopic) return []
    return getRelatedTopics(activeTopic, clusters, 3)
  }, [activeTopic, clusters])

  // Improvement #4: Entity-topic cross-reference
  const entityTopics = useMemo(() => {
    if (subPane.type !== 'entity' || docs.length === 0 || clusters.length === 0) return []
    // Match loaded documents' topic_ids against clusters
    const topicCounts = new Map<number, number>()
    for (const doc of docs) {
      if (doc.topic_id != null) {
        topicCounts.set(doc.topic_id, (topicCounts.get(doc.topic_id) || 0) + 1)
      }
    }
    // Also best-effort match via representative_doc_ids
    const docIdSet = new Set(docs.map((d) => d.doc_id))
    for (const cluster of clusters) {
      const matchCount = cluster.representative_doc_ids.filter((id) => docIdSet.has(id)).length
      if (matchCount > 0 && !topicCounts.has(cluster.cluster_id)) {
        topicCounts.set(cluster.cluster_id, matchCount)
      }
    }
    return [...topicCounts.entries()]
      .sort((a, b) => b[1] - a[1])
      .slice(0, 5)
      .map(([topicId]) => clusters.find((c) => c.cluster_id === topicId))
      .filter((c): c is TopicCluster => c != null)
  }, [subPane.type, docs, clusters])

  const hasActiveFilters = filter || mimeFilter || topicFilter

  return (
    <div className="animate-slide-in">
      {/* Breadcrumb header */}
      <div className="mb-4 flex items-center gap-2">
        <button
          onClick={onBack}
          className="flex items-center gap-1 text-[13px] text-(--color-accent) hover:text-(--color-accent)/80 transition-colors"
        >
          <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M15 19l-7-7 7-7" />
          </svg>
          Explore
        </button>
        <span className="text-(--color-text-secondary) text-[11px]">/</span>
        <h1 className="text-lg font-semibold text-(--color-text-primary)">{subPane.label}</h1>
      </div>

      {/* Improvement #2: Related topics (for cluster sub-pane) */}
      {relatedTopics.length > 0 && (
        <div className="mb-3 rounded-lg bg-(--color-bg-secondary) dark:bg-(--color-bg-tertiary) px-4 py-2.5">
          <span className="text-[11px] font-medium text-(--color-text-secondary) uppercase tracking-wider mr-2">
            Related topics
          </span>
          <div className="inline-flex flex-wrap gap-1.5 mt-1">
            {relatedTopics.map((rt) => (
              <button
                key={rt.cluster_id}
                onClick={() =>
                  onOpenSubPane({
                    type: 'cluster',
                    label: rt.name,
                    clusterId: rt.cluster_id,
                    docIds: rt.representative_doc_ids,
                  })
                }
                className="inline-flex items-center gap-1 rounded-full px-2.5 py-1 text-[11px] bg-white dark:bg-[#3a3a3c] text-(--color-text-primary) shadow-sm ring-1 ring-(--color-border) hover:shadow-md transition-shadow"
              >
                {rt.name}
                <span className="text-[10px] text-(--color-text-secondary)">({rt.doc_count})</span>
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Improvement #4: Entity-topic cross-reference */}
      {entityTopics.length > 0 && (
        <div className="mb-3 rounded-lg bg-(--color-bg-secondary) dark:bg-(--color-bg-tertiary) px-4 py-2.5">
          <span className="text-[11px] font-medium text-(--color-text-secondary) uppercase tracking-wider mr-2">
            Topics containing this entity
          </span>
          <div className="inline-flex flex-wrap gap-1.5 mt-1">
            {entityTopics.map((et) => (
              <button
                key={et.cluster_id}
                onClick={() =>
                  onOpenSubPane({
                    type: 'cluster',
                    label: et.name,
                    clusterId: et.cluster_id,
                    docIds: et.representative_doc_ids,
                  })
                }
                className="inline-flex items-center gap-1 rounded-full px-2.5 py-1 text-[11px] bg-white dark:bg-[#3a3a3c] text-(--color-text-primary) shadow-sm ring-1 ring-(--color-border) hover:shadow-md transition-shadow"
              >
                {et.name}
                <span className="text-[10px] text-(--color-text-secondary)">({et.doc_count})</span>
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Filter bar */}
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <span className="text-[12px] text-(--color-text-secondary)">
          {total} {total === 1 ? 'document' : 'documents'}
        </span>

        <div className="flex-1" />

        <input
          type="text"
          placeholder="Filter by filename..."
          value={filterInput}
          onChange={(e) => handleFilterChange(e.target.value)}
          className="w-52 rounded-lg border-0 bg-(--color-bg-secondary) dark:bg-(--color-bg-tertiary) shadow-mac px-3 py-1 text-xs text-(--color-text-primary) placeholder-(--color-text-secondary) focus:outline-hidden focus:ring-2 focus:ring-(--color-accent)/30 focus:shadow-md transition-shadow"
        />

        {filterOptions.mime_types.length > 0 && (
          <select
            value={mimeFilter}
            onChange={(e) => {
              setMimeFilter(e.target.value)
              setCurrentPage(1)
            }}
            className="rounded-lg border-0 bg-(--color-bg-secondary) dark:bg-(--color-bg-tertiary) shadow-mac px-2 py-1 text-xs text-(--color-text-primary) focus:outline-hidden focus:ring-2 focus:ring-(--color-accent)/30"
          >
            <option value="">All types</option>
            {filterOptions.mime_types.map((m) => (
              <option key={m.value} value={m.value}>
                {m.value.split('/').pop()} ({m.count})
              </option>
            ))}
          </select>
        )}

        {/* Improvement #3: Topic dropdown filter */}
        {clusters.length > 0 && (
          <select
            value={topicFilter}
            onChange={(e) => {
              setTopicFilter(e.target.value)
              setCurrentPage(1)
            }}
            className="max-w-48 rounded-lg border-0 bg-(--color-bg-secondary) dark:bg-(--color-bg-tertiary) shadow-mac px-2 py-1 text-xs text-(--color-text-primary) focus:outline-hidden focus:ring-2 focus:ring-(--color-accent)/30"
          >
            <option value="">All topics</option>
            {clusters.map((c) => (
              <option key={c.cluster_id} value={String(c.cluster_id)}>
                {c.name} ({c.doc_count})
              </option>
            ))}
          </select>
        )}

        {/* Sort controls */}
        <div className="flex items-center gap-1 text-xs text-gray-400">
          <span>Sort:</span>
          {(['updated', 'created', 'title'] as const).map((field) => (
            <button
              key={field}
              onClick={() => {
                if (sortField === field) {
                  setSortDir(sortDir === 'desc' ? 'asc' : 'desc')
                } else {
                  setSortField(field)
                  setSortDir(field === 'title' ? 'asc' : 'desc')
                }
                setCurrentPage(1)
              }}
              className={`rounded px-1.5 py-0.5 ${
                sortField === field
                  ? 'bg-gray-200 dark:bg-gray-700 text-(--color-text-primary) font-medium'
                  : 'hover:bg-gray-100 dark:hover:bg-gray-700/50'
              }`}
            >
              {field === 'updated' ? 'Updated' : field === 'created' ? 'Created' : 'Name'}
              {sortField === field && <span className="ml-0.5">{sortDir === 'desc' ? '\u2193' : '\u2191'}</span>}
            </button>
          ))}
        </div>

        {hasActiveFilters && (
          <button
            onClick={() => {
              setFilterInput('')
              setFilter('')
              setMimeFilter('')
              setTopicFilter('')
              setCurrentPage(1)
            }}
            className="text-xs text-blue-600 dark:text-blue-400 hover:underline"
          >
            Clear filters
          </button>
        )}
      </div>

      {loading && docs.length === 0 ? (
        <div className="flex min-h-[200px] items-center justify-center">
          <p className="text-sm text-(--color-text-secondary)">Loading...</p>
        </div>
      ) : docs.length === 0 ? (
        <div className="rounded-xl bg-white dark:bg-[#2c2c2e] shadow-mac ring-1 ring-(--color-border) px-5 py-8 text-center">
          <p className="text-sm text-(--color-text-secondary)">No documents found.</p>
        </div>
      ) : (
        <>
          <div className="rounded-xl bg-white dark:bg-[#2c2c2e] shadow-mac ring-1 ring-(--color-border) overflow-hidden">
            <table className="min-w-full divide-y divide-(--color-border)">
              <thead className="bg-(--color-bg-secondary)">
                <tr>
                  <th className="px-4 py-3 text-left text-xs font-medium uppercase text-gray-500 dark:text-gray-400">
                    Title
                  </th>
                  <th className="px-4 py-3 text-left text-xs font-medium uppercase text-gray-500 dark:text-gray-400">
                    Status
                  </th>
                  <th className="px-4 py-3 text-left text-xs font-medium uppercase text-gray-500 dark:text-gray-400">
                    Versions
                  </th>
                  <th className="px-4 py-3 text-left text-xs font-medium uppercase text-gray-500 dark:text-gray-400">
                    Updated
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-(--color-border)">
                {docs.map((doc) => {
                  const isExpanded = expanded.has(doc.doc_id)
                  return (
                    <Fragment key={doc.doc_id}>
                      <tr className="hover:bg-black/3 dark:hover:bg-white/3">
                        <td className="px-4 py-3">
                          <div className="flex items-center gap-1.5">
                            <button
                              onClick={() =>
                                setExpanded((prev) => {
                                  const next = new Set(prev)
                                  if (next.has(doc.doc_id)) next.delete(doc.doc_id)
                                  else next.add(doc.doc_id)
                                  return next
                                })
                              }
                              className="rounded-sm p-0.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"
                              title="Toggle details"
                            >
                              <svg
                                className={`h-3.5 w-3.5 transition-transform ${isExpanded ? 'rotate-90' : ''}`}
                                fill="none"
                                viewBox="0 0 24 24"
                                stroke="currentColor"
                                strokeWidth={2}
                              >
                                <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
                              </svg>
                            </button>
                            <div>
                              <Link
                                to={`/docs/${doc.doc_id}`}
                                className="font-medium text-[13px] text-blue-600 dark:text-blue-400 hover:underline"
                              >
                                {doc.title}
                              </Link>
                              {doc.canonical_filename && (
                                <div className="text-[11px] text-gray-400 mt-0.5">{doc.canonical_filename}</div>
                              )}
                            </div>
                          </div>
                        </td>
                        <td className="px-4 py-3">
                          <StatusBadge status={doc.latest_version_status || doc.status} />
                        </td>
                        <td className="px-4 py-3 text-sm text-gray-600 dark:text-gray-400">{doc.version_count}</td>
                        <td className="px-4 py-3 text-sm text-gray-500 dark:text-gray-400">
                          {new Date(doc.updated_at).toLocaleDateString()}
                        </td>
                      </tr>
                      {isExpanded && (
                        <tr className="bg-gray-50/50 dark:bg-white/2">
                          <td colSpan={4} className="px-4 py-3 pl-14">
                            <div className="space-y-1 text-sm">
                              {doc.doc_type && (
                                <div>
                                  <span className="font-medium text-gray-500 dark:text-gray-400">Type: </span>
                                  <span className="text-gray-700 dark:text-gray-300">{doc.doc_type}</span>
                                </div>
                              )}
                              <div>
                                <span className="font-medium text-gray-500 dark:text-gray-400">
                                  Summary
                                  {doc.summary_model ? (
                                    <span className="font-normal text-gray-400 dark:text-gray-500">
                                      {' '}
                                      ({doc.summary_model})
                                    </span>
                                  ) : (
                                    ''
                                  )}
                                  :{' '}
                                </span>
                                {doc.summary ? (
                                  <span className="text-gray-700 dark:text-gray-300">{doc.summary}</span>
                                ) : (
                                  <span className="italic text-gray-400 dark:text-gray-500">No summary</span>
                                )}
                              </div>
                            </div>
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  )
                })}
              </tbody>
            </table>
          </div>

          {/* Pagination */}
          {totalPages > 1 && (
            <SubPanePagination currentPage={currentPage} totalPages={totalPages} onPageChange={setCurrentPage} />
          )}

          <div className="mt-2 text-center text-xs text-gray-400">
            Showing {(currentPage - 1) * pageSize + 1}–{Math.min(currentPage * pageSize, total)} of {total}
          </div>
        </>
      )}
    </div>
  )
}

function SubPanePagination({
  currentPage,
  totalPages,
  onPageChange,
}: {
  currentPage: number
  totalPages: number
  onPageChange: (p: number) => void
}) {
  const pages = Array.from({ length: totalPages }, (_, i) => i + 1)
    .filter((p) => p === 1 || p === totalPages || Math.abs(p - currentPage) <= 1)
    .reduce<(number | '...')[]>((acc, p) => {
      if (acc.length > 0) {
        const last = acc[acc.length - 1]
        if (typeof last === 'number' && p - last > 1) acc.push('...')
      }
      acc.push(p)
      return acc
    }, [])

  return (
    <div className="mt-3 flex items-center justify-center gap-1">
      <button
        onClick={() => onPageChange(Math.max(1, currentPage - 1))}
        disabled={currentPage <= 1}
        className="rounded-lg px-2 py-1 text-sm text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-30"
      >
        Prev
      </button>
      {pages.map((p, i) =>
        p === '...' ? (
          <span key={`e${i}`} className="px-2 text-sm text-gray-400">
            ...
          </span>
        ) : (
          <button
            key={p}
            onClick={() => onPageChange(p)}
            className={`rounded-lg px-2.5 py-1 text-sm font-medium ${
              p === currentPage
                ? 'bg-(--color-accent) text-white'
                : 'text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700'
            }`}
          >
            {p}
          </button>
        ),
      )}
      <button
        onClick={() => onPageChange(Math.min(totalPages, currentPage + 1))}
        disabled={currentPage >= totalPages}
        className="rounded-lg px-2 py-1 text-sm text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-30"
      >
        Next
      </button>
    </div>
  )
}

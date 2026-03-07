import { ReactNode, useCallback, useEffect, useRef, useState } from 'react'
import { useParams, useNavigate, useSearchParams, Link } from 'react-router-dom'
import { get, post, del } from '../api'
import { useAuth } from '../auth'
import { useJobEvents, type JobEvent } from '../hooks/useJobEvents'

interface JobInfo {
  job_id: string
  stage: string
  status: string
  progress_current?: number
  progress_total?: number
  error?: string
  created_at: string
  started_at?: string
  finished_at?: string
}

interface VersionInfo {
  version_id: string
  status: string
  mime_type?: string
  size_bytes?: number
  has_text_layer?: boolean
  needs_ocr?: boolean
  extracted_chars?: number
  source_path?: string
  error?: string
  created_at: string
  jobs: JobInfo[]
}

interface DocumentDetail {
  doc_id: string
  title: string
  canonical_filename?: string
  status: string
  created_at: string
  updated_at: string
  versions: VersionInfo[]
}

interface PageContent {
  page_num: number
  text: string
  ocr_used: boolean
  ocr_confidence?: number
}

interface ContentResponse {
  doc_id: string
  version_id: string
  pages: PageContent[]
  total_chars: number
}

function Disclosure({ label, defaultOpen, children }: { label: ReactNode; defaultOpen: boolean; children: ReactNode }) {
  const [open, setOpen] = useState(defaultOpen)

  return (
    <div>
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="flex items-center gap-1.5 text-sm font-medium text-gray-700 dark:text-gray-300 hover:text-gray-900 dark:hover:text-gray-100"
      >
        <svg
          className={`h-3.5 w-3.5 shrink-0 transition-transform ${open ? 'rotate-90' : ''}`}
          fill="currentColor"
          viewBox="0 0 20 20"
        >
          <path
            fillRule="evenodd"
            d="M7.21 14.77a.75.75 0 01.02-1.06L11.168 10 7.23 6.29a.75.75 0 111.04-1.08l4.5 4.25a.75.75 0 010 1.08l-4.5 4.25a.75.75 0 01-1.06-.02z"
            clipRule="evenodd"
          />
        </svg>
        {label}
      </button>
      {open && <div className="mt-2">{children}</div>}
    </div>
  )
}

function JobStatusBadge({ status }: { status: string }) {
  let cls = 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300'
  if (status === 'done') cls = 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400'
  else if (status === 'error') cls = 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400'
  else if (status === 'running') cls = 'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400 animate-pulse'
  else if (status === 'queued') cls = 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400'

  return <span className={`rounded-md px-2 py-0.5 text-[11px] font-medium ${cls}`}>{status}</span>
}

function VersionBanner({ version }: { version: VersionInfo }) {
  const hasJobs = version.jobs.length > 0
  const allDone = hasJobs && version.jobs.every((j) => j.status === 'done')
  const hasError = version.jobs.some((j) => j.status === 'error')
  const runningJob = version.jobs.find((j) => j.status === 'running' || j.status === 'queued')

  if (version.status === 'ready' || allDone) {
    return (
      <div className="mb-3 flex items-center gap-2 rounded-lg bg-green-50 dark:bg-green-900/20 px-3 py-2 text-sm text-green-700 dark:text-green-400">
        <svg className="h-4 w-4 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
        </svg>
        Ingestion complete
      </div>
    )
  }

  if (hasError) {
    const errorJob = version.jobs.find((j) => j.status === 'error')
    return (
      <div className="mb-3 flex items-center gap-2 rounded-lg bg-red-50 dark:bg-red-900/20 px-3 py-2 text-sm text-red-700 dark:text-red-400">
        <svg className="h-4 w-4 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
        </svg>
        Error in {errorJob?.stage}
        {errorJob?.error ? `: ${errorJob.error}` : ''}
      </div>
    )
  }

  if (runningJob) {
    const doneCount = version.jobs.filter((j) => j.status === 'done').length
    return (
      <div className="mb-3 flex items-center gap-2 rounded-lg bg-amber-50 dark:bg-amber-900/20 px-3 py-2 text-sm text-amber-700 dark:text-amber-400">
        <div className="h-3 w-3 animate-spin rounded-full border-2 border-current border-t-transparent" />
        Processing — stage {doneCount + 1} of {version.jobs.length}
      </div>
    )
  }

  return null
}

const PAGE_SIZE_OPTIONS = [10, 25, 50, 100]

function Pagination({
  currentPage,
  totalPages,
  onPageChange,
}: {
  currentPage: number
  totalPages: number
  onPageChange: (page: number) => void
}) {
  if (totalPages <= 1) return null

  // Build page number list with ellipsis
  const pages: (number | '...')[] = []
  if (totalPages <= 7) {
    for (let i = 1; i <= totalPages; i++) pages.push(i)
  } else {
    pages.push(1)
    if (currentPage > 3) pages.push('...')
    const start = Math.max(2, currentPage - 1)
    const end = Math.min(totalPages - 1, currentPage + 1)
    for (let i = start; i <= end; i++) pages.push(i)
    if (currentPage < totalPages - 2) pages.push('...')
    pages.push(totalPages)
  }

  const btn = 'inline-flex items-center justify-center min-w-8 h-8 rounded-sm text-sm font-medium'

  return (
    <div className="flex items-center gap-1">
      <button
        onClick={() => onPageChange(1)}
        disabled={currentPage === 1}
        className={`${btn} px-1.5 text-gray-600 dark:text-gray-400 hover:bg-black/4 dark:hover:bg-white/6 disabled:opacity-30 disabled:cursor-default`}
        title="First page"
      >
        &laquo;
      </button>
      <button
        onClick={() => onPageChange(currentPage - 1)}
        disabled={currentPage === 1}
        className={`${btn} px-1.5 text-gray-600 dark:text-gray-400 hover:bg-black/4 dark:hover:bg-white/6 disabled:opacity-30 disabled:cursor-default`}
        title="Previous page"
      >
        &lsaquo;
      </button>
      {pages.map((p, i) =>
        p === '...' ? (
          <span key={`ellipsis-${i}`} className={`${btn} px-1 text-gray-400`}>
            &hellip;
          </span>
        ) : (
          <button
            key={p}
            onClick={() => onPageChange(p)}
            className={`${btn} px-1.5 ${
              p === currentPage
                ? 'bg-blue-600 text-white'
                : 'text-gray-700 dark:text-gray-300 hover:bg-black/4 dark:hover:bg-white/6'
            }`}
          >
            {p}
          </button>
        ),
      )}
      <button
        onClick={() => onPageChange(currentPage + 1)}
        disabled={currentPage === totalPages}
        className={`${btn} px-1.5 text-gray-600 dark:text-gray-400 hover:bg-black/4 dark:hover:bg-white/6 disabled:opacity-30 disabled:cursor-default`}
        title="Next page"
      >
        &rsaquo;
      </button>
      <button
        onClick={() => onPageChange(totalPages)}
        disabled={currentPage === totalPages}
        className={`${btn} px-1.5 text-gray-600 dark:text-gray-400 hover:bg-black/4 dark:hover:bg-white/6 disabled:opacity-30 disabled:cursor-default`}
        title="Last page"
      >
        &raquo;
      </button>
    </div>
  )
}

interface DocStats {
  chunk_count: number
  page_count: number
  languages: Record<string, number>
  entity_types: Record<string, number>
  top_entities: { text: string; type: string; mentions: number }[]
  ocr_confidence: { avg: number; min: number; max: number } | null
}

const ENTITY_TYPE_COLORS: Record<string, string> = {
  PERSON: 'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400',
  ORG: 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400',
  GPE: 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400',
  LOC: 'bg-purple-100 text-purple-700 dark:bg-purple-900/30 dark:text-purple-400',
  DATE: 'bg-cyan-100 text-cyan-700 dark:bg-cyan-900/30 dark:text-cyan-400',
  EVENT: 'bg-pink-100 text-pink-700 dark:bg-pink-900/30 dark:text-pink-400',
}

const DEFAULT_HIDDEN_ENTITY_TYPES = new Set(['CARDINAL', 'ORDINAL', 'QUANTITY'])

function DocumentStatsDisclosure({ docId }: { docId: string }) {
  const [stats, setStats] = useState<DocStats | null>(null)
  const [loading, setLoading] = useState(false)
  const [open, setOpen] = useState(false)
  const loadedRef = useRef(false)
  const [hiddenTypes, setHiddenTypes] = useState<Set<string>>(() => new Set(DEFAULT_HIDDEN_ENTITY_TYPES))

  function handleToggle() {
    const willOpen = !open
    setOpen(willOpen)
    if (willOpen && !loadedRef.current) {
      loadedRef.current = true
      setLoading(true)
      get<DocStats>(`/api/docs/${docId}/stats?exclude_types=`)
        .then(setStats)
        .catch(() => {})
        .finally(() => setLoading(false))
    }
  }

  const langEntries = stats ? Object.entries(stats.languages) : []
  const langText =
    langEntries.length === 1
      ? `${langEntries[0][0]} (${langEntries[0][1]} chunks)`
      : langEntries.map(([lang, count]) => `${lang}: ${count}`).join(', ')

  return (
    <div className="mt-4 mb-2">
      <button
        type="button"
        onClick={handleToggle}
        className="flex items-center gap-1.5 text-sm font-medium text-gray-700 dark:text-gray-300 hover:text-gray-900 dark:hover:text-gray-100"
      >
        <svg
          className={`h-3.5 w-3.5 shrink-0 transition-transform ${open ? 'rotate-90' : ''}`}
          fill="currentColor"
          viewBox="0 0 20 20"
        >
          <path
            fillRule="evenodd"
            d="M7.21 14.77a.75.75 0 01.02-1.06L11.168 10 7.23 6.29a.75.75 0 111.04-1.08l4.5 4.25a.75.75 0 010 1.08l-4.5 4.25a.75.75 0 01-1.06-.02z"
            clipRule="evenodd"
          />
        </svg>
        Document Statistics
      </button>
      {open && (
        <div className="mt-2 rounded-xl bg-white dark:bg-[#2c2c2e] shadow-mac p-4">
          {loading ? (
            <p className="text-sm text-(--color-text-secondary)">Loading statistics...</p>
          ) : stats ? (
            <div className="space-y-3">
              <div className="flex flex-wrap gap-x-6 gap-y-1 text-sm text-(--color-text-secondary)">
                <span>
                  <span className="font-medium text-(--color-text-primary)">{stats.chunk_count}</span> chunks
                </span>
                <span>
                  <span className="font-medium text-(--color-text-primary)">{stats.page_count}</span> pages
                </span>
                {langText && <span>Language: {langText}</span>}
              </div>

              {stats.ocr_confidence && (
                <div className="text-sm text-(--color-text-secondary)">
                  OCR confidence: avg{' '}
                  <span className="font-medium text-(--color-text-primary)">
                    {(stats.ocr_confidence.avg * 100).toFixed(0)}%
                  </span>
                  , min {(stats.ocr_confidence.min * 100).toFixed(0)}%, max{' '}
                  {(stats.ocr_confidence.max * 100).toFixed(0)}%
                </div>
              )}

              {Object.keys(stats.entity_types).length > 0 && (
                <div>
                  <p className="mb-1.5 text-[11px] font-medium text-(--color-text-secondary) uppercase tracking-wide">
                    Entity Types
                  </p>
                  <div className="flex flex-wrap gap-1">
                    {Object.entries(stats.entity_types)
                      .sort((a, b) => b[1] - a[1])
                      .map(([type, count]) => {
                        const isHidden = hiddenTypes.has(type)
                        return (
                          <button
                            key={type}
                            type="button"
                            onClick={() =>
                              setHiddenTypes((prev) => {
                                const next = new Set(prev)
                                if (next.has(type)) next.delete(type)
                                else next.add(type)
                                return next
                              })
                            }
                            className={`rounded-md px-2 py-0.5 text-[11px] font-medium transition-opacity ${
                              isHidden
                                ? 'bg-gray-100 text-gray-400 dark:bg-gray-700/50 dark:text-gray-500 opacity-60'
                                : ENTITY_TYPE_COLORS[type] ||
                                  'bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300'
                            }`}
                          >
                            {type}: {count}
                          </button>
                        )
                      })}
                  </div>
                </div>
              )}

              {stats.top_entities.filter((e) => !hiddenTypes.has(e.type)).length > 0 && (
                <div>
                  <p className="mb-1.5 text-[11px] font-medium text-(--color-text-secondary) uppercase tracking-wide">
                    Top Entities
                  </p>
                  <div className="flex flex-wrap gap-1.5">
                    {stats.top_entities
                      .filter((e) => !hiddenTypes.has(e.type))
                      .map((e, i) => {
                        const cls =
                          ENTITY_TYPE_COLORS[e.type] || 'bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300'
                        return (
                          <span key={i} className={`rounded-md px-2 py-0.5 text-[11px] font-medium ${cls}`}>
                            {e.text}
                            <span className="ml-1 opacity-60">{e.mentions}</span>
                          </span>
                        )
                      })}
                  </div>
                </div>
              )}
            </div>
          ) : (
            <p className="text-sm text-(--color-text-secondary)">No statistics available</p>
          )}
        </div>
      )}
    </div>
  )
}

export default function DocumentDetailPage() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const { isAdmin, user } = useAuth()
  const [doc, setDoc] = useState<DocumentDetail | null>(null)
  const [content, setContent] = useState<ContentResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [showContent, setShowContent] = useState(false)
  const [actionLoading, setActionLoading] = useState(false)
  const [contentPage, setContentPage] = useState(1)
  const [pageSize, setPageSize] = useState(user?.preferences?.page_size || 10)
  const [highlightPage, setHighlightPage] = useState<number | null>(null)
  const contentLoadedRef = useRef(false)
  const [related, setRelated] = useState<
    { doc_id: string; title: string; summary: string | null; similarity: number }[]
  >([])

  // URL params for deep linking from search results
  const urlShowContent = searchParams.get('showContent') === 'true'
  const urlTargetPage = searchParams.get('page') ? Number(searchParams.get('page')) : null

  function loadDoc() {
    return get<DocumentDetail>(`/api/docs/${id}`)
      .then(setDoc)
      .catch((e) => setError(e.message))
  }

  useEffect(() => {
    loadDoc()
      .then(() => {
        // Persist for "continue viewing" on DocumentsPage
      })
      .finally(() => setLoading(false))
  }, [id])

  // Write to sessionStorage when doc loads for state preservation
  useEffect(() => {
    if (doc) {
      sessionStorage.setItem('lastDoc', JSON.stringify({ doc_id: doc.doc_id, title: doc.title }))
    }
  }, [doc])

  // Fetch related documents
  useEffect(() => {
    if (!id) return
    get<{ related: typeof related }>(`/api/docs/${id}/related`, { k: 5 })
      .then((data) => setRelated(data.related))
      .catch(() => {})
  }, [id])

  // SSE: live job updates
  const onJobEvent = useCallback(
    (event: JobEvent) => {
      setDoc((prev) => {
        if (!prev) return prev
        // Find the version this event belongs to
        const vIdx = prev.versions.findIndex((v) => v.version_id === event.version_id)
        if (vIdx === -1) return prev

        const versions = [...prev.versions]
        const version = { ...versions[vIdx], jobs: [...versions[vIdx].jobs] }
        versions[vIdx] = version

        // Find the matching job
        const jIdx = version.jobs.findIndex((j) => j.stage === event.stage)
        if (jIdx === -1) return prev

        const job = { ...version.jobs[jIdx] }
        job.status = event.status
        if (event.progress !== undefined) job.progress_current = event.progress
        if (event.total !== undefined) job.progress_total = event.total
        if (event.error) job.error = event.error
        if (event.status === 'done') job.finished_at = new Date().toISOString()
        if (event.status === 'running' && !job.started_at) job.started_at = new Date().toISOString()
        version.jobs[jIdx] = job

        // Check if all jobs are done → refresh from API for final state
        const allDone = version.jobs.every((j) => j.status === 'done')
        if (allDone) {
          // Async refresh — won't affect this render, next state update will
          loadDoc()
        }

        return { ...prev, versions }
      })
    },
    [id],
  )

  useJobEvents(onJobEvent)

  async function loadContent() {
    setShowContent(true)
    try {
      const data = await get<ContentResponse>(`/api/docs/${id}/content`)
      setContent(data)
      return data
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load content')
      return null
    }
  }

  // Auto-load content when arriving from search with ?showContent=true
  useEffect(() => {
    if (urlShowContent && !contentLoadedRef.current && !loading) {
      contentLoadedRef.current = true
      loadContent().then((data) => {
        if (data && urlTargetPage != null) {
          // Find the index of the target document page
          const pageIdx = data.pages.findIndex((p) => p.page_num === urlTargetPage)
          if (pageIdx >= 0) {
            const paginationPage = Math.floor(pageIdx / pageSize) + 1
            setContentPage(paginationPage)
            setHighlightPage(urlTargetPage)
            // Clear highlight after animation
            setTimeout(() => setHighlightPage(null), 2000)
          }
        }
      })
    }
  }, [urlShowContent, urlTargetPage, loading])

  const hasProcessing = doc?.versions.some((v) => v.status !== 'ready' && v.status !== 'error')

  async function handleCancel() {
    setActionLoading(true)
    try {
      await post(`/api/docs/${id}/cancel`)
      const updated = await get<DocumentDetail>(`/api/docs/${id}`)
      setDoc(updated)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Cancel failed')
    } finally {
      setActionLoading(false)
    }
  }

  async function handleReprocess() {
    setActionLoading(true)
    try {
      await post(`/api/docs/${id}/reprocess`)
      // Reload doc
      const updated = await get<DocumentDetail>(`/api/docs/${id}`)
      setDoc(updated)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Reprocess failed')
    } finally {
      setActionLoading(false)
    }
  }

  async function handleDelete() {
    if (!confirm('Delete this document and all its versions? This cannot be undone.')) return
    setActionLoading(true)
    try {
      await del(`/api/docs/${id}`)
      navigate('/')
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Delete failed')
      setActionLoading(false)
    }
  }

  if (loading) return <div className="text-gray-500 dark:text-gray-400">Loading...</div>
  if (error && !doc) return <div className="text-red-600 dark:text-red-400">Error: {error}</div>
  if (!doc) return <div className="text-gray-500 dark:text-gray-400">Not found</div>

  // Version numbering: API returns newest first, but "Version 1" = oldest
  const versionsWithNumber = doc.versions.map((v, i) => ({
    ...v,
    versionNumber: doc.versions.length - i,
  }))

  const allVersionsReady = doc.versions.every((v) => v.status === 'ready')
  const versionCount = doc.versions.length
  const versionLabel = `${versionCount} version${versionCount !== 1 ? 's' : ''}`

  return (
    <div>
      <div className="mb-4 flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold">{doc.title}</h1>
          {doc.canonical_filename && (
            <p className="text-sm text-gray-500 dark:text-gray-400">{doc.canonical_filename}</p>
          )}
        </div>
        {isAdmin && (
          <div className="flex space-x-2">
            {hasProcessing && (
              <button
                onClick={handleCancel}
                disabled={actionLoading}
                className="rounded-lg bg-gray-500 px-3 py-1.5 text-sm font-medium text-white shadow-xs hover:bg-gray-600 disabled:opacity-50"
              >
                Cancel Processing
              </button>
            )}
            <button
              onClick={handleReprocess}
              disabled={actionLoading}
              className="rounded-lg bg-amber-500 px-3 py-1.5 text-sm font-medium text-white shadow-xs hover:bg-amber-600 disabled:opacity-50"
            >
              Reprocess
            </button>
            <button
              onClick={handleDelete}
              disabled={actionLoading}
              className="rounded-lg bg-red-500/10 px-3 py-1.5 text-sm font-medium text-red-600 hover:bg-red-500/20 disabled:opacity-50"
            >
              Delete
            </button>
          </div>
        )}
      </div>

      {error && (
        <div className="mb-4 rounded-sm bg-red-50 dark:bg-red-900/20 px-3 py-2 text-sm text-red-700 dark:text-red-400">
          {error}
        </div>
      )}

      <div className="mb-2 text-sm text-gray-500 dark:text-gray-400">
        Created {new Date(doc.created_at).toLocaleString()} | Updated {new Date(doc.updated_at).toLocaleString()}
      </div>

      <DocumentStatsDisclosure docId={doc.doc_id} />

      {related.length > 0 && (
        <details className="group mt-4 rounded-xl bg-white dark:bg-[#2c2c2e] shadow-mac" open>
          <summary className="cursor-pointer px-5 py-3 text-sm font-semibold text-(--color-text-primary)">
            Related Documents ({related.length})
          </summary>
          <div className="border-t border-(--color-border) px-5 py-3 space-y-1">
            {related.map((r) => (
              <Link
                key={r.doc_id}
                to={`/docs/${r.doc_id}`}
                className="block rounded-lg px-3 py-2 hover:bg-gray-50 dark:hover:bg-gray-700/50 transition-colors"
              >
                <div className="flex items-center justify-between">
                  <span className="text-sm font-medium text-(--color-text-primary) truncate">{r.title}</span>
                  <span className="ml-2 shrink-0 text-xs tabular-nums text-gray-400">
                    {Math.round(r.similarity * 100)}% match
                  </span>
                </div>
                {r.summary && (
                  <p className="mt-0.5 text-xs text-gray-500 dark:text-gray-400 line-clamp-2">{r.summary}</p>
                )}
              </Link>
            ))}
          </div>
        </details>
      )}

      <div className="mt-6 mb-6">
        <Disclosure
          label={<span className="text-lg font-semibold">{versionLabel}</span>}
          defaultOpen={!allVersionsReady}
        >
          <div className="space-y-4">
            {versionsWithNumber.map((v) => {
              const versionNotReady = v.status !== 'ready'
              return (
                <div key={v.version_id} className="rounded-xl bg-white dark:bg-[#2c2c2e] shadow-mac p-5">
                  <div className="mb-2 text-sm font-semibold text-gray-800 dark:text-gray-200">
                    Version {v.versionNumber}{' '}
                    <span className="font-normal text-gray-500 dark:text-gray-400">
                      ({new Date(v.created_at).toLocaleDateString()})
                    </span>
                  </div>
                  <VersionBanner version={v} />
                  <div className="mb-2 flex items-center justify-between">
                    <div className="text-sm">
                      <span className="font-medium">{v.mime_type || 'unknown type'}</span>
                      {v.size_bytes != null && (
                        <span className="ml-2 text-gray-500 dark:text-gray-400">
                          {(v.size_bytes / 1024).toFixed(0)} KB
                        </span>
                      )}
                    </div>
                    <JobStatusBadge status={v.status} />
                  </div>
                  {v.error && <div className="mb-2 text-sm text-red-600 dark:text-red-400">Error: {v.error}</div>}
                  <div className="text-xs text-gray-400">
                    {v.extracted_chars != null && <span>Chars: {v.extracted_chars} | </span>}
                    OCR: {v.needs_ocr ? 'yes' : 'no'} | Text layer: {v.has_text_layer ? 'yes' : 'no'}
                  </div>
                  {v.source_path && <div className="mt-1 text-xs text-gray-400 break-all">Source: {v.source_path}</div>}

                  {v.jobs.length > 0 && (
                    <div className="mt-3">
                      <Disclosure label="Ingestion Jobs" defaultOpen={versionNotReady}>
                        <table className="w-full text-sm">
                          <thead>
                            <tr className="border-b border-gray-200 dark:border-gray-700 text-left text-xs text-gray-500 dark:text-gray-400">
                              <th className="pb-1 pr-3">Stage</th>
                              <th className="pb-1 pr-3">Status</th>
                              <th className="pb-1 pr-3">Progress</th>
                              <th className="pb-1">Time</th>
                            </tr>
                          </thead>
                          <tbody>
                            {v.jobs.map((j) => (
                              <tr key={j.job_id} className="border-b border-gray-100 dark:border-gray-700">
                                <td className="py-1 pr-3 font-medium">{j.stage}</td>
                                <td className="py-1 pr-3">
                                  <JobStatusBadge status={j.status} />
                                </td>
                                <td className="py-1 pr-3 text-gray-500 dark:text-gray-400">
                                  {j.progress_total ? `${j.progress_current || 0}/${j.progress_total}` : '\u2014'}
                                </td>
                                <td className="py-1 text-xs text-gray-400">
                                  {j.finished_at
                                    ? new Date(j.finished_at).toLocaleTimeString()
                                    : j.started_at
                                      ? 'running...'
                                      : 'queued'}
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </Disclosure>
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        </Disclosure>
      </div>

      <h2 className="mb-2 mt-6 text-lg font-semibold">Content</h2>
      {!showContent ? (
        <button
          onClick={loadContent}
          className="rounded-lg bg-(--color-bg-tertiary) px-4 py-2 text-sm font-medium text-(--color-text-secondary) hover:opacity-80"
        >
          View Content
        </button>
      ) : content ? (
        (() => {
          const totalPages = Math.max(1, Math.ceil(content.pages.length / pageSize))
          const clampedPage = Math.min(contentPage, totalPages)
          const startIdx = (clampedPage - 1) * pageSize
          const visiblePages = content.pages.slice(startIdx, startIdx + pageSize)

          return (
            <div className="space-y-3">
              <div className="flex items-center justify-between">
                <p className="text-sm text-gray-500 dark:text-gray-400">
                  {content.total_chars.toLocaleString()} total characters, {content.pages.length} pages
                </p>
                <div className="flex items-center gap-2 text-sm">
                  <label htmlFor="page-size" className="text-gray-500 dark:text-gray-400">
                    Show
                  </label>
                  <select
                    id="page-size"
                    value={pageSize}
                    onChange={(e) => {
                      setPageSize(Number(e.target.value))
                      setContentPage(1)
                    }}
                    className="rounded-lg border-0 bg-(--color-bg-secondary) dark:bg-(--color-bg-tertiary) shadow-mac px-2 py-1 text-sm text-gray-900 dark:text-gray-100"
                  >
                    {PAGE_SIZE_OPTIONS.map((n) => (
                      <option key={n} value={n}>
                        {n}
                      </option>
                    ))}
                  </select>
                  <span className="text-gray-500 dark:text-gray-400">per page</span>
                </div>
              </div>

              {totalPages > 1 && (
                <div className="flex items-center justify-between text-sm text-gray-500 dark:text-gray-400">
                  <span>
                    Pages {startIdx + 1}&ndash;
                    {Math.min(startIdx + pageSize, content.pages.length)} of {content.pages.length}
                  </span>
                  <Pagination currentPage={clampedPage} totalPages={totalPages} onPageChange={setContentPage} />
                </div>
              )}

              {visiblePages.map((page) => (
                <div
                  key={page.page_num}
                  className={`rounded-xl bg-white dark:bg-[#2c2c2e] shadow-mac p-4 transition-all duration-500 ${
                    highlightPage === page.page_num ? 'ring-2 ring-(--color-accent)/40' : ''
                  }`}
                >
                  <div className="mb-2 flex items-center justify-between">
                    <span className="text-sm font-medium text-gray-700 dark:text-gray-300">Page {page.page_num}</span>
                    {page.ocr_used && (
                      <span className="text-xs text-gray-400">
                        OCR {page.ocr_confidence != null ? `(${(page.ocr_confidence * 100).toFixed(0)}%)` : ''}
                      </span>
                    )}
                  </div>
                  <pre className="whitespace-pre-wrap text-sm text-gray-800 dark:text-gray-200">{page.text}</pre>
                </div>
              ))}

              {totalPages > 1 && (
                <div className="flex justify-end">
                  <Pagination currentPage={clampedPage} totalPages={totalPages} onPageChange={setContentPage} />
                </div>
              )}
            </div>
          )
        })()
      ) : (
        <div className="text-gray-500 dark:text-gray-400">Loading content...</div>
      )}
    </div>
  )
}

import { ReactNode, useCallback, useEffect, useRef, useState } from 'react'
import { useParams, useNavigate, useSearchParams, useLocation, Link } from 'react-router-dom'
import { get, post, del } from '../api'
import { useAuth } from '../auth'
import { useJobEvents, type JobEvent } from '../hooks/useJobEvents'
import { useSystemConfig, type SystemConfig } from '../hooks/useSystemConfig'
import { ENTITY_TYPE_LABELS } from '../components/stats/CorpusCharts'
import { DEFAULT_HIDDEN_ENTITY_TYPES, entityTypeClass } from '../utils/entityTypeColors'
import { PageHeader } from '../components/PageHeader'
import { Card } from '../components/Card'
import { StatusPill, type PillState } from '../components/StatusPill'
import { IconTile } from '../components/IconTile'
import { documentTypeIcon } from '../utils/documentTypeIcon'
import EmailMetadataSection from '../components/EmailMetadataSection'

interface JobInfo {
  job_id: string
  stage: string
  status: string
  progress_current?: number | null
  progress_total?: number | null
  error?: string | null
  created_at: string
  started_at?: string | null
  finished_at?: string | null
}

interface DocumentDetail {
  doc_id: string
  title: string
  canonical_filename: string | null
  status: string
  pipeline_status: string | null
  pipeline_seq: number | null
  summary: string | null
  doc_type: string | null
  mime_type: string | null
  source_path: string | null
  has_text_layer: boolean | null
  needs_ocr: boolean | null
  extracted_chars: number | null
  size_bytes: number | null
  ocr_languages_used: string[] | null
  error: string | null
  created_at: string
  updated_at: string
  jobs: JobInfo[]
  email_message_id?: string | null
  email_thread_id?: string | null
  email_parent_doc_id?: string | null
  email_from_address?: string | null
  email_from_name?: string | null
  email_to_addresses?: string[] | null
  email_cc_addresses?: string[] | null
  email_date_sent?: string | null
  email_label_path?: string | null
}

interface PageContent {
  page_num: number
  text: string
  ocr_used: boolean
  ocr_confidence?: number
}

interface ContentResponse {
  doc_id: string
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

function stageName(stage: string): string {
  switch (stage) {
    case 'extract':
      return 'Extract'
    case 'ocr':
      return 'OCR'
    case 'chunk':
      return 'Chunk'
    case 'entities':
      return 'Entities'
    case 'embed':
      return 'Embed'
    case 'summarize':
      return 'Summarize'
    case 'finalize':
      return 'Finalize'
    default:
      return stage
  }
}

function jobPillState(status: string): PillState {
  if (status === 'done') return 'active'
  if (status === 'running') return 'running'
  if (status === 'error') return 'error'
  if (status === 'skipped') return 'idle'
  return 'pending'
}

function jobProgressLabel(job: JobInfo): string {
  if (job.progress_total) return `${job.progress_current || 0}/${job.progress_total}`
  if (job.status !== 'skipped') return '—'
  if (job.error) return job.error
  if (job.stage === 'ocr') return 'not needed'
  if (job.stage === 'summarize') return 'skipped by maintenance'
  return 'skipped'
}

function jobTimeLabel(job: JobInfo): string {
  if (job.finished_at) return new Date(job.finished_at).toLocaleTimeString()
  if (job.status === 'skipped') return 'skipped'
  if (job.started_at) return 'running...'
  return 'queued'
}

function DocStatusBanner({ doc }: { doc: DocumentDetail }) {
  const hasJobs = doc.jobs.length > 0
  const allDone = hasJobs && doc.jobs.every((j) => j.status === 'done' || j.status === 'skipped')
  const hasError = doc.jobs.some((j) => j.status === 'error')
  const runningJob = doc.jobs.find((j) => j.status === 'running' || j.status === 'queued')

  if (doc.pipeline_status === 'ready' || allDone) {
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
    const errorJob = doc.jobs.find((j) => j.status === 'error')
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
    const doneCount = doc.jobs.filter((j) => j.status === 'done' || j.status === 'skipped').length
    return (
      <div className="mb-3 flex items-center gap-2 rounded-lg bg-amber-50 dark:bg-amber-900/20 px-3 py-2 text-sm text-amber-700 dark:text-amber-400">
        <div className="h-3 w-3 animate-spin rounded-full border-2 border-current border-t-transparent" />
        Processing — stage {doneCount + 1} of {doc.jobs.length}
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

// Display name lookup for the small subset of languages we currently
// surface in the mismatch banner. Falls back to the ISO code when a
// new language is added on the backend before the frontend is updated.
const LANGUAGE_DISPLAY_NAMES: Record<string, string> = {
  en: 'English',
  fr: 'French',
  de: 'German',
  es: 'Spanish',
  it: 'Italian',
  nl: 'Dutch',
  pt: 'Portuguese',
}

function languageDisplay(code: string): string {
  return LANGUAGE_DISPLAY_NAMES[code] ?? code
}

/**
 * Banner shown on the doc detail page when the operator has more
 * languages enabled than the doc was OCR'd with. Surfaces a one-click
 * reprocess. Only renders when:
 *   - The doc was OCR'd at all (ocr_languages_used is non-null) — non-OCR
 *     docs don't benefit from re-running OCR.
 *   - The set of enabled languages strictly extends what was used —
 *     otherwise nothing would change.
 *   - The viewer is admin (only admins can reprocess).
 *
 * For legacy docs (ocr_languages_used = null because they were ingested
 * before that column existed), we don't second-guess. The operator can
 * still hit the "Reprocess" button manually.
 */
function LanguageMismatchBanner({
  doc,
  enabledLanguages,
  canReprocess,
  onReprocess,
  actionLoading,
}: {
  doc: DocumentDetail
  enabledLanguages: string[]
  canReprocess: boolean
  onReprocess: () => void
  actionLoading: boolean
}) {
  if (!canReprocess) return null
  if (!doc.ocr_languages_used || doc.ocr_languages_used.length === 0) return null

  const used = new Set(doc.ocr_languages_used)
  const missing = enabledLanguages.filter((c) => !used.has(c))
  if (missing.length === 0) return null

  const missingNames = missing.map(languageDisplay).join(', ')
  return (
    <div className="mb-3 flex items-start justify-between gap-3 rounded-lg bg-blue-50 dark:bg-blue-900/20 px-3 py-2 text-sm text-blue-700 dark:text-blue-400">
      <div className="flex-1 min-w-0">
        <div className="font-medium">
          OCR'd in {doc.ocr_languages_used.map(languageDisplay).join(', ')}, but {missingNames}{' '}
          {missing.length === 1 ? 'is' : 'are'} now enabled.
        </div>
        <div className="mt-0.5 text-[12px] opacity-80">
          Reprocess to extract text in the additional language{missing.length > 1 ? 's' : ''}.
        </div>
      </div>
      <button
        type="button"
        onClick={onReprocess}
        disabled={actionLoading}
        className="shrink-0 rounded-md bg-blue-600 px-3 py-1 text-[12px] font-medium text-white shadow-xs hover:bg-blue-700 disabled:opacity-50"
      >
        Reprocess
      </button>
    </div>
  )
}

/**
 * Source file row: shows the path and exposes whichever of "Reveal in Finder"
 * and "Download" the deployment supports.
 *
 * Reveal: only available when running inside the macOS WKWebView client (the
 * native bridge `window.harborclerk.revealInFinder` is detected synchronously
 * — absent in regular browsers and in the Docker web UI). Doesn't go through
 * the HTTP server at all; calls NSWorkspace directly via the WKScriptMessage
 * channel registered in macos/HarborClerk/.../ContentView.swift.
 *
 * Download: gated by the server-side `allow_source_download` setting. Default
 * off everywhere — see hooks/useSystemConfig.ts and the backend setting
 * docstring for the rationale.
 *
 * If neither action is available, we still show the path because operators
 * frequently want to confirm "this is the right file".
 */
function SourceFileSection({ doc, sysConfig }: { doc: DocumentDetail; sysConfig: SystemConfig }) {
  // Anything to display? Watched-folder docs always have source_path; uploaded
  // docs may have only original_object_key (which is internal storage and not
  // particularly useful to the user — we don't surface it as a "source path").
  if (!doc.source_path) return null

  const canReveal = typeof window !== 'undefined' && Boolean(window.harborclerk?.revealInFinder)
  const canDownload = sysConfig.allowSourceDownload && sysConfig.loaded

  async function handleReveal() {
    try {
      await window.harborclerk?.revealInFinder?.(doc.source_path!)
    } catch {
      // Swift handler logs its own errors; nothing actionable in the UI.
    }
  }

  return (
    <div className="mb-2 flex items-start justify-between gap-3">
      <div className="min-w-0 flex-1">
        <div className="text-xs text-gray-500 dark:text-gray-400">Source</div>
        <div className="text-xs text-gray-400 break-all font-mono">{doc.source_path}</div>
      </div>
      {(canReveal || canDownload) && (
        <div className="flex shrink-0 items-center gap-2">
          {canReveal && (
            <button
              type="button"
              onClick={handleReveal}
              className="rounded-md bg-(--color-bg-tertiary) px-2.5 py-1 text-xs font-medium text-(--color-text-secondary) hover:opacity-80"
              title="Reveal this file in Finder"
            >
              Reveal in Finder
            </button>
          )}
          {canDownload && (
            <a
              href={`/api/docs/${doc.doc_id}/download`}
              download
              className="rounded-md bg-(--color-bg-tertiary) px-2.5 py-1 text-xs font-medium text-(--color-text-secondary) hover:opacity-80"
              title="Download the original file"
            >
              Download
            </a>
          )}
        </div>
      )}
    </div>
  )
}

/**
 * Wrap the chunk-text substring inside a page's text with a `<mark>` tag so
 * the user lands on the exact span that matched their search. Falls back to
 * the unmodified text when:
 *   - no highlight target was passed (normal navigation, not from search)
 *   - the chunk text spans multiple pages and doesn't fully appear in any
 *     single page (partial-match fallback isn't worth the complexity)
 */
function renderPageWithHighlight(text: string, highlight: string | null): ReactNode {
  if (!highlight) return text
  const trimmed = highlight.trim()
  if (!trimmed) return text
  const idx = text.indexOf(trimmed)
  if (idx === -1) return text
  return (
    <>
      {text.slice(0, idx)}
      <mark className="rounded-sm bg-yellow-200 px-0.5 dark:bg-yellow-700/50 dark:text-yellow-100">{trimmed}</mark>
      {text.slice(idx + trimmed.length)}
    </>
  )
}

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
        <Card className="mt-2 p-4 mb-4">
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
                            title={ENTITY_TYPE_LABELS[type] ?? type}
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
                                : entityTypeClass(type)
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
                        const cls = entityTypeClass(e.type)
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
        </Card>
      )}
    </div>
  )
}

export default function DocumentDetailPage() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const location = useLocation()
  const { isAdmin, user } = useAuth()
  const sysConfig = useSystemConfig()

  // Optional inline highlight target — populated when the user arrives from a
  // search result. The chunk text is passed via React Router state (not URL)
  // because chunk bodies can be long and noisy in the address bar.
  const highlightChunkText = (location.state as { highlightChunkText?: string } | null)?.highlightChunkText ?? null
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
    {
      doc_id: string
      title: string
      summary: string | null
      similarity: number
      doc_type?: string | null
      mime_type?: string | null
      canonical_filename?: string | null
    }[]
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
        // Only handle events for this document
        if (event.doc_id && event.doc_id !== prev.doc_id) return prev

        const jobs = [...prev.jobs]
        const jIdx = jobs.findIndex((j) => j.stage === event.stage)

        if (jIdx === -1) {
          // New job not in initial response — append
          jobs.push({
            job_id: '',
            stage: event.stage,
            status: event.status,
            progress_current: event.progress ?? null,
            progress_total: event.total ?? null,
            error: event.error ?? null,
            created_at: new Date().toISOString(),
            started_at: event.status === 'running' ? new Date().toISOString() : null,
            finished_at: event.status === 'done' ? new Date().toISOString() : null,
          })
        } else {
          const job = { ...jobs[jIdx] }
          job.status = event.status
          if (event.progress !== undefined) job.progress_current = event.progress
          if (event.total !== undefined) job.progress_total = event.total
          if (event.error) job.error = event.error
          if (event.status === 'done') job.finished_at = new Date().toISOString()
          if (event.status === 'running' && !job.started_at) job.started_at = new Date().toISOString()
          jobs[jIdx] = job
        }

        // Check if all jobs are done → refresh from API for final state
        const allDone = jobs.every((j) => j.status === 'done' || j.status === 'skipped')
        if (allDone) {
          // Async refresh — won't affect this render, next state update will
          loadDoc()
        }

        return { ...prev, jobs }
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
            // Scroll to the page anchor once it's rendered. Two RAFs let
            // pagination + the page DOM commit before we measure positions.
            requestAnimationFrame(() => {
              requestAnimationFrame(() => {
                document.getElementById(`page-${urlTargetPage}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
              })
            })
            // Clear highlight after animation
            setTimeout(() => setHighlightPage(null), 2000)
          }
        }
      })
    }
  }, [urlShowContent, urlTargetPage, loading])

  const isReady =
    doc?.pipeline_status === 'ready' ||
    (doc?.jobs.length ? doc.jobs.every((j) => j.status === 'done' || j.status === 'skipped') : false)

  const hasProcessing = doc && !isReady && doc.pipeline_status !== 'error'

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
      const updated = await get<DocumentDetail>(`/api/docs/${id}`)
      setDoc(updated)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Reprocess failed')
    } finally {
      setActionLoading(false)
    }
  }

  async function handleDelete() {
    if (!confirm('Delete this document? This cannot be undone.')) return
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

  return (
    <div>
      <div className="mb-4 flex items-start gap-3">
        <IconTile hue={documentTypeIcon(doc).hue} size={36}>
          {documentTypeIcon(doc).glyph}
        </IconTile>
        <div className="flex-1">
          <PageHeader
            title={doc.title}
            subtitle={doc.canonical_filename ?? undefined}
            actions={
              isAdmin ? (
                <>
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
                </>
              ) : undefined
            }
          />
        </div>
      </div>

      {error && (
        <div className="mb-4 rounded-sm bg-red-50 dark:bg-red-900/20 px-3 py-2 text-sm text-red-700 dark:text-red-400">
          {error}
        </div>
      )}

      <div className="mb-2 text-sm text-gray-500 dark:text-gray-400">
        Created {new Date(doc.created_at).toLocaleString()} | Updated {new Date(doc.updated_at).toLocaleString()}
      </div>

      <DocStatusBanner doc={doc} />

      <LanguageMismatchBanner
        doc={doc}
        enabledLanguages={user?.preferences?.enabled_languages ?? ['en']}
        canReprocess={isAdmin}
        onReprocess={handleReprocess}
        actionLoading={actionLoading}
      />

      {doc.jobs.length > 0 && (
        <div className="mb-4">
          <Disclosure label="Ingestion Jobs" defaultOpen={!isReady}>
            <Card className="p-3">
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
                  {doc.jobs.map((j) => {
                    const pillState = jobPillState(j.status)
                    const progressLabel = jobProgressLabel(j)
                    return (
                      <tr key={j.job_id || j.stage} className="border-b border-gray-100 dark:border-gray-700">
                        <td className="py-1 pr-3 font-medium">{stageName(j.stage)}</td>
                        <td className="py-1 pr-3">
                          <StatusPill state={pillState} label={j.status} />
                        </td>
                        <td
                          className="py-1 pr-3 text-gray-500 dark:text-gray-400"
                          title={progressLabel.length > 48 ? progressLabel : undefined}
                        >
                          {progressLabel}
                        </td>
                        <td className="py-1 text-xs text-gray-400">{jobTimeLabel(j)}</td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </Card>
          </Disclosure>
        </div>
      )}

      {/* File metadata line */}
      <div className="mb-2 flex items-center justify-between">
        <div className="text-sm">
          <span className="font-medium">{doc.mime_type || 'unknown type'}</span>
          {doc.size_bytes != null && (
            <span className="ml-2 text-gray-500 dark:text-gray-400">{(doc.size_bytes / 1024).toFixed(0)} KB</span>
          )}
        </div>
        <JobStatusBadge status={doc.status} />
      </div>

      {doc.error && <div className="mb-2 text-sm text-red-600 dark:text-red-400">Error: {doc.error}</div>}

      <div className="text-xs text-gray-400 mb-1">
        {doc.extracted_chars != null && <span>Chars: {doc.extracted_chars} | </span>}
        OCR: {doc.needs_ocr ? 'yes' : 'no'} | Text layer: {doc.has_text_layer ? 'yes' : 'no'}
      </div>

      <SourceFileSection doc={doc} sysConfig={sysConfig} />

      <EmailMetadataSection
        emailMessageId={doc.email_message_id}
        emailFromAddress={doc.email_from_address}
        emailFromName={doc.email_from_name}
        emailToAddresses={doc.email_to_addresses}
        emailCcAddresses={doc.email_cc_addresses}
        emailDateSent={doc.email_date_sent}
        emailLabelPath={doc.email_label_path}
        emailParentDocId={doc.email_parent_doc_id}
      />

      <DocumentStatsDisclosure docId={doc.doc_id} />

      {related.length > 0 && (
        <Card className="mt-4 mb-4">
          <div className="px-5 py-3 text-sm font-semibold text-(--color-text-primary)">
            Related Documents ({related.length})
          </div>
          <div className="border-t border-(--color-border) px-5 py-3 space-y-1">
            {related.map((r) => {
              const icon = documentTypeIcon(r)
              return (
                <Link
                  key={r.doc_id}
                  to={`/docs/${r.doc_id}`}
                  className="flex items-start gap-2 rounded-lg px-3 py-2 hover:bg-gray-50 dark:hover:bg-gray-700/50 transition-colors"
                >
                  <IconTile hue={icon.hue} size={24}>
                    {icon.glyph}
                  </IconTile>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center justify-between">
                      <span className="text-sm font-medium text-(--color-text-primary) truncate">{r.title}</span>
                      <span className="ml-2 shrink-0 text-xs tabular-nums text-gray-400">
                        {Math.round(r.similarity * 100)}% match
                      </span>
                    </div>
                    {r.summary && (
                      <p className="mt-0.5 text-xs text-gray-500 dark:text-gray-400 line-clamp-2">{r.summary}</p>
                    )}
                  </div>
                </Link>
              )
            })}
          </div>
        </Card>
      )}

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
                <Card
                  key={page.page_num}
                  id={`page-${page.page_num}`}
                  className={`p-4 transition-all duration-500 ${
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
                  <pre className="whitespace-pre-wrap text-sm text-gray-800 dark:text-gray-200">
                    {renderPageWithHighlight(page.text, highlightChunkText)}
                  </pre>
                </Card>
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

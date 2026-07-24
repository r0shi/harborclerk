import { FormEvent, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router'
import { get, post } from '../api'
import { Card } from '../components/Card'
import { FolderPicker } from '../components/FolderPicker'
import { PageHeader } from '../components/PageHeader'
import { SourceCitation } from '../components/SourceCitation'
import { useWatchedFolders } from '../hooks/useWatchedFolders'
import type { SourceRef } from '../types/sourceRef'

type SearchMode = 'search' | 'find_all'
type FindAllSort = 'relevance' | 'date_desc' | 'date_asc'

interface FilterOption {
  value: string
  count: number
}

interface SearchFilterOptions {
  mime_types: FilterOption[]
}

interface SearchFilters {
  textContains: string
  after: string
  before: string
  language: string
  mimeType: string
  emailFrom: string
  emailTo: string
  emailCc: string
  emailSubject: string
  docId: string
  summaryState: string
  pipelineStatus: string
  jobIssue: string
}

interface SearchHit {
  chunk_id: string
  doc_id: string
  chunk_num: number
  chunk_text: string
  page_start?: number
  page_end?: number
  language: string
  ocr_used: boolean
  ocr_confidence?: number
  score: number
  doc_title?: string
  source?: SourceRef
  citation?: string
}

interface ConflictSource {
  doc_id: string
  title: string
}

interface SearchResponse {
  hits: SearchHit[]
  total_candidates: number
  has_more: boolean
  possible_conflict: boolean
  conflict_sources: ConflictSource[]
}

interface FindAllTopChunk {
  chunk_id?: string
  text?: string
  page?: number
  heading?: string
}

interface FindAllHit {
  doc_id: string
  doc_title: string
  mime_type: string
  language?: string
  score: number
  ingested_at?: string
  page_range?: string
  top_chunk?: FindAllTopChunk
  source?: SourceRef
  citation?: string
}

interface FindAllResponse {
  results: FindAllHit[]
  total_matches: number
  returned: number
  offset: number
  truncated: boolean
  sort_by: FindAllSort
  presentation: 'brief' | 'full'
}

type SearchResults = SearchResponse | FindAllResponse

const HISTORY_KEY = 'search_history'
const STATE_KEY = 'search_state'
const MAX_HISTORY = 10
const PAGE_SIZES = [10, 25, 50]
const EMPTY_FILTERS: SearchFilters = {
  textContains: '',
  after: '',
  before: '',
  language: '',
  mimeType: '',
  emailFrom: '',
  emailTo: '',
  emailCc: '',
  emailSubject: '',
  docId: '',
  summaryState: '',
  pipelineStatus: '',
  jobIssue: '',
}

const MIME_TYPE_LABELS: Record<string, string> = {
  'application/pdf': 'PDF',
  'application/rtf': 'RTF',
  'application/vnd.ms-excel': 'Excel',
  'application/vnd.ms-powerpoint': 'PowerPoint',
  'application/vnd.openxmlformats-officedocument.presentationml.presentation': 'PowerPoint',
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 'Excel',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'Word',
  'application/msword': 'Word',
  'message/rfc822': 'Email',
  'text/html': 'HTML',
  'text/markdown': 'Markdown',
  'text/plain': 'Plain text',
}

const SUMMARY_STATE_LABELS: Record<string, string> = {
  has: 'Has summary',
  missing: 'Missing summary',
  pending: 'Summary pending',
  failed: 'Summary failed',
}

const PIPELINE_STATUS_LABELS: Record<string, string> = {
  ready: 'Ready',
  processing: 'Processing',
  error: 'Failed',
}

const JOB_ISSUE_LABELS: Record<string, string> = {
  any_issue: 'Any ingest issue',
  failed_job: 'Any failed stage',
  ocr_failed: 'OCR failed',
  entity_skipped: 'Entity extraction skipped',
  summary_failed: 'Summary failed',
  summary_blocked: 'Summary paused',
  summary_pending: 'Summary pending',
  status_cleanup: 'Status cleanup needed',
}

interface SearchState {
  mode?: SearchMode
  query: string
  results: SearchResults | null
  currentPage: number
  pageSize: number
  lastQuery: string
  filters?: SearchFilters
  sortBy?: FindAllSort
}

function saveSearchState(state: SearchState) {
  try {
    sessionStorage.setItem(STATE_KEY, JSON.stringify(state))
  } catch {
    // ignore quota errors
  }
}

function loadSearchState(): SearchState | null {
  try {
    const raw = sessionStorage.getItem(STATE_KEY)
    if (!raw) return null
    return JSON.parse(raw)
  } catch {
    return null
  }
}

function getHistory(): string[] {
  try {
    const raw = localStorage.getItem(HISTORY_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw)
    return Array.isArray(parsed) ? parsed : []
  } catch {
    return []
  }
}

function saveHistory(history: string[]) {
  localStorage.setItem(HISTORY_KEY, JSON.stringify(history))
}

function addToHistory(query: string) {
  const trimmed = query.trim()
  if (!trimmed) return
  const history = getHistory().filter((q) => q !== trimmed)
  history.unshift(trimmed)
  saveHistory(history.slice(0, MAX_HISTORY))
}

function isFindAllResponse(results: SearchResults | null): results is FindAllResponse {
  return !!results && 'results' in results
}

function firstPageFromRange(pageRange?: string): number | null {
  if (!pageRange) return null
  const match = pageRange.match(/^\d+/)
  return match ? Number(match[0]) : null
}

function mimeTypeLabel(value: string): string {
  if (MIME_TYPE_LABELS[value]) return MIME_TYPE_LABELS[value]
  const subtype = value.split('/').pop()
  if (!subtype) return value
  return subtype
    .replace(/^vnd\./, '')
    .replace(/\./g, ' ')
    .replace(/[-_]/g, ' ')
    .replace(/\b\w/g, (char) => char.toUpperCase())
}

function mimeTypeOptionLabel(option: FilterOption): string {
  return `${mimeTypeLabel(option.value)} (${option.count})`
}

function normalizedFilters(filters?: Partial<SearchFilters>): SearchFilters {
  return { ...EMPTY_FILTERS, ...filters }
}

function optionLabel(labels: Record<string, string>, value: string): string {
  return labels[value] || value
}

function isUuid(value: string): boolean {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value)
}

function buildMetadataFilter(filters: SearchFilters): Record<string, string> {
  const metadataFilter: Record<string, string> = {}
  const emailFrom = filters.emailFrom.trim()
  const emailTo = filters.emailTo.trim()
  const emailCc = filters.emailCc.trim()
  const emailSubject = filters.emailSubject.trim()
  if (emailFrom) metadataFilter['email.from_address'] = emailFrom
  if (emailTo) metadataFilter['email.to_addresses'] = emailTo
  if (emailCc) metadataFilter['email.cc_addresses'] = emailCc
  if (emailSubject) metadataFilter['email.subject_contains'] = emailSubject
  return metadataFilter
}

function buildFilterPayload(filters: SearchFilters) {
  const metadataFilter = buildMetadataFilter(filters)
  return {
    ...(filters.docId.trim() && { doc_id: filters.docId.trim() }),
    ...(filters.textContains.trim() && { text_contains: filters.textContains.trim() }),
    ...(filters.after && { after: filters.after }),
    ...(filters.before && { before: filters.before }),
    ...(filters.language && { language: filters.language }),
    ...(filters.mimeType.trim() && { mime_type: filters.mimeType.trim() }),
    ...(filters.summaryState && { summary_state: filters.summaryState }),
    ...(filters.pipelineStatus && { pipeline_status: filters.pipelineStatus }),
    ...(filters.jobIssue && { job_issue: filters.jobIssue }),
    ...(Object.keys(metadataFilter).length > 0 && { metadata_filter: metadataFilter }),
  }
}

function activeFilterEntries(filters: SearchFilters): Array<{ key: keyof SearchFilters; label: string }> {
  const entries: Array<{ key: keyof SearchFilters; label: string }> = []
  if (filters.textContains.trim()) entries.push({ key: 'textContains', label: `Text: ${filters.textContains.trim()}` })
  if (filters.after) entries.push({ key: 'after', label: `After: ${filters.after}` })
  if (filters.before) entries.push({ key: 'before', label: `Before: ${filters.before}` })
  if (filters.language) entries.push({ key: 'language', label: `Language: ${filters.language}` })
  if (filters.mimeType.trim())
    entries.push({ key: 'mimeType', label: `Type: ${mimeTypeLabel(filters.mimeType.trim())}` })
  if (filters.summaryState)
    entries.push({ key: 'summaryState', label: `Summary: ${optionLabel(SUMMARY_STATE_LABELS, filters.summaryState)}` })
  if (filters.pipelineStatus)
    entries.push({
      key: 'pipelineStatus',
      label: `Pipeline: ${optionLabel(PIPELINE_STATUS_LABELS, filters.pipelineStatus)}`,
    })
  if (filters.jobIssue)
    entries.push({ key: 'jobIssue', label: `Issue: ${optionLabel(JOB_ISSUE_LABELS, filters.jobIssue)}` })
  if (filters.emailFrom.trim()) entries.push({ key: 'emailFrom', label: `From: ${filters.emailFrom.trim()}` })
  if (filters.emailTo.trim()) entries.push({ key: 'emailTo', label: `To: ${filters.emailTo.trim()}` })
  if (filters.emailCc.trim()) entries.push({ key: 'emailCc', label: `Cc: ${filters.emailCc.trim()}` })
  if (filters.emailSubject.trim())
    entries.push({ key: 'emailSubject', label: `Subject: ${filters.emailSubject.trim()}` })
  if (filters.docId.trim()) entries.push({ key: 'docId', label: `Document UUID: ${filters.docId.trim()}` })
  return entries
}

function RelevanceScore({ score, maxScore }: { score: number; maxScore: number }) {
  const safeMax = maxScore > 0 ? maxScore : 1
  const width = Math.max(0, Math.min(100, Math.round((score / safeMax) * 100)))
  const formatted = score.toFixed(3)
  const tooltip = `Relevance score ${formatted}. Higher means a stronger match for this query. Compare scores within this search, not across different searches.`

  return (
    <div className="flex shrink-0 items-center gap-1.5" title={tooltip} aria-label={tooltip}>
      <span className="text-[11px] font-semibold text-gray-500 dark:text-gray-400">Score</span>
      <div className="h-1.5 w-24 rounded-full bg-gray-200 dark:bg-gray-600" aria-hidden="true">
        <div
          className="h-1.5 rounded-full bg-blue-500"
          style={{
            width: `${width}%`,
          }}
        />
      </div>
      <span className="text-xs text-gray-400">{formatted}</span>
    </div>
  )
}

function Pagination({
  currentPage,
  totalPages,
  onPageChange,
}: {
  currentPage: number
  totalPages: number
  onPageChange: (p: number) => void
}) {
  if (totalPages <= 1) return null
  const pages: (number | '...')[] = []
  for (let i = 1; i <= totalPages; i++) {
    if (i === 1 || i === totalPages || Math.abs(i - currentPage) <= 1) {
      pages.push(i)
    } else if (pages[pages.length - 1] !== '...') {
      pages.push('...')
    }
  }
  return (
    <div className="flex items-center gap-1">
      <button
        onClick={() => onPageChange(currentPage - 1)}
        disabled={currentPage <= 1}
        className="rounded-lg px-2 py-1 text-sm text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-30"
      >
        &lsaquo;
      </button>
      {pages.map((p, idx) =>
        p === '...' ? (
          <span key={`e${idx}`} className="px-1 text-sm text-gray-400">
            &hellip;
          </span>
        ) : (
          <button
            key={p}
            onClick={() => onPageChange(p)}
            className={`rounded-lg px-2.5 py-1 text-sm font-medium ${
              p === currentPage
                ? 'bg-(--color-accent) text-white'
                : 'text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700'
            }`}
          >
            {p}
          </button>
        ),
      )}
      <button
        onClick={() => onPageChange(currentPage + 1)}
        disabled={currentPage >= totalPages}
        className="rounded-lg px-2 py-1 text-sm text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-30"
      >
        &rsaquo;
      </button>
    </div>
  )
}

export default function SearchPage() {
  const [initial] = useState(loadSearchState)
  const [mode, setMode] = useState<SearchMode>(initial?.mode === 'find_all' ? 'find_all' : 'search')
  const [query, setQuery] = useState(initial?.query || '')
  const [results, setResults] = useState<SearchResults | null>(initial?.results || null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [filters, setFilters] = useState<SearchFilters>(() => normalizedFilters(initial?.filters))
  const [filtersOpen, setFiltersOpen] = useState(false)
  const [history, setHistory] = useState<string[]>(getHistory)
  const [showHistory, setShowHistory] = useState(false)
  const [filterOptions, setFilterOptions] = useState<SearchFilterOptions>({ mime_types: [] })
  const [pageSize, setPageSize] = useState(initial?.pageSize || 25)
  const [currentPage, setCurrentPage] = useState(initial?.currentPage || 1)
  const [scopeFolderIds, setScopeFolderIds] = useState<string[]>([])
  const [sortBy, setSortBy] = useState<FindAllSort>(initial?.sortBy || 'relevance')
  const lastQuery = useRef(initial?.lastQuery || '')
  const wrapperRef = useRef<HTMLDivElement>(null)
  const { folders } = useWatchedFolders()

  // Persist search state to sessionStorage
  useEffect(() => {
    saveSearchState({ mode, query, results, currentPage, pageSize, lastQuery: lastQuery.current, filters, sortBy })
  }, [mode, query, results, currentPage, pageSize, filters, sortBy])

  useEffect(() => {
    let cancelled = false
    get<SearchFilterOptions>('/api/docs/filters')
      .then((data) => {
        if (!cancelled) setFilterOptions({ mime_types: data.mime_types || [] })
      })
      .catch(() => {
        if (!cancelled) setFilterOptions({ mime_types: [] })
      })
    return () => {
      cancelled = true
    }
  }, [])

  // Close dropdown on outside click
  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (wrapperRef.current && !wrapperRef.current.contains(e.target as Node)) {
        setShowHistory(false)
      }
    }
    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [])

  async function doSearch(
    q: string,
    page: number,
    size: number,
    folderIds: string[] = scopeFolderIds,
    searchMode: SearchMode = mode,
    findAllSort: FindAllSort = sortBy,
  ) {
    const trimmed = q.trim()
    if (!trimmed) return
    const docId = filters.docId.trim()
    if (docId && !isUuid(docId)) {
      setError('Document UUID filter must be a valid UUID.')
      return
    }
    setError('')
    setLoading(true)
    addToHistory(trimmed)
    setHistory(getHistory())
    lastQuery.current = trimmed
    try {
      const offset = (page - 1) * size
      const filterPayload = buildFilterPayload(filters)
      const data =
        searchMode === 'find_all'
          ? await post<FindAllResponse>('/api/search/find-all', {
              query: trimmed,
              max_results: size,
              offset,
              presentation: 'full',
              sort_by: findAllSort,
              ...filterPayload,
              ...(folderIds.length > 0 && { scope: { folder_ids: folderIds } }),
            })
          : await post<SearchResponse>('/api/search', {
              query: trimmed,
              k: size,
              offset,
              ...filterPayload,
              ...(folderIds.length > 0 && { scope: { folder_ids: folderIds } }),
            })
      setResults(data)
    } catch (e) {
      setError(e instanceof Error ? e.message : searchMode === 'find_all' ? 'Find All failed' : 'Search failed')
    } finally {
      setLoading(false)
    }
  }

  function handleSearch(e: FormEvent) {
    e.preventDefault()
    setShowHistory(false)
    setCurrentPage(1)
    doSearch(query, 1, pageSize)
  }

  function handlePageChange(page: number) {
    setCurrentPage(page)
    doSearch(lastQuery.current, page, pageSize)
  }

  function handlePageSizeChange(size: number) {
    setPageSize(size)
    setCurrentPage(1)
    if (lastQuery.current) {
      doSearch(lastQuery.current, 1, size)
    }
  }

  function handleModeChange(nextMode: SearchMode) {
    if (nextMode === mode) return
    setMode(nextMode)
    setResults(null)
    setCurrentPage(1)
    lastQuery.current = ''
  }

  function handleSortChange(nextSort: FindAllSort) {
    setSortBy(nextSort)
    setCurrentPage(1)
    if (mode === 'find_all' && lastQuery.current) {
      doSearch(lastQuery.current, 1, pageSize, scopeFolderIds, 'find_all', nextSort)
    }
  }

  function selectHistoryItem(q: string) {
    setQuery(q)
    setShowHistory(false)
    setCurrentPage(1)
    doSearch(q, 1, pageSize)
  }

  function clearHistory() {
    localStorage.removeItem(HISTORY_KEY)
    setHistory([])
    setShowHistory(false)
  }

  function updateFilter(key: keyof SearchFilters, value: string) {
    setFilters((current) => ({ ...current, [key]: value }))
  }

  function clearFilter(key: keyof SearchFilters) {
    setFilters((current) => ({ ...current, [key]: '' }))
  }

  function clearFilters() {
    setFilters(EMPTY_FILTERS)
  }

  const searchResults = mode === 'search' && results && !isFindAllResponse(results) ? results : null
  const findAllResults = mode === 'find_all' && isFindAllResponse(results) ? results : null
  const metadataFilter = buildMetadataFilter(filters)
  const activeFilters = activeFilterEntries(filters)
  const activeFilterCount = activeFilters.length
  const mimeOptions = filterOptions.mime_types
  const selectedMimeMissing = !!filters.mimeType && !mimeOptions.some((option) => option.value === filters.mimeType)
  const totalCount = findAllResults?.total_matches ?? searchResults?.total_candidates ?? 0
  const maxScore = searchResults?.hits[0]?.score || findAllResults?.results[0]?.score || 1
  const totalPages = results ? Math.max(1, Math.ceil(totalCount / pageSize)) : 1
  const startIdx = (currentPage - 1) * pageSize

  return (
    <div>
      <PageHeader title="Search" subtitle="Hybrid lexical + semantic retrieval" />
      <div className="mb-4 inline-flex rounded-lg bg-(--color-bg-secondary) p-0.5 shadow-mac ring-1 ring-(--color-border)">
        <button
          type="button"
          aria-label="Search mode"
          aria-pressed={mode === 'search'}
          onClick={() => handleModeChange('search')}
          className={`min-w-24 rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${
            mode === 'search'
              ? 'bg-white text-(--color-text-primary) shadow-mac dark:bg-[#3a3a3c]'
              : 'text-(--color-text-secondary) hover:text-(--color-text-primary)'
          }`}
        >
          Search
        </button>
        <button
          type="button"
          aria-label="Find All mode"
          aria-pressed={mode === 'find_all'}
          onClick={() => handleModeChange('find_all')}
          className={`min-w-24 rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${
            mode === 'find_all'
              ? 'bg-white text-(--color-text-primary) shadow-mac dark:bg-[#3a3a3c]'
              : 'text-(--color-text-secondary) hover:text-(--color-text-primary)'
          }`}
        >
          Find All
        </button>
      </div>
      <form onSubmit={handleSearch} className="mb-6 flex items-center gap-2">
        <div className="relative flex-1" ref={wrapperRef}>
          <input
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onFocus={() => history.length > 0 && setShowHistory(true)}
            onKeyDown={(e) => {
              if (e.key === 'Escape') setShowHistory(false)
            }}
            placeholder={mode === 'find_all' ? 'Find all matching documents...' : 'Search documents...'}
            autoFocus
            className="w-full rounded-lg border-0 bg-(--color-bg-secondary) dark:bg-(--color-bg-tertiary) shadow-mac focus:ring-2 focus:ring-(--color-accent)/30 px-4 py-2 text-sm"
          />
          {showHistory && history.length > 0 && (
            <div className="absolute z-10 mt-1 w-full rounded-xl bg-white dark:bg-[#2c2c2e] shadow-mac-lg ring-1 ring-(--color-border) overflow-hidden">
              {history.map((q) => (
                <button
                  key={q}
                  type="button"
                  onMouseDown={(e) => {
                    e.preventDefault()
                    selectHistoryItem(q)
                  }}
                  className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm text-gray-700 dark:text-gray-300 hover:bg-black/3 dark:hover:bg-white/3"
                >
                  <svg
                    className="h-3.5 w-3.5 shrink-0 text-gray-400"
                    fill="none"
                    viewBox="0 0 24 24"
                    stroke="currentColor"
                    strokeWidth={2}
                  >
                    <path
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"
                    />
                  </svg>
                  {q}
                </button>
              ))}
              <button
                type="button"
                onMouseDown={(e) => {
                  e.preventDefault()
                  clearHistory()
                }}
                className="w-full border-t border-gray-100 dark:border-gray-700 px-3 py-1.5 text-left text-xs text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"
              >
                Clear history
              </button>
            </div>
          )}
        </div>
        <FolderPicker value={scopeFolderIds} onChange={setScopeFolderIds} folders={folders} size="sm" />
        {mode === 'find_all' && (
          <select
            value={sortBy}
            onChange={(e) => handleSortChange(e.target.value as FindAllSort)}
            aria-label="Find All sort"
            className="rounded-md border border-(--color-border) bg-(--color-bg-secondary) px-2.5 py-1.5 text-sm text-(--color-text-primary)"
          >
            <option value="relevance">Relevance</option>
            <option value="date_desc">Newest</option>
            <option value="date_asc">Oldest</option>
          </select>
        )}
        <button
          type="button"
          onClick={() => setFiltersOpen((open) => !open)}
          aria-expanded={filtersOpen}
          className="rounded-md border border-(--color-border) bg-(--color-bg-secondary) px-3 py-1.5 text-sm font-medium text-(--color-text-primary) hover:bg-(--color-bg-tertiary)"
        >
          Filters
          {activeFilterCount > 0 && (
            <span className="ml-1.5 rounded-full bg-(--area-accent) px-1.5 py-0.5 text-[10px] font-semibold text-white">
              {activeFilterCount}
            </span>
          )}
        </button>
        <button
          type="submit"
          disabled={loading}
          className="rounded-md px-4 py-1.5 text-sm font-medium disabled:opacity-50"
          style={{
            backgroundColor: 'var(--area-accent-tint)',
            color: 'var(--area-accent-text)',
            border: '1px solid var(--area-accent)',
          }}
        >
          {loading
            ? mode === 'find_all'
              ? 'Finding...'
              : 'Searching...'
            : mode === 'find_all'
              ? 'Find All'
              : 'Search'}
        </button>
      </form>

      {activeFilterCount > 0 && (
        <div className="mb-4 flex flex-wrap items-center gap-2">
          {activeFilters.map(({ key, label }) => (
            <span
              key={key}
              className="inline-flex items-center gap-1 rounded-md border border-(--color-border) bg-(--color-bg-secondary) px-2 py-1 text-xs font-medium text-(--color-text-primary)"
            >
              {label}
              <button
                type="button"
                onClick={() => clearFilter(key)}
                aria-label={`Remove ${label}`}
                className="ml-0.5 rounded-sm px-1 text-(--color-text-secondary) hover:bg-(--color-bg-tertiary) hover:text-(--color-text-primary)"
              >
                x
              </button>
            </span>
          ))}
          <button
            type="button"
            onClick={clearFilters}
            className="rounded-md px-2 py-1 text-xs font-medium text-(--color-text-secondary) hover:bg-(--color-bg-secondary) hover:text-(--color-text-primary)"
          >
            Clear filters
          </button>
        </div>
      )}

      {filtersOpen && (
        <Card className="mb-6 p-4">
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-4">
            <label className="block">
              <span className="mb-1 block text-xs font-medium text-(--color-text-secondary)">Exact text</span>
              <input
                type="text"
                value={filters.textContains}
                onChange={(e) => updateFilter('textContains', e.target.value)}
                className="input-base"
              />
            </label>
            <label className="block">
              <span className="mb-1 block text-xs font-medium text-(--color-text-secondary)">After</span>
              <input
                type="date"
                value={filters.after}
                onChange={(e) => updateFilter('after', e.target.value)}
                className="input-base"
              />
            </label>
            <label className="block">
              <span className="mb-1 block text-xs font-medium text-(--color-text-secondary)">Before</span>
              <input
                type="date"
                value={filters.before}
                onChange={(e) => updateFilter('before', e.target.value)}
                className="input-base"
              />
            </label>
            <label className="block">
              <span className="mb-1 block text-xs font-medium text-(--color-text-secondary)">Language</span>
              <select
                value={filters.language}
                onChange={(e) => updateFilter('language', e.target.value)}
                className="input-base"
              >
                <option value="">Any</option>
                <option value="en">English</option>
                <option value="fr">French</option>
              </select>
            </label>
            <label className="block">
              <span className="mb-1 block text-xs font-medium text-(--color-text-secondary)">MIME type</span>
              <select
                value={filters.mimeType}
                onChange={(e) => updateFilter('mimeType', e.target.value)}
                className="input-base"
              >
                <option value="">Any type</option>
                {selectedMimeMissing && <option value={filters.mimeType}>{filters.mimeType}</option>}
                {mimeOptions.map((option) => (
                  <option key={option.value} value={option.value}>
                    {mimeTypeOptionLabel(option)}
                  </option>
                ))}
              </select>
            </label>
            <label className="block">
              <span className="mb-1 block text-xs font-medium text-(--color-text-secondary)">Summary</span>
              <select
                value={filters.summaryState}
                onChange={(e) => updateFilter('summaryState', e.target.value)}
                className="input-base"
              >
                <option value="">Any summary state</option>
                <option value="has">Has summary</option>
                <option value="missing">Missing summary</option>
                <option value="pending">Summary pending</option>
                <option value="failed">Summary failed</option>
              </select>
            </label>
            <label className="block">
              <span className="mb-1 block text-xs font-medium text-(--color-text-secondary)">Pipeline</span>
              <select
                value={filters.pipelineStatus}
                onChange={(e) => updateFilter('pipelineStatus', e.target.value)}
                className="input-base"
              >
                <option value="">Any pipeline state</option>
                <option value="ready">Ready</option>
                <option value="processing">Processing</option>
                <option value="error">Failed</option>
              </select>
            </label>
            <label className="block">
              <span className="mb-1 block text-xs font-medium text-(--color-text-secondary)">Ingest issue</span>
              <select
                value={filters.jobIssue}
                onChange={(e) => updateFilter('jobIssue', e.target.value)}
                className="input-base"
              >
                <option value="">Any issue state</option>
                <option value="any_issue">Any ingest issue</option>
                <option value="failed_job">Any failed stage</option>
                <option value="ocr_failed">OCR failed</option>
                <option value="entity_skipped">Entity extraction skipped</option>
                <option value="summary_failed">Summary failed</option>
                <option value="summary_blocked">Summary paused</option>
                <option value="summary_pending">Summary pending</option>
                <option value="status_cleanup">Status cleanup needed</option>
              </select>
            </label>
            <label className="block">
              <span className="mb-1 block text-xs font-medium text-(--color-text-secondary)">Email from</span>
              <input
                type="text"
                value={filters.emailFrom}
                onChange={(e) => updateFilter('emailFrom', e.target.value)}
                className="input-base"
              />
            </label>
            <label className="block">
              <span className="mb-1 block text-xs font-medium text-(--color-text-secondary)">Email to</span>
              <input
                type="text"
                value={filters.emailTo}
                onChange={(e) => updateFilter('emailTo', e.target.value)}
                className="input-base"
              />
            </label>
            <label className="block">
              <span className="mb-1 block text-xs font-medium text-(--color-text-secondary)">Email cc</span>
              <input
                type="text"
                value={filters.emailCc}
                onChange={(e) => updateFilter('emailCc', e.target.value)}
                className="input-base"
              />
            </label>
            <label className="block md:col-span-2 xl:col-span-2">
              <span className="mb-1 block text-xs font-medium text-(--color-text-secondary)">Email subject</span>
              <input
                type="text"
                value={filters.emailSubject}
                onChange={(e) => updateFilter('emailSubject', e.target.value)}
                className="input-base"
              />
            </label>
            <div className="md:col-span-2 xl:col-span-2">
              <div className="mb-1 text-xs font-medium text-(--color-text-secondary)">Metadata JSON</div>
              <pre
                aria-label="Generated metadata JSON"
                className="min-h-[38px] overflow-x-auto rounded-md border border-(--color-border) bg-(--color-bg-secondary) px-2.5 py-2 text-xs text-(--color-text-primary)"
              >
                {JSON.stringify(metadataFilter, null, 2)}
              </pre>
              <p className="mt-1 text-[11px] text-(--color-text-secondary)">
                View-only for now. Friendly email filters generate this JSON.
              </p>
            </div>
            <details className="md:col-span-2 xl:col-span-4 rounded-md border border-(--color-border) bg-(--color-bg-secondary)/60 px-3 py-2">
              <summary className="cursor-pointer text-xs font-medium text-(--color-text-secondary)">
                Advanced identifier filter
              </summary>
              <div className="mt-3 grid gap-2 md:grid-cols-[minmax(0,1fr)_minmax(16rem,24rem)]">
                <p className="text-[11px] leading-5 text-(--color-text-secondary)">
                  Use this when you already have a document UUID from an API or CLI result, or from the document page
                  URL.
                </p>
                <label className="block">
                  <span className="mb-1 block text-xs font-medium text-(--color-text-secondary)">Document UUID</span>
                  <input
                    type="text"
                    value={filters.docId}
                    onChange={(e) => updateFilter('docId', e.target.value)}
                    className="input-base font-mono text-xs"
                  />
                </label>
              </div>
            </details>
          </div>
        </Card>
      )}

      {error && (
        <div className="mb-4 rounded-sm bg-red-50 dark:bg-red-900/20 px-3 py-2 text-sm text-red-700 dark:text-red-400">
          {error}
        </div>
      )}

      {searchResults && (
        <>
          {searchResults.possible_conflict && searchResults.conflict_sources.length > 0 && (
            <div className="mb-4 rounded-sm bg-amber-50 dark:bg-amber-900/20 px-4 py-3 text-sm text-amber-800 dark:text-amber-400">
              <strong>Possible conflict:</strong> Similar content found across multiple sources:{' '}
              {searchResults.conflict_sources.map((s, i) => (
                <span key={s.doc_id}>
                  {i > 0 && ', '}
                  <Link to={`/docs/${s.doc_id}`} className="font-medium text-amber-900 dark:text-amber-300 underline">
                    {s.title}
                  </Link>
                </span>
              ))}
            </div>
          )}

          {searchResults.hits.length === 0 && currentPage === 1 ? (
            <p className="text-gray-500 dark:text-gray-400">No results found.</p>
          ) : (
            <div className="space-y-3">
              {searchResults.hits.map((hit) => {
                const linkTo =
                  hit.page_start != null
                    ? `/docs/${hit.doc_id}?showContent=true&page=${hit.page_start}`
                    : `/docs/${hit.doc_id}?showContent=true`
                // Pass the chunk text via React Router state so the doc detail
                // page can scroll to and inline-highlight the matching span
                // within the page text. Avoids URL pollution since chunk
                // bodies can be long.
                const linkState = { highlightChunkText: hit.chunk_text, highlightChunkId: hit.chunk_id }

                return (
                  <Card key={hit.chunk_id} className="p-4">
                    <div className="mb-2 flex items-start justify-between gap-3">
                      <Link
                        to={linkTo}
                        state={linkState}
                        className="min-w-0 font-medium text-blue-600 dark:text-blue-400 hover:underline"
                      >
                        {hit.doc_title || 'Untitled'}
                      </Link>
                      <RelevanceScore score={hit.score} maxScore={maxScore} />
                    </div>
                    <p className="mb-2 text-sm text-gray-700 dark:text-gray-300 line-clamp-3">{hit.chunk_text}</p>
                    <div className="flex flex-wrap items-start gap-x-3 gap-y-1 text-xs text-gray-400">
                      <SourceCitation source={hit.source} citation={hit.citation} />
                      {hit.page_start != null && (
                        <span>
                          Page {hit.page_start}
                          {hit.page_end != null && hit.page_end !== hit.page_start ? `\u2013${hit.page_end}` : ''}
                        </span>
                      )}
                      <span>Lang: {hit.language}</span>
                      {hit.ocr_used && (
                        <span className="rounded-md text-[11px] font-medium bg-gray-100 dark:bg-gray-700 px-1.5 py-0.5">
                          OCR
                        </span>
                      )}
                    </div>
                  </Card>
                )
              })}

              {/* Status bar + pagination */}
              <div className="flex items-center justify-between pt-2">
                <div className="flex items-center gap-3 text-sm text-gray-500 dark:text-gray-400">
                  <span>
                    Showing {searchResults.total_candidates === 0 ? 0 : startIdx + 1}&ndash;
                    {Math.min(startIdx + pageSize, searchResults.total_candidates)} of {searchResults.total_candidates}{' '}
                    results
                  </span>
                  <select
                    value={pageSize}
                    onChange={(e) => handlePageSizeChange(Number(e.target.value))}
                    className="rounded-md border-0 bg-(--color-bg-secondary) dark:bg-(--color-bg-tertiary) py-0.5 pl-2 pr-6 text-sm"
                  >
                    {PAGE_SIZES.map((s) => (
                      <option key={s} value={s}>
                        {s} / page
                      </option>
                    ))}
                  </select>
                </div>
                <Pagination currentPage={currentPage} totalPages={totalPages} onPageChange={handlePageChange} />
              </div>
            </div>
          )}
        </>
      )}

      {findAllResults && (
        <>
          {findAllResults.results.length === 0 && currentPage === 1 ? (
            <p className="text-gray-500 dark:text-gray-400">No documents found.</p>
          ) : (
            <div className="space-y-3">
              {findAllResults.results.map((hit) => {
                const page = hit.top_chunk?.page ?? firstPageFromRange(hit.page_range)
                const linkTo =
                  page != null
                    ? `/docs/${hit.doc_id}?showContent=true&page=${page}`
                    : `/docs/${hit.doc_id}?showContent=true`
                const linkState =
                  hit.top_chunk?.text || hit.top_chunk?.chunk_id
                    ? {
                        highlightChunkText: hit.top_chunk?.text,
                        highlightChunkId: hit.top_chunk?.chunk_id,
                      }
                    : undefined

                return (
                  <Card key={hit.doc_id} className="p-4">
                    <div className="mb-2 flex items-start justify-between gap-3">
                      <div className="min-w-0">
                        <Link
                          to={linkTo}
                          state={linkState}
                          className="font-medium text-blue-600 dark:text-blue-400 hover:underline"
                        >
                          {hit.doc_title || 'Untitled'}
                        </Link>
                        {hit.source?.folder_label && (
                          <div className="mt-1 text-xs text-gray-500 dark:text-gray-400">{hit.source.folder_label}</div>
                        )}
                      </div>
                      <RelevanceScore score={hit.score} maxScore={maxScore} />
                    </div>
                    {hit.top_chunk?.text && (
                      <p className="mb-2 text-sm text-gray-700 dark:text-gray-300 line-clamp-3">{hit.top_chunk.text}</p>
                    )}
                    <div className="flex flex-wrap items-start gap-x-3 gap-y-1 text-xs text-gray-400">
                      <SourceCitation source={hit.source} citation={hit.citation} />
                      {hit.page_range && <span>Pages {hit.page_range}</span>}
                      {hit.language && <span>Lang: {hit.language}</span>}
                      {hit.mime_type && <span title={hit.mime_type}>{mimeTypeLabel(hit.mime_type)}</span>}
                    </div>
                  </Card>
                )
              })}

              <div className="flex items-center justify-between pt-2">
                <div className="flex items-center gap-3 text-sm text-gray-500 dark:text-gray-400">
                  <span>
                    Showing {findAllResults.total_matches === 0 ? 0 : startIdx + 1}&ndash;
                    {Math.min(startIdx + pageSize, findAllResults.total_matches)} of {findAllResults.total_matches}{' '}
                    documents
                  </span>
                  <select
                    value={pageSize}
                    onChange={(e) => handlePageSizeChange(Number(e.target.value))}
                    className="rounded-md border-0 bg-(--color-bg-secondary) dark:bg-(--color-bg-tertiary) py-0.5 pl-2 pr-6 text-sm"
                  >
                    {PAGE_SIZES.map((s) => (
                      <option key={s} value={s}>
                        {s} / page
                      </option>
                    ))}
                  </select>
                </div>
                <Pagination currentPage={currentPage} totalPages={totalPages} onPageChange={handlePageChange} />
              </div>
            </div>
          )}
        </>
      )}
    </div>
  )
}

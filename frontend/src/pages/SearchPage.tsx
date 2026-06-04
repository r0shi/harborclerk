import { FormEvent, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { post } from '../api'
import { Card } from '../components/Card'
import { FolderPicker } from '../components/FolderPicker'
import { PageHeader } from '../components/PageHeader'
import { useWatchedFolders } from '../hooks/useWatchedFolders'

type SearchMode = 'search' | 'find_all'

interface SourceRef {
  doc_id: string
  doc_title: string
  chunk_id?: string
  pages?: string
  section?: string
  source_kind: 'document' | 'email' | 'attachment' | 'unknown'
  source_label: string
  folder_label?: string
  relative_path?: string
  citation: string
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
  sort_by: 'relevance' | 'date_desc' | 'date_asc'
  presentation: 'brief' | 'full'
}

type SearchResults = SearchResponse | FindAllResponse

const HISTORY_KEY = 'search_history'
const STATE_KEY = 'search_state'
const MAX_HISTORY = 10
const PAGE_SIZES = [10, 25, 50]

interface SearchState {
  mode?: SearchMode
  query: string
  results: SearchResults | null
  currentPage: number
  pageSize: number
  lastQuery: string
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
  const [history, setHistory] = useState<string[]>(getHistory)
  const [showHistory, setShowHistory] = useState(false)
  const [pageSize, setPageSize] = useState(initial?.pageSize || 25)
  const [currentPage, setCurrentPage] = useState(initial?.currentPage || 1)
  const [scopeFolderIds, setScopeFolderIds] = useState<string[]>([])
  const lastQuery = useRef(initial?.lastQuery || '')
  const wrapperRef = useRef<HTMLDivElement>(null)
  const { folders } = useWatchedFolders()

  // Persist search state to sessionStorage
  useEffect(() => {
    saveSearchState({ mode, query, results, currentPage, pageSize, lastQuery: lastQuery.current })
  }, [mode, query, results, currentPage, pageSize])

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
  ) {
    const trimmed = q.trim()
    if (!trimmed) return
    setError('')
    setLoading(true)
    addToHistory(trimmed)
    setHistory(getHistory())
    lastQuery.current = trimmed
    try {
      const offset = (page - 1) * size
      const data =
        searchMode === 'find_all'
          ? await post<FindAllResponse>('/api/search/find-all', {
              query: trimmed,
              max_results: size,
              offset,
              presentation: 'full',
              sort_by: 'relevance',
              ...(folderIds.length > 0 && { scope: { folder_ids: folderIds } }),
            })
          : await post<SearchResponse>('/api/search', {
              query: trimmed,
              k: size,
              offset,
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

  const searchResults = mode === 'search' && results && !isFindAllResponse(results) ? results : null
  const findAllResults = mode === 'find_all' && isFindAllResponse(results) ? results : null
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
                    <div className="mb-2 flex items-center justify-between">
                      <Link
                        to={linkTo}
                        state={linkState}
                        className="font-medium text-blue-600 dark:text-blue-400 hover:underline"
                      >
                        {hit.doc_title || 'Untitled'}
                      </Link>
                      <div className="flex items-center space-x-2">
                        <div className="h-1.5 w-24 rounded-full bg-gray-200 dark:bg-gray-600">
                          <div
                            className="h-1.5 rounded-full bg-blue-500"
                            style={{
                              width: `${Math.round((hit.score / maxScore) * 100)}%`,
                            }}
                          />
                        </div>
                        <span className="text-xs text-gray-400">{hit.score.toFixed(3)}</span>
                      </div>
                    </div>
                    <p className="mb-2 text-sm text-gray-700 dark:text-gray-300 line-clamp-3">{hit.chunk_text}</p>
                    <div className="flex items-center space-x-3 text-xs text-gray-400">
                      {hit.citation && <span>{hit.citation}</span>}
                      {hit.page_start != null && (
                        <span>
                          Page {hit.page_start}
                          {hit.page_end != null && hit.page_end !== hit.page_start ? `\u2013${hit.page_end}` : ''}
                        </span>
                      )}
                      <span>Lang: {hit.language}</span>
                      {hit.source?.relative_path && <span>{hit.source.relative_path}</span>}
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
                      <div className="flex shrink-0 items-center space-x-2">
                        <div className="h-1.5 w-24 rounded-full bg-gray-200 dark:bg-gray-600">
                          <div
                            className="h-1.5 rounded-full bg-blue-500"
                            style={{
                              width: `${Math.round((hit.score / maxScore) * 100)}%`,
                            }}
                          />
                        </div>
                        <span className="text-xs text-gray-400">{hit.score.toFixed(3)}</span>
                      </div>
                    </div>
                    {hit.top_chunk?.text && (
                      <p className="mb-2 text-sm text-gray-700 dark:text-gray-300 line-clamp-3">{hit.top_chunk.text}</p>
                    )}
                    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-gray-400">
                      {hit.citation && <span>{hit.citation}</span>}
                      {hit.page_range && <span>Pages {hit.page_range}</span>}
                      {hit.language && <span>Lang: {hit.language}</span>}
                      {hit.mime_type && <span>{hit.mime_type}</span>}
                      {hit.source?.relative_path && <span>{hit.source.relative_path}</span>}
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

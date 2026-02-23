import { FormEvent, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { post } from '../api'

interface SearchHit {
  chunk_id: string
  doc_id: string
  version_id: string
  chunk_num: number
  chunk_text: string
  page_start?: number
  page_end?: number
  language: string
  ocr_used: boolean
  ocr_confidence?: number
  score: number
  doc_title?: string
}

interface ConflictSource {
  doc_id: string
  version_id: string
  title: string
}

interface SearchResponse {
  hits: SearchHit[]
  possible_conflict: boolean
  conflict_sources: ConflictSource[]
}

const HISTORY_KEY = 'search_history'
const MAX_HISTORY = 10

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

export default function SearchPage() {
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<SearchResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [history, setHistory] = useState<string[]>(getHistory)
  const [showHistory, setShowHistory] = useState(false)
  const wrapperRef = useRef<HTMLDivElement>(null)

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

  async function doSearch(q: string) {
    const trimmed = q.trim()
    if (!trimmed) return
    setError('')
    setLoading(true)
    addToHistory(trimmed)
    setHistory(getHistory())
    try {
      const data = await post<SearchResponse>('/api/search', {
        query: trimmed,
        k: 10,
      })
      setResults(data)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Search failed')
    } finally {
      setLoading(false)
    }
  }

  function handleSearch(e: FormEvent) {
    e.preventDefault()
    setShowHistory(false)
    doSearch(query)
  }

  function selectHistoryItem(q: string) {
    setQuery(q)
    setShowHistory(false)
    doSearch(q)
  }

  function clearHistory() {
    localStorage.removeItem(HISTORY_KEY)
    setHistory([])
    setShowHistory(false)
  }

  const maxScore = results?.hits[0]?.score || 1

  return (
    <div>
      <h1 className="mb-4 text-xl font-bold">Search</h1>
      <form onSubmit={handleSearch} className="mb-6 flex space-x-2">
        <div className="relative flex-1" ref={wrapperRef}>
          <input
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onFocus={() => history.length > 0 && setShowHistory(true)}
            onKeyDown={(e) => {
              if (e.key === 'Escape') setShowHistory(false)
            }}
            placeholder="Search documents..."
            autoFocus
            className="w-full rounded-lg border-0 bg-[var(--color-bg-secondary)] dark:bg-[var(--color-bg-tertiary)] shadow-mac focus:ring-2 focus:ring-[var(--color-accent)]/30 px-4 py-2 text-sm"
          />
          {showHistory && history.length > 0 && (
            <div className="absolute z-10 mt-1 w-full rounded-xl bg-white dark:bg-[#2c2c2e] shadow-mac overflow-hidden">
              {history.map((q) => (
                <button
                  key={q}
                  type="button"
                  onMouseDown={(e) => {
                    e.preventDefault()
                    selectHistoryItem(q)
                  }}
                  className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm text-gray-700 dark:text-gray-300 hover:bg-black/[0.02] dark:hover:bg-white/[0.02]"
                >
                  <svg className="h-3.5 w-3.5 flex-shrink-0 text-gray-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
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
        <button
          type="submit"
          disabled={loading}
          className="rounded-lg bg-blue-600 px-6 py-2 text-sm font-medium text-white shadow-sm hover:bg-blue-700 disabled:opacity-50"
        >
          {loading ? 'Searching...' : 'Search'}
        </button>
      </form>

      {error && (
        <div className="mb-4 rounded bg-red-50 dark:bg-red-900/20 px-3 py-2 text-sm text-red-700 dark:text-red-400">
          {error}
        </div>
      )}

      {results && (
        <>
          {results.possible_conflict && results.conflict_sources.length > 0 && (
            <div className="mb-4 rounded bg-amber-50 dark:bg-amber-900/20 px-4 py-3 text-sm text-amber-800 dark:text-amber-400">
              <strong>Possible conflict:</strong> Similar content found across
              multiple sources:{' '}
              {results.conflict_sources.map((s, i) => (
                <span key={s.version_id}>
                  {i > 0 && ', '}
                  <Link
                    to={`/docs/${s.doc_id}`}
                    className="font-medium text-amber-900 dark:text-amber-300 underline"
                  >
                    {s.title}
                  </Link>
                </span>
              ))}
            </div>
          )}

          {results.hits.length === 0 ? (
            <p className="text-gray-500 dark:text-gray-400">No results found.</p>
          ) : (
            <div className="space-y-3">
              <p className="text-sm text-gray-500 dark:text-gray-400">
                {results.hits.length} results
              </p>
              {results.hits.map((hit) => {
                const linkTo = hit.page_start != null
                  ? `/docs/${hit.doc_id}?showContent=true&page=${hit.page_start}`
                  : `/docs/${hit.doc_id}?showContent=true`

                return (
                  <div
                    key={hit.chunk_id}
                    className="rounded-xl bg-white dark:bg-[#2c2c2e] shadow-mac p-4"
                  >
                    <div className="mb-2 flex items-center justify-between">
                      <Link
                        to={linkTo}
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
                        <span className="text-xs text-gray-400">
                          {hit.score.toFixed(3)}
                        </span>
                      </div>
                    </div>
                    <p className="mb-2 text-sm text-gray-700 dark:text-gray-300 line-clamp-3">
                      {hit.chunk_text}
                    </p>
                    <div className="flex items-center space-x-3 text-xs text-gray-400">
                      {hit.page_start != null && (
                        <span>
                          Page {hit.page_start}
                          {hit.page_end != null && hit.page_end !== hit.page_start
                            ? `\u2013${hit.page_end}`
                            : ''}
                        </span>
                      )}
                      <span>Lang: {hit.language}</span>
                      {hit.ocr_used && <span className="rounded-md text-[11px] font-medium bg-gray-100 dark:bg-gray-700 px-1.5 py-0.5">OCR</span>}
                    </div>
                  </div>
                )
              })}
            </div>
          )}
        </>
      )}
    </div>
  )
}

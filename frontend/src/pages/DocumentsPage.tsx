import { Fragment, useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { get, post, downloadBlob } from '../api'
import { useAuth } from '../auth'
import { useJobEvents } from '../hooks/useJobEvents'

interface DocSummary {
  doc_id: string
  title: string
  canonical_filename?: string
  status: string
  latest_version_status?: string
  version_count: number
  created_at: string
  updated_at: string
  summary?: string
  summary_model?: string
  source_path?: string
}

const PROCESSING_STATUSES = new Set([
  'queued', 'extracting', 'extracted', 'ocr_running', 'ocr_done',
  'chunking', 'chunked', 'embedding', 'embedded', 'finalizing',
  'summarizing', 'summarized',
])

function normalizeStatus(status?: string): string {
  if (!status) return 'unknown'
  if (PROCESSING_STATUSES.has(status)) return 'processing'
  return status
}

function StatusBadge({ status }: { status: string }) {
  const display = normalizeStatus(status)
  let cls = 'bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300'
  if (display === 'ready') cls = 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400'
  else if (display === 'error') cls = 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400'
  else if (display === 'processing') cls = 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400'

  return (
    <span className={`inline-block rounded-md px-2 py-0.5 text-[11px] font-medium ${cls}`}>
      {display}
    </span>
  )
}

function Pagination({ currentPage, totalPages, onPageChange }: {
  currentPage: number; totalPages: number; onPageChange: (p: number) => void
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
    <div className="mt-4 flex items-center justify-center gap-1">
      <button
        onClick={() => onPageChange(currentPage - 1)}
        disabled={currentPage <= 1}
        className="rounded-lg px-2 py-1 text-sm text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-30"
      >
        Prev
      </button>
      {pages.map((p, i) =>
        p === '...' ? (
          <span key={`e${i}`} className="px-2 text-sm text-gray-400">...</span>
        ) : (
          <button
            key={p}
            onClick={() => onPageChange(p)}
            className={`rounded-lg px-2.5 py-1 text-sm font-medium ${
              p === currentPage
                ? 'bg-[var(--color-accent)] text-white'
                : 'text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700'
            }`}
          >
            {p}
          </button>
        )
      )}
      <button
        onClick={() => onPageChange(currentPage + 1)}
        disabled={currentPage >= totalPages}
        className="rounded-lg px-2 py-1 text-sm text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-30"
      >
        Next
      </button>
    </div>
  )
}

export default function DocumentsPage() {
  const { user, isAdmin, updatePreferences } = useAuth()
  const [pageSize, setPageSize] = useState(user?.preferences?.page_size || 10)
  const [docs, setDocs] = useState<DocSummary[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [filter, setFilter] = useState('')
  const [currentPage, setCurrentPage] = useState(1)
  const [expanded, setExpanded] = useState<Set<string>>(new Set())

  const loadDocs = useCallback(() => {
    return get<DocSummary[]>('/api/docs')
      .then(setDocs)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => {
    loadDocs()
  }, [loadDocs])

  // Live-update when ingestion completes
  const debounceRef = useRef<ReturnType<typeof setTimeout>>(undefined)
  useJobEvents(useCallback((event) => {
    if (event.stage === 'finalize' && event.status === 'done') {
      clearTimeout(debounceRef.current)
      debounceRef.current = setTimeout(() => loadDocs(), 1000)
    }
  }, [loadDocs]))

  async function handleCancel(docId: string) {
    try {
      await post(`/api/docs/${docId}/cancel`)
      loadDocs()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Cancel failed')
    }
  }

  async function handleDownload(docId: string) {
    try {
      const { blob, filename } = await downloadBlob(`/api/docs/${docId}/download`)
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = filename
      document.body.appendChild(a)
      a.click()
      a.remove()
      URL.revokeObjectURL(url)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Download failed')
    }
  }

  const filteredDocs = docs.filter((d) => {
    if (!filter) return true
    const q = filter.toLowerCase()
    return d.title.toLowerCase().includes(q) ||
      (d.canonical_filename?.toLowerCase().includes(q) ?? false)
  })

  const totalPages = Math.max(1, Math.ceil(filteredDocs.length / pageSize))
  const effectivePage = Math.min(currentPage, totalPages)
  const startIdx = (effectivePage - 1) * pageSize
  const visibleDocs = filteredDocs.slice(startIdx, startIdx + pageSize)

  // Sync state when page exceeds total (e.g. after doc count changes)
  useEffect(() => {
    if (currentPage > totalPages) setCurrentPage(totalPages)
  }, [totalPages, currentPage])

  let lastDoc: { doc_id: string; title: string } | null = null
  try {
    const raw = sessionStorage.getItem('lastDoc')
    if (raw) lastDoc = JSON.parse(raw)
  } catch { /* ignore */ }

  if (loading) return <div className="text-gray-500 dark:text-gray-400">Loading documents...</div>
  if (error) return <div className="text-red-600 dark:text-red-400">Error: {error}</div>

  return (
    <div>
      {lastDoc && (
        <div className="mb-3 text-sm text-gray-500 dark:text-gray-400">
          Continue viewing:{' '}
          <Link
            to={`/docs/${lastDoc.doc_id}`}
            className="text-blue-600 dark:text-blue-400 hover:underline"
          >
            {lastDoc.title}
          </Link>
        </div>
      )}
      <div className="mb-4 flex items-center justify-between">
        <h1 className="text-xl font-bold">Documents</h1>
        <div className="flex items-center gap-3">
          <input
            type="text"
            placeholder="Filter by filename..."
            value={filter}
            onChange={(e) => { setFilter(e.target.value); setCurrentPage(1) }}
            className="w-64 rounded-lg border border-[var(--color-border)] bg-white dark:bg-[#2c2c2e] px-3 py-1.5 text-sm text-[var(--color-text-primary)] placeholder-[var(--color-text-secondary)] focus:outline-none focus:ring-2 focus:ring-[var(--color-accent)]/30"
          />
          <Link
            to="/upload"
            className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white shadow-sm hover:bg-blue-700"
          >
            Upload
          </Link>
        </div>
      </div>
      {docs.length === 0 ? (
        <p className="text-gray-500 dark:text-gray-400">
          No documents yet.{' '}
          <Link to="/upload" className="text-blue-600 dark:text-blue-400 hover:underline">
            Upload one
          </Link>
          .
        </p>
      ) : (
        <>
          <div className="rounded-xl bg-white dark:bg-[#2c2c2e] shadow-mac overflow-hidden">
            <table className="min-w-full divide-y divide-[var(--color-border)]">
              <thead className="bg-[var(--color-bg-secondary)]">
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
                  <th className="w-10"></th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[var(--color-border)] bg-white dark:bg-[#2c2c2e]">
                {visibleDocs.map((doc) => {
                  const isExpanded = expanded.has(doc.doc_id)
                  return (
                    <Fragment key={doc.doc_id}>
                      <tr className="hover:bg-black/[0.02] dark:hover:bg-white/[0.02]">
                        <td className="px-4 py-3">
                          <div className="flex items-center gap-1.5">
                            <button
                              onClick={() => setExpanded(prev => {
                                const next = new Set(prev)
                                if (next.has(doc.doc_id)) next.delete(doc.doc_id)
                                else next.add(doc.doc_id)
                                return next
                              })}
                              className="rounded p-0.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"
                              title="Toggle details"
                            >
                              <svg
                                className={`h-3.5 w-3.5 transition-transform ${isExpanded ? 'rotate-90' : ''}`}
                                fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}
                              >
                                <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
                              </svg>
                            </button>
                            <div>
                              <Link
                                to={`/docs/${doc.doc_id}`}
                                className="font-medium text-blue-600 dark:text-blue-400 hover:underline"
                              >
                                {doc.title}
                              </Link>
                              {doc.canonical_filename && (
                                <div className="text-xs text-gray-400">
                                  {doc.canonical_filename}
                                </div>
                              )}
                            </div>
                          </div>
                        </td>
                        <td className="px-4 py-3">
                          <div className="flex items-center gap-1.5">
                            <StatusBadge status={doc.latest_version_status || doc.status} />
                            {isAdmin && normalizeStatus(doc.latest_version_status) === 'processing' && (
                              <button
                                onClick={(e) => {
                                  e.preventDefault()
                                  handleCancel(doc.doc_id)
                                }}
                                className="rounded p-0.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"
                                title="Cancel processing"
                              >
                                <svg className="h-3.5 w-3.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                                  <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
                                </svg>
                              </button>
                            )}
                          </div>
                        </td>
                        <td className="px-4 py-3 text-sm text-gray-600 dark:text-gray-400">
                          {doc.version_count}
                        </td>
                        <td className="px-4 py-3 text-sm text-gray-500 dark:text-gray-400">
                          {new Date(doc.updated_at).toLocaleDateString()}
                        </td>
                        <td className="px-4 py-3 text-right">
                          <button
                            onClick={() => handleDownload(doc.doc_id)}
                            className="rounded p-1 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 transition-colors"
                            title="Download original"
                          >
                            <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                              <path strokeLinecap="round" strokeLinejoin="round" d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5M16.5 12L12 16.5m0 0L7.5 12m4.5 4.5V3" />
                            </svg>
                          </button>
                        </td>
                      </tr>
                      {isExpanded && (
                        <tr className="bg-gray-50/50 dark:bg-white/[0.02]">
                          <td colSpan={5} className="px-4 py-3 pl-10">
                            <div className="space-y-1 text-sm">
                              <div>
                                <span className="font-medium text-gray-500 dark:text-gray-400">Summary{doc.summary_model ? <span className="font-normal text-gray-400 dark:text-gray-500"> ({doc.summary_model})</span> : ''}: </span>
                                {doc.summary
                                  ? <span className="text-gray-700 dark:text-gray-300">{doc.summary}</span>
                                  : <span className="italic text-gray-400 dark:text-gray-500">No summary</span>
                                }
                              </div>
                              <div>
                                <span className="font-medium text-gray-500 dark:text-gray-400">Source: </span>
                                {doc.source_path
                                  ? <span className="text-gray-700 dark:text-gray-300 font-mono text-xs">{doc.source_path}</span>
                                  : <span className="italic text-gray-400 dark:text-gray-500">Unknown</span>
                                }
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
          <div className="mt-2 flex items-center justify-center gap-3 text-xs text-gray-400">
            <span>
              Showing {filteredDocs.length === 0 ? 0 : startIdx + 1}–{Math.min(startIdx + pageSize, filteredDocs.length)} of {filteredDocs.length}{filter && ` (filtered from ${docs.length})`}
            </span>
            <span className="text-gray-300 dark:text-gray-600">|</span>
            <label className="flex items-center gap-1.5">
              Show
              <select
                value={pageSize}
                onChange={(e) => {
                  const n = Number(e.target.value)
                  setPageSize(n)
                  setCurrentPage(1)
                  updatePreferences({ page_size: n }).catch(() => {})
                }}
                className="rounded border-0 bg-gray-100 dark:bg-gray-700/50 px-1.5 py-0.5 text-xs focus:ring-2 focus:ring-[var(--color-accent)]/30"
              >
                {[10, 25, 50, 100].map((n) => (
                  <option key={n} value={n}>{n}</option>
                ))}
              </select>
              per page
            </label>
          </div>
          <Pagination currentPage={effectivePage} totalPages={totalPages} onPageChange={setCurrentPage} />
        </>
      )}
    </div>
  )
}

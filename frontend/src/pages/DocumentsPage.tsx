import { Fragment, useCallback, useEffect, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { get, post, del, downloadBlob } from '../api'
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
  doc_type?: string
}

interface PaginatedDocs {
  items: DocSummary[]
  total: number
  limit: number
  offset: number
}

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

  return <span className={`inline-block rounded-md px-2 py-0.5 text-[11px] font-medium ${cls}`}>{display}</span>
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
  const [searchParams] = useSearchParams()
  const [pageSize, setPageSize] = useState(user?.preferences?.page_size || 10)
  const [docs, setDocs] = useState<DocSummary[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [filter, setFilter] = useState('')
  const [currentPage, setCurrentPage] = useState(1)
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [bulkAction, setBulkAction] = useState('')
  const [confirmingDelete, setConfirmingDelete] = useState(false)

  // Filter & sort state
  const [filterOptions, setFilterOptions] = useState<{
    mime_types: { value: string; count: number }[]
    doc_types: { value: string; count: number }[]
    languages: { value: string; count: number }[]
    entity_types: { value: string; count: number }[]
  }>({ mime_types: [], doc_types: [], languages: [], entity_types: [] })

  const [mimeFilter, setMimeFilter] = useState('')
  const [langFilter, setLangFilter] = useState('')
  const [docTypeFilter, setDocTypeFilter] = useState('')
  const [entityFilter, setEntityFilter] = useState('')
  const [entityTypeFilter, setEntityTypeFilter] = useState('')
  const [sortField, setSortField] = useState<'updated' | 'created' | 'title'>('updated')
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('desc')

  // Entity autocomplete
  const [entityInput, setEntityInput] = useState('')
  const [entitySuggestions, setEntitySuggestions] = useState<
    { entity_text: string; entity_type: string; doc_count: number }[]
  >([])
  const [showEntityDropdown, setShowEntityDropdown] = useState(false)

  // Load filter options on mount
  useEffect(() => {
    get<typeof filterOptions>('/api/docs/filters')
      .then(setFilterOptions)
      .catch(() => {})
  }, [])

  // Initialize filters from URL params on mount
  useEffect(() => {
    async function initFromUrl() {
      const urlEntity = searchParams.get('entity')
      const urlMime = searchParams.get('mime_type')
      const urlLang = searchParams.get('language')
      const urlDocType = searchParams.get('doc_type')
      const urlEntityType = searchParams.get('entity_type')
      if (urlEntity) {
        setEntityFilter(urlEntity)
        setEntityInput(urlEntity)
      }
      if (urlEntityType) setEntityTypeFilter(urlEntityType)
      if (urlMime) setMimeFilter(urlMime)
      if (urlLang) setLangFilter(urlLang)
      if (urlDocType) setDocTypeFilter(urlDocType)
    }
    initFromUrl()
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const entityTimerRef = useRef<ReturnType<typeof setTimeout>>(undefined)

  function handleEntityInputChange(value: string) {
    setEntityInput(value)
    if (value.length < 2) {
      setEntitySuggestions([])
      return
    }
    clearTimeout(entityTimerRef.current)
    entityTimerRef.current = setTimeout(async () => {
      try {
        const results = await get<typeof entitySuggestions>('/api/docs/entities/autocomplete', {
          q: value,
          limit: 10,
        })
        setEntitySuggestions(results)
        setShowEntityDropdown(true)
      } catch {
        /* ignore */
      }
    }, 200)
  }

  function selectEntity(text: string) {
    setEntityFilter(text)
    setEntityInput(text)
    setShowEntityDropdown(false)
    setCurrentPage(1)
  }

  function clearEntityFilter() {
    setEntityFilter('')
    setEntityTypeFilter('')
    setEntityInput('')
    setEntitySuggestions([])
    setCurrentPage(1)
  }

  const loadDocs = useCallback(
    (page: number, size: number, q: string) => {
      const params: Record<string, string | number> = {
        limit: size,
        offset: (page - 1) * size,
        sort: sortField,
        sort_dir: sortDir,
      }
      if (q) params.q = q
      if (mimeFilter) params.mime_type = mimeFilter
      if (langFilter) params.language = langFilter
      if (docTypeFilter) params.doc_type = docTypeFilter
      if (entityFilter) params.entity = entityFilter
      if (entityTypeFilter) params.entity_type = entityTypeFilter
      return get<PaginatedDocs>('/api/docs', params)
        .then((data) => {
          setDocs(data.items)
          setTotal(data.total)
        })
        .catch((e) => setError(e.message))
        .finally(() => setLoading(false))
    },
    [sortField, sortDir, mimeFilter, langFilter, docTypeFilter, entityFilter, entityTypeFilter],
  )

  useEffect(() => {
    loadDocs(currentPage, pageSize, filter)
  }, [loadDocs, currentPage, pageSize, filter])

  // Debounce filter input
  const filterTimerRef = useRef<ReturnType<typeof setTimeout>>(undefined)
  const [filterInput, setFilterInput] = useState('')

  function handleFilterChange(value: string) {
    setFilterInput(value)
    clearTimeout(filterTimerRef.current)
    filterTimerRef.current = setTimeout(() => {
      setFilter(value)
      setCurrentPage(1)
    }, 300)
  }

  // Live-update when ingestion completes
  const debounceRef = useRef<ReturnType<typeof setTimeout>>(undefined)
  useJobEvents(
    useCallback(
      (event) => {
        if (event.stage === 'finalize' && event.status === 'done') {
          clearTimeout(debounceRef.current)
          debounceRef.current = setTimeout(() => loadDocs(currentPage, pageSize, filter), 1000)
        }
      },
      [loadDocs, currentPage, pageSize, filter],
    ),
  )

  async function handleCancel(docId: string) {
    try {
      await post(`/api/docs/${docId}/cancel`)
      loadDocs(currentPage, pageSize, filter)
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

  function toggleSelect(docId: string) {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(docId)) next.delete(docId)
      else next.add(docId)
      return next
    })
  }

  function toggleSelectAll() {
    if (selected.size === visibleDocIds.size) {
      setSelected(new Set())
    } else {
      setSelected(new Set(visibleDocIds))
    }
  }

  async function handleBulkReprocess() {
    setBulkAction('reprocess')
    const errors: string[] = []
    for (const docId of selected) {
      try {
        await post(`/api/docs/${docId}/reprocess`)
      } catch (e) {
        errors.push(e instanceof Error ? e.message : 'Reprocess failed')
      }
    }
    if (errors.length > 0) setError(errors.join('; '))
    setSelected(new Set())
    loadDocs(currentPage, pageSize, filter)
    setBulkAction('')
  }

  async function handleBulkDelete() {
    if (!confirmingDelete) {
      setConfirmingDelete(true)
      return
    }
    setConfirmingDelete(false)
    setBulkAction('delete')
    const errors: string[] = []
    for (const docId of selected) {
      try {
        await del(`/api/docs/${docId}`)
      } catch (e) {
        errors.push(e instanceof Error ? e.message : 'Delete failed')
      }
    }
    if (errors.length > 0) setError(errors.join('; '))
    setSelected(new Set())
    loadDocs(currentPage, pageSize, filter)
    setBulkAction('')
  }

  async function handleBulkDownload() {
    setBulkAction('download')
    for (const docId of selected) {
      await handleDownload(docId)
    }
    setBulkAction('')
  }

  const totalPages = Math.max(1, Math.ceil(total / pageSize))
  const visibleDocs = docs
  const visibleDocIds = new Set(visibleDocs.map((d) => d.doc_id))
  const startIdx = (currentPage - 1) * pageSize
  const hasFilters = !!(filter || mimeFilter || langFilter || docTypeFilter || entityFilter)

  let lastDoc: { doc_id: string; title: string } | null = null
  try {
    const raw = sessionStorage.getItem('lastDoc')
    if (raw) lastDoc = JSON.parse(raw)
  } catch {
    /* ignore */
  }

  if (loading) return <div className="text-gray-500 dark:text-gray-400">Loading documents...</div>
  if (error) return <div className="text-red-600 dark:text-red-400">Error: {error}</div>

  return (
    <div>
      {lastDoc && (
        <div className="mb-3 text-sm text-gray-500 dark:text-gray-400">
          Continue viewing:{' '}
          <Link to={`/docs/${lastDoc.doc_id}`} className="text-blue-600 dark:text-blue-400 hover:underline">
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
            value={filterInput}
            onChange={(e) => handleFilterChange(e.target.value)}
            className="w-64 rounded-lg border border-(--color-border) bg-white dark:bg-[#2c2c2e] px-3 py-1.5 text-sm text-(--color-text-primary) placeholder-(--color-text-secondary) focus:outline-hidden focus:ring-2 focus:ring-(--color-accent)/30"
          />
          <Link
            to="/upload"
            className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white shadow-xs hover:bg-blue-700"
          >
            Upload
          </Link>
        </div>
      </div>
      {/* Filter bar */}
      {(filterOptions.mime_types.length > 0 ||
        filterOptions.doc_types.length > 0 ||
        filterOptions.languages.length > 0) && (
        <div className="mb-3 flex flex-wrap items-center gap-2">
          {/* Entity search */}
          <div className="relative">
            <label className="absolute -top-4 left-0 text-[10px] font-medium uppercase tracking-wider text-(--color-text-secondary)">
              Entities
            </label>
            <input
              type="text"
              placeholder="Filter by entity..."
              value={entityInput}
              onChange={(e) => handleEntityInputChange(e.target.value)}
              onFocus={() => entitySuggestions.length > 0 && setShowEntityDropdown(true)}
              onBlur={() => setTimeout(() => setShowEntityDropdown(false), 200)}
              className="w-48 rounded-lg border border-(--color-border) bg-white dark:bg-[#2c2c2e] px-2.5 py-1 text-xs text-(--color-text-primary) placeholder-(--color-text-secondary) focus:outline-hidden focus:ring-2 focus:ring-(--color-accent)/30"
            />
            {entityFilter && (
              <button
                onClick={clearEntityFilter}
                className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600"
              >
                <svg className="h-3 w-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            )}
            {showEntityDropdown && entitySuggestions.length > 0 && (
              <div className="absolute z-50 mt-1 w-64 rounded-lg bg-white dark:bg-[#2c2c2e] shadow-mac-lg ring-1 ring-(--color-border) max-h-48 overflow-y-auto">
                {entitySuggestions.map((s) => (
                  <button
                    key={`${s.entity_type}-${s.entity_text}`}
                    onMouseDown={() => selectEntity(s.entity_text)}
                    className="flex w-full items-center justify-between px-3 py-1.5 text-xs hover:bg-gray-50 dark:hover:bg-gray-700/50"
                  >
                    <span className="truncate text-(--color-text-primary)">{s.entity_text}</span>
                    <span className="ml-2 shrink-0 text-gray-400">
                      {s.entity_type} ({s.doc_count})
                    </span>
                  </button>
                ))}
              </div>
            )}
          </div>

          {/* File type dropdown */}
          {filterOptions.mime_types.length > 0 && (
            <select
              value={mimeFilter}
              onChange={(e) => {
                setMimeFilter(e.target.value)
                setCurrentPage(1)
              }}
              className="rounded-lg border border-(--color-border) bg-white dark:bg-[#2c2c2e] px-2 py-1 text-xs text-(--color-text-primary) focus:outline-hidden focus:ring-2 focus:ring-(--color-accent)/30"
            >
              <option value="">All types</option>
              {filterOptions.mime_types.map((m) => (
                <option key={m.value} value={m.value}>
                  {m.value.split('/').pop()} ({m.count})
                </option>
              ))}
            </select>
          )}

          {/* Language dropdown */}
          {filterOptions.languages.length > 0 && (
            <select
              value={langFilter}
              onChange={(e) => {
                setLangFilter(e.target.value)
                setCurrentPage(1)
              }}
              className="rounded-lg border border-(--color-border) bg-white dark:bg-[#2c2c2e] px-2 py-1 text-xs text-(--color-text-primary) focus:outline-hidden focus:ring-2 focus:ring-(--color-accent)/30"
            >
              <option value="">All languages</option>
              {filterOptions.languages.map((l) => (
                <option key={l.value} value={l.value}>
                  {l.value.toUpperCase()} ({l.count})
                </option>
              ))}
            </select>
          )}

          {/* Doc type dropdown */}
          {filterOptions.doc_types.length > 0 && (
            <select
              value={docTypeFilter}
              onChange={(e) => {
                setDocTypeFilter(e.target.value)
                setCurrentPage(1)
              }}
              className="rounded-lg border border-(--color-border) bg-white dark:bg-[#2c2c2e] px-2 py-1 text-xs text-(--color-text-primary) focus:outline-hidden focus:ring-2 focus:ring-(--color-accent)/30"
            >
              <option value="">All categories</option>
              {filterOptions.doc_types.map((d) => (
                <option key={d.value} value={d.value}>
                  {d.value} ({d.count})
                </option>
              ))}
            </select>
          )}

          {/* Sort controls */}
          <div className="ml-auto flex items-center gap-1 text-xs text-gray-400">
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

          {/* Clear all filters */}
          {(mimeFilter || langFilter || docTypeFilter || entityFilter || entityTypeFilter) && (
            <button
              onClick={() => {
                setMimeFilter('')
                setLangFilter('')
                setDocTypeFilter('')
                clearEntityFilter()
                setCurrentPage(1)
              }}
              className="text-xs text-blue-600 dark:text-blue-400 hover:underline"
            >
              Clear filters
            </button>
          )}
        </div>
      )}

      {total === 0 && !hasFilters ? (
        <p className="text-gray-500 dark:text-gray-400">
          No documents yet.{' '}
          <Link to="/upload" className="text-blue-600 dark:text-blue-400 hover:underline">
            Upload one
          </Link>
          .
        </p>
      ) : (
        <>
          {/* Bulk actions bar */}
          {selected.size > 0 && (
            <BulkActionsBar
              count={selected.size}
              isAdmin={isAdmin}
              bulkAction={bulkAction}
              confirmingDelete={confirmingDelete}
              onReprocess={handleBulkReprocess}
              onDelete={handleBulkDelete}
              onCancelDelete={() => setConfirmingDelete(false)}
              onDownload={handleBulkDownload}
              onClear={() => {
                setSelected(new Set())
                setConfirmingDelete(false)
              }}
            />
          )}

          <div className="rounded-xl bg-white dark:bg-[#2c2c2e] shadow-mac overflow-hidden">
            <table className="min-w-full divide-y divide-(--color-border)">
              <thead className="bg-(--color-bg-secondary)">
                <tr>
                  <th className="w-10 px-4 py-3">
                    <input
                      type="checkbox"
                      checked={visibleDocs.length > 0 && visibleDocs.every((d) => selected.has(d.doc_id))}
                      onChange={toggleSelectAll}
                      className="h-3.5 w-3.5 rounded-sm border-gray-300 text-(--color-accent) focus:ring-(--color-accent)/30"
                    />
                  </th>
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
              <tbody className="divide-y divide-(--color-border) bg-white dark:bg-[#2c2c2e]">
                {visibleDocs.map((doc) => {
                  const isExpanded = expanded.has(doc.doc_id)
                  return (
                    <Fragment key={doc.doc_id}>
                      <tr className="hover:bg-black/2 dark:hover:bg-white/2">
                        <td className="w-10 px-4 py-3">
                          <input
                            type="checkbox"
                            checked={selected.has(doc.doc_id)}
                            onChange={() => toggleSelect(doc.doc_id)}
                            className="h-3.5 w-3.5 rounded-sm border-gray-300 text-(--color-accent) focus:ring-(--color-accent)/30"
                          />
                        </td>
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
                                className="font-medium text-blue-600 dark:text-blue-400 hover:underline"
                              >
                                {doc.title}
                              </Link>
                              {doc.canonical_filename && (
                                <div className="text-xs text-gray-400">{doc.canonical_filename}</div>
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
                                className="rounded-sm p-0.5 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"
                                title="Cancel processing"
                              >
                                <svg
                                  className="h-3.5 w-3.5"
                                  fill="none"
                                  viewBox="0 0 24 24"
                                  stroke="currentColor"
                                  strokeWidth={2}
                                >
                                  <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
                                </svg>
                              </button>
                            )}
                          </div>
                        </td>
                        <td className="px-4 py-3 text-sm text-gray-600 dark:text-gray-400">{doc.version_count}</td>
                        <td className="px-4 py-3 text-sm text-gray-500 dark:text-gray-400">
                          {new Date(doc.updated_at).toLocaleDateString()}
                        </td>
                        <td className="px-4 py-3 text-right">
                          <button
                            onClick={() => handleDownload(doc.doc_id)}
                            className="rounded-sm p-1 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 transition-colors"
                            title="Download original"
                          >
                            <svg
                              className="h-4 w-4"
                              fill="none"
                              viewBox="0 0 24 24"
                              stroke="currentColor"
                              strokeWidth={2}
                            >
                              <path
                                strokeLinecap="round"
                                strokeLinejoin="round"
                                d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5M16.5 12L12 16.5m0 0L7.5 12m4.5 4.5V3"
                              />
                            </svg>
                          </button>
                        </td>
                      </tr>
                      {isExpanded && (
                        <tr className="bg-gray-50/50 dark:bg-white/2">
                          <td colSpan={6} className="px-4 py-3 pl-14">
                            <div className="space-y-1 text-sm">
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
                              <div>
                                <span className="font-medium text-gray-500 dark:text-gray-400">Source: </span>
                                {doc.source_path ? (
                                  <span className="text-gray-700 dark:text-gray-300 font-mono text-xs">
                                    {doc.source_path}
                                  </span>
                                ) : (
                                  <span className="italic text-gray-400 dark:text-gray-500">Unknown</span>
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
          {/* Bottom bulk actions bar */}
          {selected.size > 0 && (
            <BulkActionsBar
              count={selected.size}
              isAdmin={isAdmin}
              bulkAction={bulkAction}
              confirmingDelete={confirmingDelete}
              onReprocess={handleBulkReprocess}
              onDelete={handleBulkDelete}
              onCancelDelete={() => setConfirmingDelete(false)}
              onDownload={handleBulkDownload}
              onClear={() => {
                setSelected(new Set())
                setConfirmingDelete(false)
              }}
            />
          )}

          <div className="mt-2 flex items-center justify-center gap-3 text-xs text-gray-400">
            <span>
              Showing {total === 0 ? 0 : startIdx + 1}–{Math.min(startIdx + pageSize, total)} of {total}
              {hasFilters && ' (filtered)'}
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
                className="rounded-sm border-0 bg-gray-100 dark:bg-gray-700/50 px-1.5 py-0.5 text-xs focus:ring-2 focus:ring-(--color-accent)/30"
              >
                {[10, 25, 50, 100].map((n) => (
                  <option key={n} value={n}>
                    {n}
                  </option>
                ))}
              </select>
              per page
            </label>
          </div>
          <Pagination currentPage={currentPage} totalPages={totalPages} onPageChange={setCurrentPage} />
        </>
      )}
    </div>
  )
}

function BulkActionsBar({
  count,
  isAdmin,
  bulkAction,
  confirmingDelete,
  onReprocess,
  onDelete,
  onCancelDelete,
  onDownload,
  onClear,
}: {
  count: number
  isAdmin: boolean
  bulkAction: string
  confirmingDelete: boolean
  onReprocess: () => void
  onDelete: () => void
  onCancelDelete: () => void
  onDownload: () => void
  onClear: () => void
}) {
  return (
    <div className="my-2 flex items-center gap-2 rounded-lg bg-(--color-bg-secondary) px-3 py-2">
      <span className="text-xs font-medium text-(--color-text-secondary)">{count} selected</span>
      <div className="flex-1" />
      {isAdmin && (
        <button
          onClick={onReprocess}
          disabled={!!bulkAction}
          className="rounded-lg border border-gray-300 dark:border-gray-600 px-3 py-1 text-xs font-medium text-gray-700 dark:text-gray-300 hover:bg-white dark:hover:bg-gray-700 disabled:opacity-50"
        >
          {bulkAction === 'reprocess' ? 'Reprocessing...' : 'Reprocess'}
        </button>
      )}
      <button
        onClick={onDownload}
        disabled={!!bulkAction}
        className="rounded-lg border border-gray-300 dark:border-gray-600 px-3 py-1 text-xs font-medium text-gray-700 dark:text-gray-300 hover:bg-white dark:hover:bg-gray-700 disabled:opacity-50"
      >
        {bulkAction === 'download' ? 'Downloading...' : 'Download'}
      </button>
      {isAdmin &&
        (confirmingDelete ? (
          <div className="flex items-center gap-1.5">
            <button
              onClick={onDelete}
              disabled={!!bulkAction}
              className="rounded-lg bg-red-600 px-3 py-1 text-xs font-medium text-white hover:bg-red-700 disabled:opacity-50"
            >
              {bulkAction === 'delete' ? 'Deleting...' : 'Click to confirm'}
            </button>
            <button
              onClick={onCancelDelete}
              className="text-xs text-gray-500 hover:text-gray-700 dark:hover:text-gray-300"
            >
              Cancel
            </button>
          </div>
        ) : (
          <button
            onClick={onDelete}
            disabled={!!bulkAction}
            className="rounded-lg border border-red-300 dark:border-red-700 px-3 py-1 text-xs font-medium text-red-700 dark:text-red-400 hover:bg-red-50 dark:hover:bg-red-900/20 disabled:opacity-50"
          >
            Delete
          </button>
        ))}
      <button onClick={onClear} className="text-xs text-gray-500 hover:text-gray-700 dark:hover:text-gray-300">
        Clear
      </button>
    </div>
  )
}

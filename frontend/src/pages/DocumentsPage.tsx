import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { get, post } from '../api'
import { useAuth } from '../auth'

interface DocSummary {
  doc_id: string
  title: string
  canonical_filename?: string
  status: string
  latest_version_status?: string
  version_count: number
  created_at: string
  updated_at: string
}

const PROCESSING_STATUSES = new Set([
  'queued', 'extracting', 'extracted', 'ocr_running', 'ocr_done',
  'chunking', 'chunked', 'embedding', 'embedded',
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

export default function DocumentsPage() {
  const { isAdmin } = useAuth()
  const [docs, setDocs] = useState<DocSummary[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  function loadDocs() {
    return get<DocSummary[]>('/api/docs')
      .then(setDocs)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    loadDocs()
  }, [])

  async function handleCancel(docId: string) {
    try {
      await post(`/api/docs/${docId}/cancel`)
      loadDocs()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Cancel failed')
    }
  }

  const lastDocRaw = sessionStorage.getItem('lastDoc')
  const lastDoc = lastDocRaw ? JSON.parse(lastDocRaw) as { doc_id: string; title: string } : null

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
        <Link
          to="/upload"
          className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white shadow-sm hover:bg-blue-700"
        >
          Upload
        </Link>
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
              </tr>
            </thead>
            <tbody className="divide-y divide-[var(--color-border)] bg-white dark:bg-[#2c2c2e]">
              {docs.map((doc) => (
                <tr key={doc.doc_id} className="hover:bg-black/[0.02] dark:hover:bg-white/[0.02]">
                  <td className="px-4 py-3">
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
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

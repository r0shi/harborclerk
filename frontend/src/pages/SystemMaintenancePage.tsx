import { useEffect, useState } from 'react'
import { get, post } from '../api'

interface LogFile {
  name: string
  path: string
  size_bytes: number
  modified: string
  service: string
}

interface LogsResponse {
  mode: 'native' | 'docker'
  logs_dir: string | null
  files: LogFile[]
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false)

  return (
    <button
      onClick={() => {
        navigator.clipboard.writeText(text)
        setCopied(true)
        setTimeout(() => setCopied(false), 1500)
      }}
      className="ml-2 shrink-0 rounded px-1.5 py-0.5 text-xs font-medium bg-[var(--color-bg-tertiary)] text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200 transition-colors"
      title="Copy to clipboard"
    >
      {copied ? 'Copied' : 'Copy'}
    </button>
  )
}

export default function SystemMaintenancePage() {
  const [error, setError] = useState('')
  const [actionResult, setActionResult] = useState('')
  const [confirmingReprocess, setConfirmingReprocess] = useState(false)
  const [deleteStep, setDeleteStep] = useState<0 | 1 | 2>(0)
  const [deleteInput, setDeleteInput] = useState('')
  const [deleting, setDeleting] = useState(false)
  const [logs, setLogs] = useState<LogsResponse | null>(null)

  useEffect(() => {
    get<LogsResponse>('/api/system/logs').then(setLogs).catch(() => {})
  }, [])

  async function handlePurge() {
    setError('')
    setActionResult('')
    try {
      const data = await post<{ purged: number }>('/api/system/purge-run')
      setActionResult(`Purge complete: ${data.purged} items removed`)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Purge failed')
    }
  }

  async function handleReaper() {
    setError('')
    setActionResult('')
    try {
      const data = await post<{ reaped: number }>('/api/system/reaper-run')
      setActionResult(`Reaper complete: ${data.reaped} orphaned jobs recovered`)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Reaper failed')
    }
  }

  async function handleReprocessAll() {
    if (!confirmingReprocess) {
      setConfirmingReprocess(true)
      return
    }
    setConfirmingReprocess(false)
    setError('')
    setActionResult('')
    try {
      const data = await post<{ reprocessed: number }>('/api/system/reprocess-all')
      setActionResult(`Reprocess all complete: ${data.reprocessed} documents queued`)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Reprocess all failed')
    }
  }

  // Group log files by service
  const logsByService = logs?.files.reduce<Record<string, LogFile[]>>((acc, f) => {
    ;(acc[f.service] ??= []).push(f)
    return acc
  }, {})

  return (
    <div className="animate-slide-in">
      <h1 className="mb-4 text-xl font-bold">System Maintenance</h1>

      {error && (
        <div className="mb-4 rounded bg-red-50 dark:bg-red-900/20 px-3 py-2 text-sm text-red-700 dark:text-red-400">
          {error}
        </div>
      )}

      {actionResult && (
        <div className="mb-4 rounded bg-green-50 dark:bg-green-900/20 px-3 py-2 text-sm text-green-700 dark:text-green-400">
          {actionResult}
        </div>
      )}

      <div className="space-y-4">
        <div>
          <p className="mb-1.5 text-sm text-gray-600 dark:text-gray-400">Permanently remove documents deleted more than 60 days ago, including stored files.</p>
          <button
            onClick={handlePurge}
            className="rounded-lg bg-[var(--color-bg-tertiary)] px-4 py-2 text-sm font-medium text-gray-700 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-600"
          >
            Purge
          </button>
        </div>
        <div>
          <p className="mb-1.5 text-sm text-gray-600 dark:text-gray-400">Recover ingestion jobs that got stuck due to a crashed or killed worker.</p>
          <button
            onClick={handleReaper}
            className="rounded-lg bg-[var(--color-bg-tertiary)] px-4 py-2 text-sm font-medium text-gray-700 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-600"
          >
            Reap
          </button>
        </div>
        <div>
          <p className="mb-1.5 text-sm text-gray-600 dark:text-gray-400">Re-run the full ingestion pipeline on every document from the original files.</p>
          <div className="flex items-center gap-2">
            <button
              onClick={handleReprocessAll}
              className={`rounded-lg px-4 py-2 text-sm font-medium ${
                confirmingReprocess
                  ? 'bg-red-600 text-white hover:bg-red-700'
                  : 'bg-[var(--color-bg-tertiary)] text-gray-700 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-600'
              }`}
            >
              {confirmingReprocess ? 'Click again to confirm' : 'Reprocess All'}
            </button>
            {confirmingReprocess && (
              <button
                onClick={() => setConfirmingReprocess(false)}
                className="text-sm text-gray-500 hover:text-gray-700 dark:hover:text-gray-300"
              >
                Cancel
              </button>
            )}
          </div>
        </div>

        {/* Logs */}
        {logs && (
          <div className="mt-6 border-t border-[var(--color-border)] pt-6">
            <h2 className="text-lg font-semibold mb-3">Service Logs</h2>
            {logs.mode === 'docker' ? (
              <div className="space-y-2">
                <p className="text-sm text-gray-600 dark:text-gray-400">Logs are sent to stdout in Docker mode. Use the Docker CLI to view them:</p>
                <div className="flex items-center">
                  <code className="block flex-1 rounded bg-[var(--color-bg-tertiary)] px-3 py-2 text-xs font-mono text-[var(--color-text-primary)] select-all">
                    docker compose logs -f app worker-io worker-cpu
                  </code>
                  <CopyButton text="docker compose logs -f app worker-io worker-cpu" />
                </div>
              </div>
            ) : logs.files.length === 0 ? (
              <p className="text-sm text-gray-500">No log files found.</p>
            ) : (
              <div className="space-y-4">
                {logs.logs_dir && (
                  <p className="text-xs text-gray-500 dark:text-gray-400 font-mono">{logs.logs_dir}</p>
                )}
                {logsByService && Object.entries(logsByService).map(([service, files]) => (
                  <div key={service}>
                    <h3 className="text-sm font-medium text-[var(--color-text-primary)] mb-1.5">{service}</h3>
                    <div className="space-y-1.5">
                      {files.map((f) => (
                        <div key={f.name} className="rounded-lg bg-[var(--color-bg-secondary)] p-3">
                          <div className="flex items-center justify-between mb-2">
                            <span className="text-xs font-mono text-gray-600 dark:text-gray-400">{f.name}</span>
                            <span className="text-xs text-gray-500">{formatSize(f.size_bytes)}</span>
                          </div>
                          <div className="flex items-center">
                            <code className="block flex-1 rounded bg-[var(--color-bg-tertiary)] px-3 py-1.5 text-xs font-mono text-[var(--color-text-primary)] select-all overflow-x-auto">
                              tail -f &quot;{f.path}&quot;
                            </code>
                            <CopyButton text={`tail -f "${f.path}"`} />
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {/* Delete All Documents */}
        <div className="mt-6 border-t border-red-200 dark:border-red-800/50 pt-6">
          <h2 className="text-lg font-semibold text-red-700 dark:text-red-400 mb-2">Danger Zone</h2>
          <p className="mb-3 text-sm text-gray-600 dark:text-gray-400">
            Permanently delete <strong>all</strong> documents, versions, chunks, and uploaded files.
            <br />
            Users, API keys, conversations, and audit logs are preserved.
          </p>
          {deleteStep === 0 && (
            <button
              onClick={() => setDeleteStep(1)}
              className="rounded-lg border border-red-300 dark:border-red-700 px-4 py-2 text-sm font-medium text-red-700 dark:text-red-400 hover:bg-red-50 dark:hover:bg-red-900/20"
            >
              Delete All Documents
            </button>
          )}
          {deleteStep === 1 && (
            <div className="flex items-center gap-2">
              <span className="text-sm text-red-600 dark:text-red-400 font-medium">Are you sure?</span>
              <button
                onClick={() => setDeleteStep(2)}
                className="rounded-lg bg-red-600 px-4 py-2 text-sm font-medium text-white hover:bg-red-700"
              >
                Yes, continue
              </button>
              <button
                onClick={() => setDeleteStep(0)}
                className="text-sm text-gray-500 hover:text-gray-700 dark:hover:text-gray-300"
              >
                Cancel
              </button>
            </div>
          )}
          {deleteStep === 2 && (
            <div className="space-y-2">
              <p className="text-sm text-red-600 dark:text-red-400">
                Type <code className="font-mono bg-red-50 dark:bg-red-900/30 px-1.5 py-0.5 rounded text-xs">DELETE EVERYTHING</code> to confirm:
              </p>
              <div className="flex items-center gap-2">
                <input
                  type="text"
                  value={deleteInput}
                  onChange={(e) => setDeleteInput(e.target.value)}
                  placeholder="DELETE EVERYTHING"
                  className="w-56 rounded-lg border border-red-300 dark:border-red-700 bg-white dark:bg-[#2c2c2e] px-3 py-1.5 text-sm text-[var(--color-text-primary)] placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-red-500/30"
                />
                <button
                  onClick={async () => {
                    setDeleting(true)
                    setError('')
                    setActionResult('')
                    try {
                      await post('/api/system/delete-all-documents', { confirmation: deleteInput })
                      setActionResult('All documents deleted successfully')
                      setDeleteStep(0)
                      setDeleteInput('')
                    } catch (e) {
                      setError(e instanceof Error ? e.message : 'Delete failed')
                    } finally {
                      setDeleting(false)
                    }
                  }}
                  disabled={deleteInput !== 'DELETE EVERYTHING' || deleting}
                  className="rounded-lg bg-red-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-red-700 disabled:opacity-50 disabled:cursor-not-allowed"
                >
                  {deleting ? 'Deleting...' : 'Delete'}
                </button>
                <button
                  onClick={() => { setDeleteStep(0); setDeleteInput('') }}
                  className="text-sm text-gray-500 hover:text-gray-700 dark:hover:text-gray-300"
                >
                  Cancel
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

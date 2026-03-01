import { useEffect, useState } from 'react'
import { get } from '../api'

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

export default function ServiceLogsPage() {
  const [logs, setLogs] = useState<LogsResponse | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    get<LogsResponse>('/api/system/logs')
      .then(setLogs)
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [])

  // Group log files by service
  const logsByService = logs?.files.reduce<Record<string, LogFile[]>>((acc, f) => {
    ;(acc[f.service] ??= []).push(f)
    return acc
  }, {})

  return (
    <div className="animate-slide-in">
      <h1 className="mb-4 text-xl font-bold">Service Logs</h1>

      {loading ? (
        <p className="text-sm text-gray-500">Loading...</p>
      ) : !logs ? (
        <p className="text-sm text-gray-500">Failed to load log information.</p>
      ) : logs.mode === 'docker' ? (
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
                    <div className="flex items-center justify-between mb-1">
                      <span className="text-xs font-mono text-gray-500 dark:text-gray-400">{f.path}</span>
                      <span className="text-xs text-gray-500 ml-3 shrink-0">{formatSize(f.size_bytes)}</span>
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
  )
}

import { useState } from 'react'
import { post } from '../api'

export default function SystemMaintenancePage() {
  const [error, setError] = useState('')
  const [actionResult, setActionResult] = useState('')

  async function handlePurge() {
    setActionResult('')
    try {
      const data = await post<{ purged: number }>('/api/system/purge-run')
      setActionResult(`Purge complete: ${data.purged} items removed`)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Purge failed')
    }
  }

  async function handleReaper() {
    setActionResult('')
    try {
      const data = await post<{ reaped: number }>('/api/system/reaper-run')
      setActionResult(`Reaper complete: ${data.reaped} orphaned jobs recovered`)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Reaper failed')
    }
  }

  async function handleReprocessAll() {
    if (!window.confirm('This will reprocess every document from scratch and could take a long time. Continue?')) return
    setActionResult('')
    try {
      const data = await post<{ reprocessed: number }>('/api/system/reprocess-all')
      setActionResult(`Reprocess all complete: ${data.reprocessed} documents queued`)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Reprocess all failed')
    }
  }

  return (
    <div>
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
          <button
            onClick={handleReprocessAll}
            className="rounded-lg bg-[var(--color-bg-tertiary)] px-4 py-2 text-sm font-medium text-gray-700 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-600"
          >
            Reprocess All
          </button>
        </div>
      </div>
    </div>
  )
}

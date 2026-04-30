import { useEffect, useState } from 'react'
import { del, get, patch, post, ApiError } from '../api'
import { useFolderProgress } from '../hooks/useFolderProgress'

interface FolderInfo {
  folder_id: string
  path: string
  display_name: string | null
  enabled: boolean
  auto_discovered: boolean
  unavailable_reason: string | null
}

interface StageCounts {
  pending: number
  running: number
  done: number
  error: number
}

interface ProgressInfo {
  total_files: number
  completed_files: number
  by_stage: Record<string, StageCounts>
  scan_status: 'scanning' | 'idle'
  last_scan_at: string | null
}

interface SystemInfo {
  platform: 'macos' | 'docker'
  picker: 'native' | 'none'
  watch_root: string | null
}

declare global {
  interface Window {
    harborclerk?: { pickFolder: () => Promise<string | null> }
  }
}

function errorMessage(e: unknown, fallback: string): string {
  if (e instanceof ApiError || e instanceof Error) return e.message
  return fallback
}

export default function FoldersPage() {
  const [system, setSystem] = useState<SystemInfo | null>(null)
  const [folders, setFolders] = useState<FolderInfo[]>([])
  const [progress, setProgress] = useState<Record<string, ProgressInfo>>({})
  const [expanded, setExpanded] = useState<string | null>(null)
  const [error, setError] = useState('')

  async function reload() {
    try {
      const [sys, fs] = await Promise.all([
        get<SystemInfo>('/api/watch/system'),
        get<FolderInfo[]>('/api/watch/folders'),
      ])
      setSystem(sys)
      setFolders(fs)
      const progs = await Promise.all(fs.map((f) => get<ProgressInfo>(`/api/watch/folders/${f.folder_id}/progress`)))
      setProgress(Object.fromEntries(fs.map((f, i) => [f.folder_id, progs[i]])))
    } catch (e) {
      setError(errorMessage(e, 'Failed to load'))
    }
  }

  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const [sys, fs] = await Promise.all([
          get<SystemInfo>('/api/watch/system'),
          get<FolderInfo[]>('/api/watch/folders'),
        ])
        if (cancelled) return
        setSystem(sys)
        setFolders(fs)
        const progs = await Promise.all(fs.map((f) => get<ProgressInfo>(`/api/watch/folders/${f.folder_id}/progress`)))
        if (cancelled) return
        setProgress(Object.fromEntries(fs.map((f, i) => [f.folder_id, progs[i]])))
      } catch (e) {
        if (!cancelled) setError(errorMessage(e, 'Failed to load'))
      }
    }
    load()
    return () => {
      cancelled = true
    }
  }, [])

  // Live updates via SSE: merge incoming snapshots into the per-folder progress map.
  // Only updates folders we already have full progress for; new folders show up via reload().
  useFolderProgress((event) => {
    setProgress((prev) => {
      const existing = prev[event.folder_id]
      if (!existing) return prev
      return {
        ...prev,
        [event.folder_id]: {
          ...existing,
          total_files: event.total_files,
          completed_files: event.completed_files,
          scan_status: event.scan_status,
        },
      }
    })
  })

  async function handleAdd() {
    if (system?.picker !== 'native' || !window.harborclerk) {
      // Native bridge not available (e.g., dev in plain browser); silently no-op.
      return
    }
    try {
      const path = await window.harborclerk.pickFolder()
      if (!path) return
      await post('/api/watch/folders', { path })
      reload()
    } catch (e) {
      setError(errorMessage(e, 'Add failed'))
    }
  }

  async function handleDelete(folderId: string) {
    if (!confirm('Remove this folder? Documents already ingested will stay queryable.')) return
    try {
      await del(`/api/watch/folders/${folderId}`)
      reload()
    } catch (e) {
      setError(errorMessage(e, 'Delete failed'))
    }
  }

  async function handleToggle(folder: FolderInfo) {
    try {
      await patch(`/api/watch/folders/${folder.folder_id}`, { enabled: !folder.enabled })
      reload()
    } catch (e) {
      setError(errorMessage(e, 'Toggle failed'))
    }
  }

  if (!system) return <div className="text-sm text-gray-500">Loading...</div>

  return (
    <div className="animate-slide-in">
      <div className="mb-4 flex items-center justify-between">
        <h1 className="text-xl font-bold">Folders</h1>
        {system.picker === 'native' ? (
          <button
            onClick={handleAdd}
            className="rounded-lg bg-blue-600 px-3 py-1 text-xs font-medium text-white shadow-xs hover:bg-blue-700"
          >
            Add Folder
          </button>
        ) : null}
      </div>

      {system.picker === 'none' && (
        <div className="mb-4 rounded-md bg-blue-50 dark:bg-blue-900/20 p-3 text-xs text-blue-700 dark:text-blue-400">
          Folders are managed by mounting them under <code>{system.watch_root}</code> in your Docker setup.{' '}
          <a href="/docs/watched-folders-docker" className="underline">
            How to add a folder →
          </a>
        </div>
      )}

      {error && (
        <div className="mb-4 rounded-sm bg-red-50 dark:bg-red-900/20 px-3 py-2 text-sm text-red-700 dark:text-red-400">
          {error}
        </div>
      )}

      <div className="overflow-hidden rounded-xl bg-white dark:bg-[#2c2c2e] shadow-mac ring-1 ring-(--color-border)">
        <table className="w-full text-sm">
          <thead className="bg-(--color-bg-secondary)">
            <tr>
              <th className="px-4 py-3 text-left">Folder</th>
              <th className="px-4 py-3 text-left">Status</th>
              <th className="px-4 py-3 text-left">Progress</th>
              <th className="px-4 py-3 text-right">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-(--color-border)">
            {folders.length === 0 && (
              <tr>
                <td colSpan={4} className="px-4 py-8 text-center text-sm text-gray-500">
                  No folders yet.{' '}
                  {system.picker === 'native'
                    ? 'Click Add Folder to start watching one.'
                    : 'Mount a folder under WATCH_ROOT to begin.'}
                </td>
              </tr>
            )}
            {folders.map((f) => {
              const p = progress[f.folder_id]
              const isExpanded = expanded === f.folder_id
              const deleteDisabled = f.auto_discovered && f.unavailable_reason === null
              return (
                <FolderRow
                  key={f.folder_id}
                  folder={f}
                  progress={p}
                  isExpanded={isExpanded}
                  deleteDisabled={deleteDisabled}
                  onToggleExpand={() => setExpanded(isExpanded ? null : f.folder_id)}
                  onToggleEnabled={() => handleToggle(f)}
                  onDelete={() => handleDelete(f.folder_id)}
                />
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}

interface FolderRowProps {
  folder: FolderInfo
  progress?: ProgressInfo
  isExpanded: boolean
  deleteDisabled: boolean
  onToggleExpand: () => void
  onToggleEnabled: () => void
  onDelete: () => void
}

function FolderRow({
  folder: f,
  progress: p,
  isExpanded,
  deleteDisabled,
  onToggleExpand,
  onToggleEnabled,
  onDelete,
}: FolderRowProps) {
  return (
    <>
      <tr className="cursor-pointer" onClick={onToggleExpand}>
        <td className="px-4 py-3">
          <div className="font-medium">{f.display_name || f.path}</div>
          <div className="text-xs text-gray-500 font-mono">{f.path}</div>
          {f.auto_discovered && (
            <span className="inline-flex items-center mt-1 rounded-md bg-purple-100 dark:bg-purple-900/30 px-2 py-0.5 text-[11px] text-purple-700 dark:text-purple-400">
              auto-discovered
            </span>
          )}
        </td>
        <td className="px-4 py-3">{renderStatusPill(f, p)}</td>
        <td className="px-4 py-3 text-xs">{p ? `${p.completed_files} / ${p.total_files}` : '—'}</td>
        <td className="px-4 py-3 text-right">
          <button
            onClick={(e) => {
              e.stopPropagation()
              onToggleEnabled()
            }}
            className="mr-2 rounded-lg border border-gray-400 px-2 py-1 text-xs"
          >
            {f.enabled ? 'Disable' : 'Enable'}
          </button>
          <button
            disabled={deleteDisabled}
            title={deleteDisabled ? 'Active Docker mount — unmount first' : ''}
            onClick={(e) => {
              e.stopPropagation()
              onDelete()
            }}
            className="rounded-lg bg-red-600 px-2 py-1 text-xs text-white disabled:opacity-50 disabled:cursor-not-allowed"
          >
            Delete
          </button>
        </td>
      </tr>
      {isExpanded && p && (
        <tr>
          <td colSpan={4} className="bg-(--color-bg-secondary) px-4 py-3">
            <div className="grid grid-cols-7 gap-2 text-xs">
              {Object.entries(p.by_stage).map(([stage, counts]) => {
                const total = counts.done + counts.pending + counts.running + counts.error
                return (
                  <div key={stage}>
                    <div className="font-medium capitalize">{stage}</div>
                    <div>
                      {counts.done}/{total}
                    </div>
                    {counts.error > 0 && <div className="text-red-600">{counts.error} err</div>}
                  </div>
                )
              })}
            </div>
          </td>
        </tr>
      )}
    </>
  )
}

function renderStatusPill(f: FolderInfo, p?: ProgressInfo) {
  if (f.unavailable_reason === 'unmounted') {
    return (
      <span className="rounded-md bg-red-100 dark:bg-red-900/30 px-2 py-0.5 text-[11px] text-red-700 dark:text-red-400">
        unmounted
      </span>
    )
  }
  if (!f.enabled) {
    return (
      <span className="rounded-md bg-gray-100 dark:bg-gray-700 px-2 py-0.5 text-[11px] text-gray-600 dark:text-gray-300">
        disabled
      </span>
    )
  }
  if (p?.scan_status === 'scanning') {
    return (
      <span className="rounded-md bg-amber-100 dark:bg-amber-900/30 px-2 py-0.5 text-[11px] text-amber-700 dark:text-amber-400">
        scanning
      </span>
    )
  }
  return (
    <span className="rounded-md bg-green-100 dark:bg-green-900/30 px-2 py-0.5 text-[11px] text-green-700 dark:text-green-400">
      idle
    </span>
  )
}

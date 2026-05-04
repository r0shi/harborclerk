import { useEffect, useState } from 'react'
import { del, get, patch, post, ApiError } from '../api'
import { useFolderProgress } from '../hooks/useFolderProgress'
import { PageHeader } from '../components/PageHeader'
import { Card } from '../components/Card'
import { StatusPill, type PillState } from '../components/StatusPill'
import { IconTile } from '../components/IconTile'

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

// window.harborclerk typing lives in src/types/harborclerk.d.ts (shared
// across pages/components that touch the native bridge).

function errorMessage(e: unknown, fallback: string): string {
  if (e instanceof ApiError || e instanceof Error) return e.message
  return fallback
}

// Precedence: a disabled folder shouldn't show "scanning" even if a stale scan is in flight.
function mapFolderStatusToPillState(f: FolderInfo, p?: ProgressInfo): PillState {
  if (f.unavailable_reason) return 'error'
  if (!f.enabled) return 'idle'
  if (p?.scan_status === 'scanning') return 'running'
  return 'active'
}

function mapFolderStatusLabel(f: FolderInfo, p?: ProgressInfo): string {
  if (f.unavailable_reason === 'unmounted') return 'unmounted'
  if (f.unavailable_reason) return 'unavailable'
  if (!f.enabled) return 'disabled'
  if (p?.scan_status === 'scanning') return 'scanning'
  return 'idle'
}

function AddFolderButton({ onClick }: { onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className="inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-medium"
      style={{
        backgroundColor: 'var(--area-accent-tint)',
        color: 'var(--area-accent-text)',
        border: '1px solid var(--area-accent)',
      }}
    >
      <span aria-hidden>＋</span> Add Folder
    </button>
  )
}

export default function FoldersPage() {
  const [system, setSystem] = useState<SystemInfo | null>(null)
  const [folders, setFolders] = useState<FolderInfo[]>([])
  const [progress, setProgress] = useState<Record<string, ProgressInfo>>({})
  const [expanded, setExpanded] = useState<string | null>(null)
  const [error, setError] = useState('')
  // Two-click delete confirmation. WKWebView (the macOS native app's
  // chrome) returns false from window.confirm() silently, which made
  // the Delete button look broken. The state tracks which folder is
  // armed for delete; first click sets it, second confirms.
  const [pendingDeleteId, setPendingDeleteId] = useState<string | null>(null)

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

  async function handleDeleteClick(folderId: string) {
    // First click: arm. Second click on the same row: actually delete.
    if (pendingDeleteId !== folderId) {
      setPendingDeleteId(folderId)
      // Auto-cancel after 5 s so the row doesn't sit in a "Confirm?" state forever.
      setTimeout(() => {
        setPendingDeleteId((cur) => (cur === folderId ? null : cur))
      }, 5000)
      return
    }
    setPendingDeleteId(null)
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

  if (!system) return <div className="text-sm text-(--color-text-secondary)">Loading…</div>

  const addButton = system.picker === 'native' ? <AddFolderButton onClick={handleAdd} /> : null

  return (
    <div className="animate-slide-in">
      <PageHeader title="Folders" actions={addButton} />

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

      {folders.length === 0 ? (
        <Card className="mx-auto mt-8 flex max-w-md flex-col items-center gap-3 p-8 text-center">
          <IconTile size={56}>📁</IconTile>
          <h3 className="font-serif text-lg font-semibold">No folders yet</h3>
          <p className="text-sm text-(--color-text-secondary)">
            {system.picker === 'native'
              ? 'Add a folder to start watching for documents to ingest.'
              : 'Mount a folder under WATCH_ROOT to begin.'}
          </p>
          {system.picker === 'native' && <AddFolderButton onClick={handleAdd} />}
        </Card>
      ) : (
        <div>
          {folders.map((f) => {
            const p = progress[f.folder_id]
            const isExpanded = expanded === f.folder_id
            const deleteDisabled = f.auto_discovered && f.unavailable_reason === null
            const pendingDelete = pendingDeleteId === f.folder_id
            return (
              <FolderRow
                key={f.folder_id}
                folder={f}
                progress={p}
                isExpanded={isExpanded}
                deleteDisabled={deleteDisabled}
                pendingDelete={pendingDelete}
                onToggleExpand={() => setExpanded(isExpanded ? null : f.folder_id)}
                onToggleEnabled={() => handleToggle(f)}
                onDelete={() => handleDeleteClick(f.folder_id)}
                onCancelDelete={() => setPendingDeleteId(null)}
              />
            )
          })}
        </div>
      )}
    </div>
  )
}

interface FolderRowProps {
  folder: FolderInfo
  progress?: ProgressInfo
  isExpanded: boolean
  deleteDisabled: boolean
  pendingDelete: boolean
  onToggleExpand: () => void
  onToggleEnabled: () => void
  onDelete: () => void
  onCancelDelete: () => void
}

function FolderRow({
  folder: f,
  progress: p,
  isExpanded,
  deleteDisabled,
  pendingDelete,
  onToggleExpand,
  onToggleEnabled,
  onDelete,
  onCancelDelete,
}: FolderRowProps) {
  return (
    <Card className="mb-2" interactive>
      <div className="flex cursor-pointer items-center gap-3 px-3.5 py-3" onClick={onToggleExpand}>
        <IconTile size={30}>📁</IconTile>
        <div className="min-w-0 flex-1">
          <div className="truncate font-medium text-(--color-text-primary)">{f.display_name || f.path}</div>
          <div className="flex items-center gap-2 text-[11px] text-(--color-text-secondary)">
            <span className="truncate font-mono">{f.path}</span>
            {f.auto_discovered && (
              <span className="inline-flex shrink-0 items-center rounded-md bg-purple-100 dark:bg-purple-900/30 px-2 py-0.5 text-[11px] text-purple-700 dark:text-purple-400">
                auto-discovered
              </span>
            )}
          </div>
          {p && (
            <div className="mt-0.5 text-[11px] text-(--color-text-secondary)">
              {p.completed_files} / {p.total_files} files
            </div>
          )}
        </div>
        <StatusPill state={mapFolderStatusToPillState(f, p)} label={mapFolderStatusLabel(f, p)} />
        <div className="flex shrink-0 items-center gap-2" onClick={(e) => e.stopPropagation()}>
          {!pendingDelete && (
            <button
              onClick={onToggleEnabled}
              className="rounded-lg border border-(--color-border) px-2 py-1 text-xs text-(--color-text-secondary) hover:text-(--color-text-primary)"
            >
              {f.enabled ? 'Disable' : 'Enable'}
            </button>
          )}
          {pendingDelete ? (
            <>
              <button
                onClick={onCancelDelete}
                className="rounded-lg border border-(--color-border) px-2 py-1 text-xs text-(--color-text-secondary) hover:text-(--color-text-primary)"
              >
                Cancel
              </button>
              <button onClick={onDelete} className="rounded-lg bg-red-600 px-2 py-1 text-xs font-medium text-white">
                Confirm Delete
              </button>
            </>
          ) : (
            <button
              disabled={deleteDisabled}
              title={deleteDisabled ? 'Active Docker mount — unmount first' : ''}
              onClick={onDelete}
              className="rounded-lg bg-red-600 px-2 py-1 text-xs text-white disabled:cursor-not-allowed disabled:opacity-50"
            >
              Delete
            </button>
          )}
        </div>
      </div>
      {isExpanded && p && (
        <div className="border-t border-(--color-border) bg-(--color-bg-secondary) px-4 py-3">
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
        </div>
      )}
    </Card>
  )
}

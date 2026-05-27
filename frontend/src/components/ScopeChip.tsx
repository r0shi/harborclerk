import type { WatchedFolderInfo } from '../hooks/useWatchedFolders'

export interface ScopeChipProps {
  scope: { folder_ids?: string[] } | null | undefined
  folders: WatchedFolderInfo[]
  className?: string
}

export function ScopeChip({ scope, folders, className }: ScopeChipProps) {
  const ids = scope?.folder_ids ?? []
  if (ids.length === 0) {
    return (
      <span
        className={
          className ??
          'inline-flex items-center rounded-md border border-[var(--color-border)] bg-[var(--color-bg-secondary)] px-2 py-1 text-xs font-medium text-[var(--color-text-primary)]'
        }
      >
        Folders: All
      </span>
    )
  }
  const labels = ids.map((id) => folders.find((f) => f.folder_id === id)?.display_name ?? '?').slice(0, 3)
  const suffix = ids.length > 1 ? ` (${ids.length})` : ''
  return (
    <span
      className={
        className ??
        'inline-flex items-center rounded-md border border-[var(--color-border)] bg-[var(--color-bg-secondary)] px-2 py-1 text-xs font-medium text-[var(--color-text-primary)]'
      }
    >
      {`Folders: ${labels.join(', ')}${suffix}`}
    </span>
  )
}

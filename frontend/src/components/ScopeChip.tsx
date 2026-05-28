import type { WatchedFolderInfo } from '../hooks/useWatchedFolders'
import { disambiguateLabels } from './folderLabels'

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
  const labelMap = disambiguateLabels(folders)
  const labels = ids.map((id) => labelMap.get(id) ?? '?').slice(0, 3)
  const suffix = ids.length > 1 ? ` (${ids.length})` : ''
  const fullPaths = ids
    .map((id) => folders.find((f) => f.folder_id === id)?.path)
    .filter((p): p is string => p != null)
    .join('\n')
  return (
    <span
      className={
        className ??
        'inline-flex items-center rounded-md border border-[var(--color-border)] bg-[var(--color-bg-secondary)] px-2 py-1 text-xs font-medium text-[var(--color-text-primary)]'
      }
      title={fullPaths}
    >
      {`Folders: ${labels.join(', ')}${suffix}`}
    </span>
  )
}

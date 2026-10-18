// `unavailable_reason` on a watched folder. Mirrors harbor_clerk/models/watched.py.
//
// Docker discovery sets 'unmounted' (and disables the folder) when a mount
// under WATCH_ROOT goes away: only that makes a folder unavailable for use.
// The watcher's reasons describe a pass it had to skip; the folder's
// documents stay searchable, so it can still be scoped, picked, and is still
// protected from deletion while auto-discovered.

export const UNMOUNTED = 'unmounted'

const WATCHER_REASON_LABELS: Record<string, string> = {
  root_missing: 'folder missing',
  root_unreadable: 'not readable',
  root_empty: 'empty',
}

/** Whether a folder with this reason is unavailable for use (scoping, picking, deletion protection). */
export function folderIsUnavailable(reason: string | null | undefined): boolean {
  return reason === UNMOUNTED
}

/** An auto-discovered Docker mount can be deleted only once it is unmounted (the API answers 409 otherwise). */
export function folderDeleteProtected(folder: {
  auto_discovered: boolean
  unavailable_reason: string | null
}): boolean {
  return folder.auto_discovered && !folderIsUnavailable(folder.unavailable_reason)
}

/** A short label for a folder's reason, or null when it has none. */
export function unavailableReasonLabel(reason: string | null | undefined): string | null {
  if (!reason) return null
  if (reason === UNMOUNTED) return 'unmounted'
  return WATCHER_REASON_LABELS[reason] ?? 'unavailable'
}

// How a count of folders with each note reads in a summary line ("1 not found").
const NOTE_COUNT_LABELS: Record<string, string> = {
  root_missing: 'not found',
  root_unreadable: 'not readable',
  root_empty: 'empty',
}

/**
 * The status summary's informational folder notes as one line, e.g. "1 not found · 2 empty",
 * or null when there are none. Never an error: these folders' documents stay searchable.
 */
export function folderNotesSummary(notes: Record<string, number> | null | undefined): string | null {
  const parts = Object.entries(notes ?? {})
    .filter(([, count]) => count > 0)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([reason, count]) => `${count} ${NOTE_COUNT_LABELS[reason] ?? 'with a note'}`)
  return parts.length ? parts.join(' · ') : null
}

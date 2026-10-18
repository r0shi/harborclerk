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

interface FolderStatusInput {
  enabled: boolean
  unavailable_reason: string | null
}

interface FolderProgressInput {
  scan_status?: string
  ingest_status?: string
}

type FolderPill = 'active' | 'running' | 'idle' | 'error' | 'pending'

/**
 * The Folders page pill. Precedence: unmounted is the one error; a disabled folder
 * shows neither a note nor "scanning"/"processing" even if a stale scan is in flight;
 * a watcher note (root missing, unreadable or empty) is informational, never red.
 */
export function folderPillState(f: FolderStatusInput, p?: FolderProgressInput): FolderPill {
  if (folderIsUnavailable(f.unavailable_reason)) return 'error'
  if (!f.enabled) return 'idle'
  if (f.unavailable_reason) return 'pending'
  if (p?.scan_status === 'scanning' || p?.ingest_status === 'processing') return 'running'
  return 'active'
}

/** The Folders page status label: unmounted, then disabled, then the watcher's note, then activity. */
export function folderStatusLabel(f: FolderStatusInput, p?: FolderProgressInput): string {
  if (folderIsUnavailable(f.unavailable_reason)) return 'unmounted'
  if (!f.enabled) return 'disabled'
  const reasonLabel = unavailableReasonLabel(f.unavailable_reason)
  if (reasonLabel) return reasonLabel
  if (p?.scan_status === 'scanning') return 'scanning'
  if (p?.ingest_status === 'processing') return 'processing'
  return 'idle'
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

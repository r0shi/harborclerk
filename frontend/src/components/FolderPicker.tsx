import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { WatchedFolderInfo } from '../hooks/useWatchedFolders'

export interface FolderPickerProps {
  value: string[]
  onChange: (folder_ids: string[]) => void
  folders: WatchedFolderInfo[]
  disabled?: boolean
  size?: 'sm' | 'md'
}

function summarize(value: string[], folders: WatchedFolderInfo[]): string {
  if (value.length === 0) return 'Folders: All'
  const labels = value.map((id) => folders.find((f) => f.folder_id === id)?.display_name ?? '?').slice(0, 3)
  const suffix = value.length > 1 ? ` (${value.length})` : ''
  return `Folders: ${labels.join(', ')}${suffix}`
}

export function FolderPicker({ value, onChange, folders, disabled, size = 'md' }: FolderPickerProps) {
  const [open, setOpen] = useState(false)
  const [filter, setFilter] = useState('')
  const containerRef = useRef<HTMLDivElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const [popDirection, setPopDirection] = useState<'down' | 'up'>('down')

  const visible = useMemo(
    () =>
      folders.filter((f) =>
        filter.trim() === '' ? true : (f.display_name ?? '').toLowerCase().includes(filter.toLowerCase()),
      ),
    [folders, filter],
  )

  // Close popover on outside click or Escape
  useEffect(() => {
    if (!open) return
    function onMouseDown(e: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onMouseDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('mousedown', onMouseDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [open])

  useLayoutEffect(() => {
    if (!open || !triggerRef.current) return
    const rect = triggerRef.current.getBoundingClientRect()
    const popoverMaxHeight = 360 // matches the max-h cap on the popover div
    const margin = 8
    const spaceBelow = window.innerHeight - rect.bottom - margin
    const spaceAbove = rect.top - margin
    setPopDirection(spaceBelow >= popoverMaxHeight || spaceBelow >= spaceAbove ? 'down' : 'up')
  }, [open])

  const noFolders = folders.length === 0
  const isDisabled = disabled || noFolders
  const buttonText = noFolders ? 'No folders to scope to' : summarize(value, folders)

  const toggle = (id: string) => {
    if (value.includes(id)) onChange(value.filter((v) => v !== id))
    else onChange([...value, id])
  }

  const sizeClasses = size === 'sm' ? 'px-2 py-1 text-xs' : 'px-3 py-1.5 text-sm'

  return (
    <div className="relative inline-block" ref={containerRef}>
      <button
        ref={triggerRef}
        type="button"
        onClick={() => !isDisabled && setOpen((o) => !o)}
        disabled={isDisabled}
        aria-expanded={open}
        aria-haspopup="listbox"
        className={[
          sizeClasses,
          'inline-flex items-center gap-1.5 rounded-md border font-medium transition-colors',
          'border-[var(--color-border)] bg-[var(--color-bg-secondary)] text-[var(--color-text-primary)]',
          'hover:bg-[var(--color-bg-tertiary)]',
          'disabled:cursor-not-allowed disabled:opacity-40',
        ].join(' ')}
      >
        <span>{buttonText}</span>
        {!isDisabled && (
          <svg
            aria-hidden
            width="10"
            height="10"
            viewBox="0 0 10 10"
            fill="currentColor"
            className={`transition-transform ${open ? 'rotate-180' : ''}`}
          >
            <path d="M1.5 3.5L5 7l3.5-3.5" stroke="currentColor" strokeWidth="1.5" fill="none" strokeLinecap="round" />
          </svg>
        )}
      </button>

      {open && !noFolders && (
        <div
          className={[
            'absolute z-30 min-w-[200px] max-h-[360px] rounded-lg shadow-mac-lg overflow-hidden',
            popDirection === 'up' ? 'bottom-full mb-1' : 'top-full mt-1',
            'border border-[var(--color-border)] bg-[var(--color-bg-primary)]',
            'flex flex-col',
          ].join(' ')}
          role="dialog"
          aria-label="Folder filter"
        >
          {/* Search */}
          <div className="px-2 pt-2">
            <input
              type="text"
              placeholder="Search folders…"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              className={[
                'w-full rounded-md border border-[var(--color-border)]',
                'bg-[var(--color-bg-secondary)] px-2 py-1 text-xs',
                'text-[var(--color-text-primary)] placeholder:text-[var(--color-text-secondary)]',
                'outline-none focus:ring-1 focus:ring-[var(--color-accent)]',
              ].join(' ')}
              autoFocus
            />
          </div>

          {/* Select all / Clear */}
          <div className="flex items-center gap-2 px-3 pt-2 pb-1">
            <button
              type="button"
              onClick={() => onChange(folders.map((f) => f.folder_id))}
              className="text-xs text-[var(--color-accent)] hover:underline"
            >
              Select all
            </button>
            <span className="text-[var(--color-text-secondary)] text-xs">·</span>
            <button
              type="button"
              onClick={() => onChange([])}
              className="text-xs text-[var(--color-accent)] hover:underline"
            >
              Clear
            </button>
          </div>

          {/* Folder list */}
          <ul role="listbox" aria-multiselectable="true" className="max-h-48 overflow-y-auto px-1 pb-1">
            {visible.map((f) => (
              <li key={f.folder_id} role="option" aria-selected={value.includes(f.folder_id)}>
                <label className="flex cursor-pointer items-center gap-2 rounded px-2 py-1 hover:bg-[var(--color-bg-secondary)]">
                  <input
                    type="checkbox"
                    checked={value.includes(f.folder_id)}
                    onChange={() => toggle(f.folder_id)}
                    aria-label={f.display_name ?? f.folder_id}
                    className="h-3.5 w-3.5 accent-[var(--color-accent)]"
                  />
                  <span className="text-sm text-[var(--color-text-primary)] truncate">
                    {f.display_name ?? f.folder_id}
                  </span>
                </label>
              </li>
            ))}
            {visible.length === 0 && (
              <li className="px-3 py-2 text-xs text-[var(--color-text-secondary)]">No folders match</li>
            )}
          </ul>
        </div>
      )}
    </div>
  )
}

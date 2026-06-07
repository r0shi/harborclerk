import type { SourceRef } from '../types/sourceRef'

interface SourceCitationProps {
  source?: SourceRef | null
  citation?: string | null
  className?: string
}

function citationText(source?: SourceRef | null, citation?: string | null): string | null {
  return source?.citation || citation || source?.source_label || null
}

function fileNameFromPath(path?: string | null): string | null {
  if (!path) return null
  const parts = path.split(/[\\/]/).filter(Boolean)
  return parts[parts.length - 1] || path
}

export function SourceCitation({ source, citation, className = '' }: SourceCitationProps) {
  const text = citationText(source, citation)
  const fileName = fileNameFromPath(source?.relative_path)
  if (!text && !source?.relative_path && !source?.folder_label) return null

  return (
    <span className={`inline-flex min-w-0 max-w-full flex-col gap-1 ${className}`}>
      {text && <span className="min-w-0 font-medium text-(--color-text-secondary)">{text}</span>}
      {(fileName || source?.folder_label) && (
        <span className="inline-flex min-w-0 max-w-full flex-wrap items-center gap-1.5">
          {fileName && (
            <span
              className="inline-flex min-w-0 max-w-full items-center gap-1 rounded-md border border-(--color-border) bg-(--color-bg-secondary) px-1.5 py-0.5"
              title={source?.relative_path || fileName}
            >
              <span className="shrink-0 text-[10px] font-medium text-gray-500 dark:text-gray-400">Filename</span>
              <span className="min-w-0 max-w-[32rem] truncate font-mono text-[11px] text-(--color-text-secondary)">
                {fileName}
              </span>
            </span>
          )}
          {source?.folder_label && (
            <span className="text-[11px] text-gray-500 dark:text-gray-400">Folder {source.folder_label}</span>
          )}
        </span>
      )}
      {source?.chunk_id && <span className="sr-only">Chunk {source.chunk_id}</span>}
    </span>
  )
}

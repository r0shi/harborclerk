import type { SourceRef } from '../types/sourceRef'

interface SourceCitationProps {
  source?: SourceRef | null
  citation?: string | null
  className?: string
}

function citationText(source?: SourceRef | null, citation?: string | null): string | null {
  return source?.citation || citation || source?.source_label || null
}

export function SourceCitation({ source, citation, className = '' }: SourceCitationProps) {
  const text = citationText(source, citation)
  if (!text && !source?.relative_path && !source?.folder_label) return null

  return (
    <span className={`inline-flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 ${className}`}>
      {text && <span className="font-medium text-(--color-text-secondary)">{text}</span>}
      {source?.folder_label && <span>{source.folder_label}</span>}
      {source?.relative_path && (
        <span className="min-w-0 max-w-[32rem] truncate font-mono text-[11px]" title={source.relative_path}>
          {source.relative_path}
        </span>
      )}
      {source?.chunk_id && <span className="sr-only">Chunk {source.chunk_id}</span>}
    </span>
  )
}

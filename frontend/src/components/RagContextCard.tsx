import { useState } from 'react'
import { Link } from 'react-router-dom'
import type { RagContextChunk } from '../hooks/useChat'

function formatPages(start: number | null, end: number | null): string | null {
  if (start == null) return null
  return start === end ? `p.\u00A0${start}` : `pp.\u00A0${start}\u2013${end}`
}

function scoreDots(score: number): number {
  if (score >= 0.7) return 4
  if (score >= 0.5) return 3
  if (score >= 0.35) return 2
  return 1
}

function summarize(chunks: RagContextChunk[]): string {
  const docIds = new Set(chunks.map((c) => c.doc_id))
  const nChunks = chunks.length
  const nDocs = docIds.size
  const passageWord = nChunks === 1 ? 'passage' : 'passages'
  const docWord = nDocs === 1 ? 'document' : 'documents'
  return `${nChunks} ${passageWord} from ${nDocs} ${docWord}`
}

export default function RagContextCard({ chunks }: { chunks: RagContextChunk[] }) {
  const [expanded, setExpanded] = useState(false)

  if (!chunks.length) return null

  return (
    <div className="rag-context-card mb-2 rounded-lg overflow-hidden ring-1 ring-stone-200/50 dark:ring-stone-700/30 bg-stone-50/40 dark:bg-stone-800/20">
      {/* Header — always visible */}
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex w-full items-center gap-2 px-2.5 py-1.5 text-[11px] text-stone-500 dark:text-stone-400 hover:text-stone-700 dark:hover:text-stone-300 transition-colors duration-150"
      >
        {/* Layers icon */}
        <svg
          className="h-3 w-3 shrink-0 text-stone-400 dark:text-stone-500"
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
          strokeWidth={1.5}
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            d="M6.429 9.75L2.25 12l4.179 2.25m0-4.5l5.571 3 5.571-3m-11.142 0L2.25 7.5 12 2.25l9.75 5.25-4.179 2.25m0 0L12 12.75 6.429 9.75m11.142 0l4.179 2.25-9.75 5.25-9.75-5.25 4.179-2.25"
          />
        </svg>
        <span className="font-medium">{summarize(chunks)}</span>
        <svg
          className={`ml-auto h-3 w-3 shrink-0 text-stone-300 dark:text-stone-600 transition-transform duration-200 ${expanded ? 'rotate-180' : ''}`}
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
          strokeWidth={2}
        >
          <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 8.25l-7.5 7.5-7.5-7.5" />
        </svg>
      </button>

      {/* Expandable chunk list */}
      <div className={`rag-context-detail ${expanded ? 'expanded' : ''}`}>
        <div>
          <div className="border-t border-stone-200/40 dark:border-stone-700/30 px-2.5 py-1.5 space-y-2">
            {chunks.map((chunk) => (
              <ChunkRow key={chunk.chunk_id} chunk={chunk} />
            ))}
          </div>
        </div>
      </div>
    </div>
  )
}

function ChunkRow({ chunk }: { chunk: RagContextChunk }) {
  const filled = scoreDots(chunk.score)
  const pages = formatPages(chunk.page_start, chunk.page_end)
  const tooltip = pages ? `${chunk.doc_title}, ${pages.replace('\u00A0', ' ')}` : chunk.doc_title

  return (
    <div className="text-[11px] leading-relaxed">
      {/* Title row */}
      <div className="flex items-center gap-1.5 mb-0.5">
        <Link
          to={`/docs/${chunk.doc_id}`}
          className="font-medium text-stone-600 dark:text-stone-300 hover:text-(--color-accent) dark:hover:text-(--color-accent) transition-colors duration-150 truncate"
          title={tooltip}
        >
          {chunk.doc_title}
          <span className="inline-block ml-0.5 text-[9px] opacity-50">&#x2197;</span>
        </Link>
        {pages && (
          <span className="shrink-0 rounded-sm px-1 py-px bg-stone-200/60 dark:bg-stone-700/40 text-stone-400 dark:text-stone-500 text-[10px] tabular-nums">
            {pages}
          </span>
        )}
        {/* Score dots */}
        <span className="ml-auto flex gap-px shrink-0" title={`Relevance: ${(chunk.score * 100).toFixed(0)}%`}>
          {[1, 2, 3, 4].map((n) => (
            <span
              key={n}
              className={`inline-block h-1 w-1 rounded-full ${
                n <= filled ? 'bg-stone-400 dark:bg-stone-500' : 'bg-stone-200 dark:bg-stone-700'
              }`}
            />
          ))}
        </span>
      </div>
      {/* Text preview */}
      <p className="text-stone-400 dark:text-stone-500 line-clamp-2 leading-snug">
        &ldquo;{chunk.text.slice(0, 160).trim()}
        {chunk.text.length > 160 ? '...' : ''}&rdquo;
      </p>
    </div>
  )
}

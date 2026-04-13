import { useState } from 'react'

interface ToolResultDisplayProps {
  rawResult: string
  toolName: string
}

function SearchHitList({ data }: { data: Record<string, unknown> }) {
  const hits = (data.hits || data.results || []) as Record<string, unknown>[]
  if (!hits.length) return <p className="text-gray-400 italic">No results</p>
  return (
    <div className="space-y-1.5">
      {hits.map((hit, i) => (
        <div key={i} className="flex items-baseline gap-2">
          <span className="shrink-0 text-gray-400 tabular-nums w-4 text-right">{i + 1}.</span>
          <div className="min-w-0">
            <span className="font-medium text-gray-600 dark:text-gray-300">
              {(hit.doc_title as string) || 'Untitled'}
            </span>
            {hit.page != null && <span className="ml-1 text-gray-400">p.{String(hit.page)}</span>}
            {hit.score != null && <span className="ml-1.5 text-gray-400/70">({Number(hit.score).toFixed(2)})</span>}
            {!!hit.snippet && (
              <p className="text-gray-500 dark:text-gray-400 line-clamp-2 mt-0.5">
                {String(hit.snippet).slice(0, 150)}
              </p>
            )}
            {!!hit.text && !hit.snippet && (
              <p className="text-gray-500 dark:text-gray-400 line-clamp-2 mt-0.5">{String(hit.text).slice(0, 150)}</p>
            )}
          </div>
        </div>
      ))}
    </div>
  )
}

function PassageBlocks({ data }: { data: Record<string, unknown> }) {
  const passages = (data.passages || data.chunks || []) as Record<string, unknown>[]
  if (!passages.length) return <p className="text-gray-400 italic">No passages</p>
  return (
    <div className="space-y-2">
      {passages.map((p, i) => (
        <div key={i}>
          <div className="text-gray-500 dark:text-gray-400 text-[10px] uppercase tracking-wide mb-0.5">
            {(p.doc_title as string) || 'Untitled'}
            {p.page != null && <span> — p.{String(p.page)}</span>}
          </div>
          <p className="text-gray-600 dark:text-gray-300 whitespace-pre-wrap">{String(p.text || p.content || '')}</p>
        </div>
      ))}
    </div>
  )
}

function EntityList({ data }: { data: Record<string, unknown> }) {
  const entities = (data.entities || []) as Record<string, unknown>[]
  if (!entities.length) return <p className="text-gray-400 italic">No entities</p>
  return (
    <div className="flex flex-wrap gap-1.5">
      {entities.map((e, i) => (
        <span key={i} className="inline-flex items-center gap-1 rounded-full bg-gray-100 dark:bg-gray-700 px-2 py-0.5">
          <span className="font-medium text-gray-600 dark:text-gray-300">{String(e.name || e.text || '')}</span>
          {!!e.type && <span className="text-[10px] text-gray-400 uppercase">{String(e.type)}</span>}
        </span>
      ))}
    </div>
  )
}

function GenericView({ data }: { data: unknown }) {
  if (data === null || data === undefined) return null
  if (typeof data === 'string' || typeof data === 'number' || typeof data === 'boolean') {
    return <span className="text-gray-600 dark:text-gray-300">{String(data)}</span>
  }
  if (Array.isArray(data)) {
    if (data.length === 0) return <span className="text-gray-400 italic">empty list</span>
    return (
      <ul className="list-disc pl-4 space-y-0.5">
        {data.map((item, i) => (
          <li key={i}>
            <GenericView data={item} />
          </li>
        ))}
      </ul>
    )
  }
  if (typeof data === 'object') {
    const entries = Object.entries(data as Record<string, unknown>)
    if (entries.length === 0) return <span className="text-gray-400 italic">empty</span>
    return (
      <dl className="space-y-1">
        {entries.map(([key, val]) => (
          <div key={key}>
            <dt className="text-[10px] text-gray-400 uppercase tracking-wide">{key}</dt>
            <dd className="ml-2">
              <GenericView data={val} />
            </dd>
          </div>
        ))}
      </dl>
    )
  }
  return null
}

const SEARCH_TOOLS = new Set(['search_documents', 'kb_search', 'kb_batch_search'])
const PASSAGE_TOOLS = new Set([
  'read_passages',
  'kb_read_passages',
  'expand_context',
  'kb_expand_context',
  'read_document',
  'kb_read_document',
])
const ENTITY_TOOLS = new Set([
  'entity_search',
  'kb_entity_search',
  'entity_overview',
  'kb_entity_overview',
  'entity_cooccurrence',
  'kb_entity_cooccurrence',
])

function FormattedResult({ data, toolName }: { data: Record<string, unknown>; toolName: string }) {
  if (SEARCH_TOOLS.has(toolName)) return <SearchHitList data={data} />
  if (PASSAGE_TOOLS.has(toolName)) return <PassageBlocks data={data} />
  if (ENTITY_TOOLS.has(toolName)) return <EntityList data={data} />
  return <GenericView data={data} />
}

export default function ToolResultDisplay({ rawResult, toolName }: ToolResultDisplayProps) {
  const [showRaw, setShowRaw] = useState(false)

  let parsed: unknown = null
  let parseOk = false
  try {
    parsed = JSON.parse(rawResult)
    parseOk = true
  } catch {
    // not JSON — show as plain text
  }

  if (!parseOk) {
    return (
      <div className="text-[11px] text-gray-500 dark:text-gray-400 whitespace-pre-wrap max-h-64 overflow-auto">
        {rawResult}
      </div>
    )
  }

  const obj = parsed as Record<string, unknown>
  if (obj.error) {
    return <div className="text-[11px] text-red-500 dark:text-red-400">Error: {String(obj.error)}</div>
  }

  return (
    <div className="text-[11px]">
      {showRaw ? (
        <pre className="text-gray-400 dark:text-gray-500 whitespace-pre-wrap font-mono max-h-80 overflow-auto">
          {JSON.stringify(parsed, null, 2)}
        </pre>
      ) : (
        <FormattedResult data={obj} toolName={toolName} />
      )}
      <button
        onClick={() => setShowRaw(!showRaw)}
        className="mt-1.5 text-[10px] text-gray-400 hover:text-gray-500 dark:hover:text-gray-300 underline"
      >
        {showRaw ? 'Show formatted' : 'Show raw'}
      </button>
    </div>
  )
}

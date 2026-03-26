import React, { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { get } from '../api'

interface DocEntry {
  doc_id: string
  title: string
}

// Cache doc title → doc_id map across renders
let docMapCache: Record<string, string> | null = null
let docMapPromise: Promise<Record<string, string>> | null = null

function fetchDocMap(): Promise<Record<string, string>> {
  if (docMapCache) return Promise.resolve(docMapCache)
  if (docMapPromise) return docMapPromise
  docMapPromise = get<{ items: DocEntry[] }>('/api/docs', { limit: 5000 })
    .then((data) => {
      const map: Record<string, string> = {}
      for (const d of data.items) {
        map[d.title.toLowerCase()] = d.doc_id
      }
      docMapCache = map
      return map
    })
    .catch(() => ({}))
  return docMapPromise
}

// Match [Document Title, page X] or [Document Title, pages X-Y]
const CITATION_RE = /\[([^\]]+?),\s*pages?\s+(\d+(?:\s*[-–]\s*\d+)?)\]/g

function resolveCitations(text: string, docMap: Record<string, string>): (string | React.ReactElement)[] {
  const parts: (string | React.ReactElement)[] = []
  let lastIndex = 0
  let match: RegExpExecArray | null

  const re = new RegExp(CITATION_RE.source, 'g')
  while ((match = re.exec(text)) !== null) {
    if (match.index > lastIndex) {
      parts.push(text.slice(lastIndex, match.index))
    }

    const title = match[1].trim()
    const pageStr = match[2].trim()
    const page = parseInt(pageStr, 10)
    const docId = docMap[title.toLowerCase()]

    if (docId && !isNaN(page)) {
      parts.push(
        <Link
          key={`${docId}-${match.index}`}
          to={`/docs/${docId}?page=${page}`}
          className="text-blue-600 dark:text-blue-400 hover:underline"
          title={`${title}, page ${pageStr}`}
        >
          [{title}, page {pageStr}]
        </Link>,
      )
    } else {
      // No match — keep as plain text
      parts.push(match[0])
    }

    lastIndex = re.lastIndex
  }

  if (lastIndex < text.length) {
    parts.push(text.slice(lastIndex))
  }

  return parts
}

/**
 * Renders markdown with [Document Title, page N] citations as clickable links.
 * Falls back to plain ReactMarkdown if doc map isn't loaded yet.
 */
export default function CitedMarkdown({ children }: { children: string }) {
  const [docMap, setDocMap] = useState<Record<string, string>>(docMapCache || {})

  useEffect(() => {
    fetchDocMap().then(setDocMap)
  }, [])

  const hasMap = Object.keys(docMap).length > 0

  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      components={
        hasMap
          ? {
              // Override text nodes to resolve citations
              p: ({ children: pChildren }) => {
                const resolved = resolveChildren(pChildren, docMap)
                return <p>{resolved}</p>
              },
              li: ({ children: liChildren }) => {
                const resolved = resolveChildren(liChildren, docMap)
                return <li>{resolved}</li>
              },
            }
          : undefined
      }
    >
      {children}
    </ReactMarkdown>
  )
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function resolveChildren(children: any, docMap: Record<string, string>): any {
  if (!children) return children
  if (typeof children === 'string') {
    const parts = resolveCitations(children, docMap)
    return parts.length === 1 && typeof parts[0] === 'string' ? parts[0] : <>{parts}</>
  }
  if (Array.isArray(children)) {
    return children.map((child, i) => {
      if (typeof child === 'string') {
        const parts = resolveCitations(child, docMap)
        return parts.length === 1 && typeof parts[0] === 'string' ? child : <span key={i}>{parts}</span>
      }
      return child
    })
  }
  return children
}

// frontend/src/components/DocumentErrorNotice.tsx
//
// A failed document's error in plain language (#555). The server classifies the
// stored error into a family and supplies a fixed message and suggested action
// (src/harbor_clerk/document_failure.py); the raw error stays one click away,
// uncropped and copyable. Temporary failures render amber and unreadable files
// red, so a service blip no longer looks like a broken file; anything else is neutral.

import { useState } from 'react'
import { FAMILY_STYLES, UNCLASSIFIED, UNCLASSIFIED_COMPACT, type ErrorInfo } from '../utils/documentFailure'

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false)
  async function handleCopy() {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch {
      // Best-effort: the text is selectable in the <pre> either way.
    }
  }
  return (
    <button
      type="button"
      onClick={handleCopy}
      className="shrink-0 rounded-md border border-current/20 px-2 py-0.5 text-[11px] font-medium opacity-80 hover:opacity-100"
    >
      {copied ? 'Copied' : 'Copy'}
    </button>
  )
}

interface DocumentErrorNoticeProps {
  info?: ErrorInfo | null
  /** The raw stored error (path-redacted by the server for API-key callers). */
  raw?: string | null
  /** Pipeline stage that failed, when known. */
  stage?: string | null
  /** One wrapped line for a list row; the full box otherwise. */
  compact?: boolean
  /** Compact only: add the raw error behind a "Technical details" toggle under the line. */
  showRaw?: boolean
}

function RawErrorDetails({ raw, className }: { raw: string; className: string }) {
  return (
    <details className={className}>
      <summary className="cursor-pointer text-xs font-medium opacity-80 hover:opacity-100">Technical details</summary>
      <div className="mt-1.5 flex items-start gap-2">
        <pre className="min-w-0 flex-1 whitespace-pre-wrap wrap-break-word rounded-md bg-black/5 p-2 font-mono text-[11px] leading-snug dark:bg-white/5">
          {raw}
        </pre>
        <CopyButton text={raw} />
      </div>
    </details>
  )
}

export default function DocumentErrorNotice({
  info,
  raw,
  stage,
  compact = false,
  showRaw = false,
}: DocumentErrorNoticeProps) {
  if (!info && !raw) return null
  const rawBelow = !compact || showRaw
  const explained = info ?? (rawBelow ? UNCLASSIFIED : UNCLASSIFIED_COMPACT)
  const style = FAMILY_STYLES[explained.family]

  if (compact) {
    const line = (
      <p className={`mt-0.5 text-xs leading-snug ${style.accent}`} data-family={explained.family}>
        {explained.message} {explained.action}
      </p>
    )
    if (!showRaw || !raw) return line
    return (
      <div>
        {line}
        <RawErrorDetails raw={raw} className="mt-1 text-(--color-text-secondary)" />
      </div>
    )
  }

  return (
    <div
      className={`mb-3 rounded-lg border px-3 py-2 text-sm ${style.box}`}
      role="status"
      data-family={explained.family}
    >
      <p className="font-semibold">
        {style.title}
        {stage ? <span className="ml-1 font-normal opacity-75">(during {stage})</span> : null}
      </p>
      <p className="mt-0.5 leading-relaxed">{explained.message}</p>
      <p className={`mt-0.5 leading-relaxed ${style.accent}`}>{explained.action}</p>
      {raw && <RawErrorDetails raw={raw} className="mt-2" />}
    </div>
  )
}

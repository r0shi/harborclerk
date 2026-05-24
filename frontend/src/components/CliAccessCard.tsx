import { useState } from 'react'
import { Card } from './Card'

const SKILL_MARKDOWN = `---
name: harbor-clerk
description: Search and read documents from a local Harbor Clerk knowledge base. Use when the user references their personal documents, contracts, notes, emails, or asks "what did I store about X?"
---

# Harbor Clerk — extended memory for agents

Harbor Clerk is a local document corpus with hybrid FTS+vector search and citation-preserving reads. This skill exposes it via the \`harbor-clerk\` CLI.

## First step: discover the surface
Run \`harbor-clerk --help\` for the full command list, then \`harbor-clerk <cmd> --help\` for any specific command. The help is comprehensive — JSON return shapes, examples, common mistakes are all there.

## Three patterns you'll use most

1. **Search → expand**: \`harbor-clerk search "..."\` then \`harbor-clerk expand-context <chunk_id> -n 3\` on the best hit.
2. **Read a known document**: \`harbor-clerk read-document <doc_id>\` for full text with pagination.
3. **Check ingest status before searching for new content**: \`harbor-clerk ingest-status <doc_id>\` returns per-stage state.

## What you can trust
- Every search result includes a \`citation\` field — quote it back to the user.
- \`possible_conflict: true\` means top hits disagree across documents; surface both sources.
- The CLI exits non-zero on failure. Exit code 3 specifically means an admin has disabled CLI access — tell the user.
`

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false)

  async function handleCopy() {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch {
      // best-effort
    }
  }

  return (
    <button
      onClick={handleCopy}
      className="ml-2 shrink-0 rounded-md bg-(--color-bg-secondary) px-2 py-1 text-xs font-medium text-(--color-text-secondary) hover:bg-black/6 dark:hover:bg-white/10 transition-colors"
    >
      {copied ? 'Copied!' : 'Copy'}
    </button>
  )
}

export function CliAccessCard({ enabled, envVarHint }: { enabled: boolean; envVarHint?: string }) {
  return (
    <Card className="p-6">
      <h2 className="text-lg font-semibold mb-2">Agentic CLI</h2>
      <p className="mb-4 text-sm text-(--color-text-secondary)">
        Expose Harbor Clerk to OpenClaw, Claude Code, Codex, and other CLI-orchestrating agent harnesses via a{' '}
        <code className="rounded bg-(--color-bg-secondary) px-1 py-0.5 text-xs">harbor-clerk</code> command.
      </p>

      <div className="flex items-center gap-2 mb-4">
        <span className={`inline-block h-2.5 w-2.5 rounded-full ${enabled ? 'bg-green-500' : 'bg-gray-400'}`} />
        <span className="text-sm text-(--color-text-primary)">
          CLI access is {enabled ? <strong>enabled</strong> : <strong>disabled</strong>}
        </span>
      </div>

      {!enabled && envVarHint && (
        <div className="mb-4 rounded-lg bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800 px-4 py-3">
          <p className="text-sm text-amber-800 dark:text-amber-300">
            To enable, set{' '}
            <code className="rounded bg-amber-100 dark:bg-amber-900/40 px-1 py-0.5 text-xs">{envVarHint}</code> and
            restart the API service.
          </p>
        </div>
      )}

      <h3 className="mt-2 mb-2 text-sm font-semibold text-(--color-text-primary)">
        Skill markdown <span className="font-normal text-(--color-text-secondary)">(copy into your harness)</span>
      </h3>
      <div className="relative mt-1 rounded-lg bg-(--color-bg-secondary) dark:bg-(--color-bg-tertiary) p-3 font-mono text-xs text-(--color-text-primary) overflow-x-auto">
        <div className="absolute top-2 right-2">
          <CopyButton text={SKILL_MARKDOWN} />
        </div>
        <pre className="whitespace-pre-wrap pr-16">{SKILL_MARKDOWN}</pre>
      </div>
    </Card>
  )
}

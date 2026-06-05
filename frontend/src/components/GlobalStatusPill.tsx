import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { get } from '../api'
import { useAuth } from '../auth'
import { useSystemConfig } from '../hooks/useSystemConfig'
import { useLLMStatusContext } from './LLMStatusBanner'
import { StatusPill, type PillState } from './StatusPill'

interface StatusSummary {
  state: 'ready' | 'processing' | 'needs_attention'
  counts: {
    processing_documents: number
    completed_status_stale_documents?: number
    stranded_documents?: number
    queued_jobs: number
    running_jobs: number
    failed_documents: number
    unavailable_folders: number
    ner_skipped_documents: number
    stuck_jobs: number
  }
  needs_attention: Array<{
    kind: string
    severity: 'error' | 'warning'
    title: string
    detail: string
    count?: number
  }>
}

interface PillView {
  label: string
  glyph?: string
  state: PillState
  title: string
  to: string
}

function plural(count: number, singular: string, pluralLabel = `${singular}s`): string {
  return count === 1 ? `1 ${singular}` : `${count.toLocaleString()} ${pluralLabel}`
}

function attentionView(summary: StatusSummary): PillView {
  const errorIssue = summary.needs_attention.find((issue) => issue.severity === 'error')
  const firstIssue = errorIssue ?? summary.needs_attention[0]
  const errorCount = summary.needs_attention.filter((issue) => issue.severity === 'error').length
  const state: PillState = errorIssue ? 'error' : 'pending'

  if (summary.needs_attention.length > 1) {
    return {
      label: errorCount > 0 ? plural(errorCount, 'issue') : plural(summary.needs_attention.length, 'notice'),
      glyph: errorIssue ? '⚠' : '◐',
      state,
      title: 'Status needs attention',
      to: '/settings/status',
    }
  }

  if (firstIssue?.kind === 'failed_documents') {
    return {
      label: plural(summary.counts.failed_documents || firstIssue.count || 0, 'failed doc'),
      glyph: '⚠',
      state,
      title: firstIssue.detail,
      to: '/settings/status',
    }
  }

  if (firstIssue?.kind === 'folder_access') {
    return {
      label: summary.counts.unavailable_folders === 1 ? 'Folder issue' : 'Folder issues',
      glyph: '⚠',
      state,
      title: firstIssue.detail,
      to: '/settings/status',
    }
  }

  if (firstIssue?.kind === 'stuck_jobs') {
    return {
      label: summary.counts.stuck_jobs === 1 ? 'Stuck job' : 'Stuck jobs',
      glyph: '⚠',
      state,
      title: firstIssue.detail,
      to: '/settings/status',
    }
  }

  if (firstIssue?.kind === 'entity_extraction_skipped') {
    return {
      label: 'Entities skipped',
      glyph: '◐',
      state,
      title: firstIssue.detail,
      to: '/settings/status',
    }
  }

  if (firstIssue?.kind === 'stranded_pipeline_state') {
    return {
      label: 'Stale state',
      glyph: '◐',
      state,
      title: firstIssue.detail,
      to: '/settings/status',
    }
  }

  if (firstIssue?.kind === 'completed_status_stale') {
    return {
      label: 'Status cleanup',
      glyph: '◐',
      state,
      title: firstIssue.detail,
      to: '/settings/status',
    }
  }

  return {
    label: 'Needs attention',
    glyph: errorIssue ? '⚠' : '◐',
    state,
    title: firstIssue?.title ?? 'Status needs attention',
    to: '/settings/status',
  }
}

function statusView({
  healthStatus,
  loaded,
  llmState,
  summary,
  summaryLoaded,
  isAdmin,
}: {
  healthStatus: 'healthy' | 'degraded' | null
  loaded: boolean
  llmState: string
  summary: StatusSummary | null
  summaryLoaded: boolean
  isAdmin: boolean
}): PillView {
  const summaryHasError = summary?.needs_attention.some((issue) => issue.severity === 'error') ?? false

  if ((isAdmin && !summaryLoaded) || (!loaded && !summary)) {
    return {
      label: 'Checking',
      state: 'pending',
      title: 'Checking system status',
      to: '/settings/status',
    }
  }

  if (summary?.state === 'needs_attention' && summaryHasError) {
    return attentionView(summary)
  }

  if (healthStatus === 'degraded') {
    return {
      label: 'Needs attention',
      glyph: '⚠',
      state: 'error',
      title: 'System checks need attention',
      to: '/settings/status',
    }
  }

  if (summary?.state === 'needs_attention') {
    return attentionView(summary)
  }

  if (summary?.state === 'processing') {
    const count = summary.counts.processing_documents || summary.counts.running_jobs || summary.counts.queued_jobs || 0
    return {
      label: count ? `${count.toLocaleString()} processing` : 'Processing',
      state: 'running',
      title: 'Documents are being processed',
      to: '/settings/status',
    }
  }

  if (llmState === 'loading') {
    return {
      label: 'AI loading',
      state: 'running',
      title: 'Local AI model is loading',
      to: '/settings/models',
    }
  }

  if (llmState === 'deactivated') {
    return {
      label: 'Choose model',
      state: 'idle',
      title: 'No local AI model is active',
      to: '/settings/models',
    }
  }

  if (llmState === 'unknown') {
    return {
      label: 'AI unknown',
      state: 'pending',
      title: 'Local AI status is unknown',
      to: '/settings/models',
    }
  }

  return {
    label: 'Ready',
    glyph: '●',
    state: 'active',
    title: 'System ready',
    to: '/settings/status',
  }
}

export default function GlobalStatusPill() {
  const { isAdmin } = useAuth()
  const { status } = useLLMStatusContext()
  const systemConfig = useSystemConfig()
  const [summary, setSummary] = useState<StatusSummary | null>(null)
  const [summaryLoaded, setSummaryLoaded] = useState(false)

  useEffect(() => {
    if (!isAdmin) return

    let cancelled = false

    async function loadSummary() {
      try {
        const data = await get<StatusSummary>('/api/system/status-summary')
        if (!cancelled) setSummary(data)
      } catch {
        if (!cancelled) setSummary(null)
      } finally {
        if (!cancelled) setSummaryLoaded(true)
      }
    }

    void loadSummary()
    const interval = window.setInterval(() => {
      void loadSummary()
    }, 15000)

    return () => {
      cancelled = true
      window.clearInterval(interval)
    }
  }, [isAdmin])

  const view = statusView({
    healthStatus: systemConfig.healthStatus,
    loaded: systemConfig.loaded,
    llmState: status.state,
    summary,
    summaryLoaded,
    isAdmin,
  })

  return (
    <Link
      to={view.to}
      aria-label={`Status: ${view.label}`}
      title={view.title}
      className="inline-flex rounded-full transition-opacity hover:opacity-80 focus:outline-hidden focus:ring-2 focus:ring-(--color-accent)/30"
    >
      <StatusPill state={view.state} label={view.label} glyph={view.glyph} />
    </Link>
  )
}

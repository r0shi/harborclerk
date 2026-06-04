import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { get, post } from '../api'
import { listMailAccounts } from '../api/mail'
import type { MailAccountResponse } from '../types/mail'
import { PageHeader } from '../components/PageHeader'
import { Card } from '../components/Card'
import { StatusPill, type PillState } from '../components/StatusPill'
import { useLLMStatusContext } from '../components/LLMStatusBanner'

interface HealthCheck {
  status: string
  checks: {
    postgres: string
    storage: string
    tika: string
    reranker?: string
  }
}

interface ServiceStats {
  [key: string]: number | string | null | undefined
}

interface StatsResponse {
  postgres?: ServiceStats
  storage?: ServiceStats
}

interface StatusIssue {
  kind: string
  severity: 'error' | 'warning'
  title: string
  detail: string
  count?: number
  action_label?: string
  action_href?: string
  action_kind?: 'reaper'
}

interface StatusDoc {
  doc_id: string
  title: string
  canonical_filename?: string | null
  pipeline_status: string
  error?: string | null
  failed_stage?: string | null
  updated_at?: string | null
}

interface StatusSummary {
  state: 'ready' | 'processing' | 'needs_attention'
  counts: {
    active_documents: number
    ready_documents: number
    processing_documents: number
    failed_documents: number
    queued_jobs: number
    running_jobs: number
    failed_jobs: number
    watched_folders: number
    unavailable_folders: number
    ner_skipped_documents: number
    stuck_jobs: number
  }
  needs_attention: StatusIssue[]
  recent_failed_documents: StatusDoc[]
  recent_processing_documents: StatusDoc[]
}

const STAT_LABELS: Record<string, string> = {
  db_size_mb: 'DB Size',
  active_connections: 'Connections',
  cache_hit_ratio: 'Cache Hit Ratio',
  total_chunks: 'Total Chunks',
  dead_tuples: 'Dead Tuples',
  io_queue_depth: 'IO Queue',
  cpu_queue_depth: 'CPU Queue',
  object_count: 'Objects',
  total_size_mb: 'Total Size',
}

function formatStatValue(key: string, value: number | string | null | undefined): string {
  if (value == null) return '-'
  if (typeof value === 'string') return value
  if (key.endsWith('_mb')) return `${value} MB`
  if (key === 'cache_hit_ratio') return `${(value * 100).toFixed(1)}%`
  return value.toLocaleString()
}

function stateLabel(state?: StatusSummary['state']): string {
  if (state === 'needs_attention') return 'Needs attention'
  if (state === 'processing') return 'Processing'
  return 'Ready'
}

function statePill(state?: StatusSummary['state']): PillState {
  if (state === 'needs_attention') return 'error'
  if (state === 'processing') return 'running'
  return 'active'
}

function serviceIsOk(status?: string): boolean {
  return status === 'ok' || status === 'disabled'
}

export default function SystemStatusPage() {
  const { status: llmStatus } = useLLMStatusContext()
  const [health, setHealth] = useState<HealthCheck | null>(null)
  const [stats, setStats] = useState<StatsResponse | null>(null)
  const [summary, setSummary] = useState<StatusSummary | null>(null)
  const [loading, setLoading] = useState(true)
  const [statsLoading, setStatsLoading] = useState(true)
  const [error, setError] = useState('')
  const [actionResult, setActionResult] = useState('')
  const [reaperRunning, setReaperRunning] = useState(false)
  const [refreshKey, setRefreshKey] = useState(0)

  async function loadHealth() {
    try {
      const data = await get<HealthCheck>('/api/system/health')
      setHealth(data)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load health')
    }
  }

  async function loadSummary() {
    try {
      const data = await get<StatusSummary>('/api/system/status-summary')
      setSummary(data)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load status')
    }
  }

  async function loadStats() {
    setStatsLoading(true)
    try {
      const data = await get<StatsResponse>('/api/system/stats')
      setStats(data)
    } catch {
      // Stats are diagnostic-only; status recovery should still render.
    } finally {
      setStatsLoading(false)
    }
  }

  async function reloadStatusData() {
    await Promise.all([loadHealth(), loadSummary(), loadStats()])
    setLoading(false)
  }

  useEffect(() => {
    let cancelled = false
    Promise.allSettled([
      get<HealthCheck>('/api/system/health'),
      get<StatusSummary>('/api/system/status-summary'),
      get<StatsResponse>('/api/system/stats'),
    ]).then(([healthResult, summaryResult, statsResult]) => {
      if (cancelled) return

      if (healthResult.status === 'fulfilled') {
        setHealth(healthResult.value)
      } else {
        setError(healthResult.reason instanceof Error ? healthResult.reason.message : 'Failed to load health')
      }

      if (summaryResult.status === 'fulfilled') {
        setSummary(summaryResult.value)
      } else {
        setError(summaryResult.reason instanceof Error ? summaryResult.reason.message : 'Failed to load status')
      }

      if (statsResult.status === 'fulfilled') {
        setStats(statsResult.value)
      }
      setStatsLoading(false)
      setLoading(false)
    })

    return () => {
      cancelled = true
    }
  }, [])

  function handleRefresh() {
    setError('')
    setActionResult('')
    setLoading(true)
    void reloadStatusData()
    setRefreshKey((k) => k + 1)
  }

  async function handleReaperRun() {
    setError('')
    setActionResult('')
    setReaperRunning(true)
    try {
      const data = await post<{ reaped: number }>('/api/system/reaper-run')
      setActionResult(`Recovered ${data.reaped} stale job${data.reaped === 1 ? '' : 's'}.`)
      await loadSummary()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to recover stale jobs')
    } finally {
      setReaperRunning(false)
    }
  }

  const serviceIssues = useMemo<StatusIssue[]>(() => {
    if (!health) return []
    const failed = Object.entries(health.checks).filter(([, status]) => !serviceIsOk(status))
    if (failed.length === 0) return []
    return [
      {
        kind: 'service_health',
        severity: 'error',
        title: 'Search services need attention',
        detail: failed.map(([name, status]) => `${name}: ${status}`).join('; '),
        count: failed.length,
        action_label: 'Open diagnostics',
        action_href: '/settings/diagnostics',
      },
    ]
  }, [health])

  const attentionItems = [...serviceIssues, ...(summary?.needs_attention ?? [])]
  const displayState: StatusSummary['state'] =
    attentionItems.length > 0 ? 'needs_attention' : summary?.state === 'processing' ? 'processing' : 'ready'

  if (loading && !health && !summary) {
    return <div className="text-gray-500 dark:text-gray-400">Loading status...</div>
  }

  return (
    <div className="animate-slide-in">
      <PageHeader
        title="Status"
        subtitle="Readiness, needs-attention items, and recovery actions"
        actions={
          <button
            onClick={handleRefresh}
            className="rounded-lg bg-(--color-bg-tertiary) px-4 py-2 text-sm font-medium text-gray-700 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-600"
          >
            Refresh
          </button>
        }
      />

      {error && (
        <div className="mb-4 rounded-sm bg-red-50 dark:bg-red-900/20 px-3 py-2 text-sm text-red-700 dark:text-red-400">
          {error}
        </div>
      )}

      {actionResult && (
        <div className="mb-4 rounded-sm bg-green-50 dark:bg-green-900/20 px-3 py-2 text-sm text-green-700 dark:text-green-400">
          {actionResult}
        </div>
      )}

      <section className="mb-6 grid gap-4 lg:grid-cols-[1.1fr_1fr]">
        <Card className="p-5">
          <div className="mb-4 flex items-start justify-between gap-3">
            <div>
              <h2 className="text-lg font-semibold text-(--color-text-primary)">System state</h2>
              <p className="mt-1 text-sm text-(--color-text-secondary)">
                The short version of whether Harbor Clerk is ready to search, ingest, and answer.
              </p>
            </div>
            <StatusPill state={statePill(displayState)} label={stateLabel(displayState)} />
          </div>
          <ReadinessChecklist summary={summary} health={health} llmState={llmStatus.state} />
        </Card>

        <Card className="p-5">
          <h2 className="text-lg font-semibold text-(--color-text-primary)">At a glance</h2>
          <div className="mt-4 grid grid-cols-2 gap-3">
            <MetricTile label="Documents" value={summary?.counts.active_documents ?? 0} />
            <MetricTile label="Ready" value={summary?.counts.ready_documents ?? 0} />
            <MetricTile label="Processing" value={summary?.counts.processing_documents ?? 0} />
            <MetricTile label="Failed" value={summary?.counts.failed_documents ?? 0} tone="error" />
            <MetricTile label="Queued jobs" value={summary?.counts.queued_jobs ?? 0} />
            <MetricTile label="Running jobs" value={summary?.counts.running_jobs ?? 0} />
          </div>
        </Card>
      </section>

      <section className="mb-6">
        <div className="mb-3 flex items-center justify-between gap-3">
          <h2 className="text-lg font-semibold text-(--color-text-primary)">Needs attention</h2>
          {attentionItems.length > 0 && (
            <Link to="/settings/maintenance" className="text-sm text-(--color-accent) hover:underline">
              Advanced maintenance
            </Link>
          )}
        </div>

        {attentionItems.length === 0 ? (
          <Card className="p-4">
            <div className="flex items-center gap-3">
              <StatusPill state="active" label="No action needed" />
              <p className="text-sm text-(--color-text-secondary)">
                No failed documents, stuck jobs, unavailable folders, or degraded service checks are currently reported.
              </p>
            </div>
          </Card>
        ) : (
          <div className="grid gap-3">
            {attentionItems.map((issue) => (
              <IssueCard key={issue.kind} issue={issue} reaperRunning={reaperRunning} onRunReaper={handleReaperRun} />
            ))}
          </div>
        )}
      </section>

      <RecentActivity summary={summary} />

      <section className="mb-6">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-lg font-semibold text-(--color-text-primary)">Diagnostics</h2>
          <div className="flex items-center gap-3 text-sm">
            <Link to="/settings/diagnostics" className="text-(--color-accent) hover:underline">
              Logs
            </Link>
            <Link to="/settings/maintenance" className="text-(--color-accent) hover:underline">
              Maintenance
            </Link>
          </div>
        </div>

        {health && (
          <div className="mb-6 grid gap-4 md:grid-cols-2 xl:grid-cols-4">
            <HealthCard
              name="PostgreSQL"
              status={health.checks.postgres}
              stats={stats?.postgres}
              statsLoading={statsLoading}
            />
            <HealthCard
              name="Storage"
              status={health.checks.storage}
              stats={stats?.storage}
              statsLoading={statsLoading}
            />
            <HealthCard name="Tika" status={health.checks.tika} statsLoading={false} />
            <HealthCard name="Reranker" status={health.checks.reranker ?? 'not configured'} statsLoading={false} />
          </div>
        )}
      </section>

      <MailAccountsStatus refreshKey={refreshKey} />
    </div>
  )
}

function ReadinessChecklist({
  summary,
  health,
  llmState,
}: {
  summary: StatusSummary | null
  health: HealthCheck | null
  llmState: string
}) {
  const healthOk = health ? Object.values(health.checks).every(serviceIsOk) : false
  const foldersReady = (summary?.counts.watched_folders ?? 0) > 0 && (summary?.counts.unavailable_folders ?? 0) === 0
  const docsOk = (summary?.counts.failed_documents ?? 0) === 0
  const processing = (summary?.counts.processing_documents ?? 0) > 0 || (summary?.counts.running_jobs ?? 0) > 0
  const localAiLabel =
    llmState === 'ready'
      ? 'ready'
      : llmState === 'loading'
        ? 'loading'
        : llmState === 'deactivated'
          ? 'not configured'
          : 'unknown'
  const localAiState: PillState =
    llmState === 'ready'
      ? 'active'
      : llmState === 'loading'
        ? 'running'
        : llmState === 'deactivated'
          ? 'idle'
          : 'pending'

  return (
    <div className="space-y-3">
      <CheckRow
        label="Search services"
        state={healthOk ? 'active' : 'error'}
        detail={healthOk ? 'Ready' : 'Open diagnostics'}
      />
      <CheckRow
        label="Watched folders"
        state={foldersReady ? 'active' : summary?.counts.unavailable_folders ? 'error' : 'idle'}
        detail={
          foldersReady
            ? `${summary?.counts.watched_folders ?? 0} folder${summary?.counts.watched_folders === 1 ? '' : 's'} watching`
            : summary?.counts.unavailable_folders
              ? 'Folder access issue'
              : 'No folders configured'
        }
      />
      <CheckRow
        label="Documents"
        state={docsOk ? 'active' : 'error'}
        detail={docsOk ? 'No ingest failures' : 'Failures need review'}
      />
      <CheckRow
        label="Ingestion"
        state={processing ? 'running' : 'active'}
        detail={processing ? 'Documents are processing' : 'No active processing'}
      />
      <CheckRow label="Local AI" state={localAiState} detail={localAiLabel} />
    </div>
  )
}

function CheckRow({ label, state, detail }: { label: string; state: PillState; detail: string }) {
  return (
    <div className="flex items-center justify-between gap-3 border-b border-(--color-border) pb-2 last:border-0 last:pb-0">
      <span className="text-sm font-medium text-(--color-text-primary)">{label}</span>
      <div className="flex items-center gap-2">
        <span className="text-xs text-(--color-text-secondary)">{detail}</span>
        <StatusPill state={state} label={state === 'active' ? 'OK' : state} />
      </div>
    </div>
  )
}

function MetricTile({ label, value, tone = 'normal' }: { label: string; value: number; tone?: 'normal' | 'error' }) {
  return (
    <div className="rounded-lg bg-(--color-bg-secondary) px-3 py-3">
      <div
        className={`text-2xl font-semibold ${tone === 'error' && value > 0 ? 'text-red-600 dark:text-red-400' : ''}`}
      >
        {value.toLocaleString()}
      </div>
      <div className="mt-1 text-xs text-(--color-text-secondary)">{label}</div>
    </div>
  )
}

function IssueCard({
  issue,
  reaperRunning,
  onRunReaper,
}: {
  issue: StatusIssue
  reaperRunning: boolean
  onRunReaper: () => void
}) {
  const pillState: PillState = issue.severity === 'error' ? 'error' : 'pending'
  return (
    <Card className="p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="mb-1 flex items-center gap-2">
            <StatusPill state={pillState} label={issue.severity === 'error' ? 'Action needed' : 'Review'} />
            <h3 className="font-medium text-(--color-text-primary)">{issue.title}</h3>
          </div>
          <p className="text-sm text-(--color-text-secondary)">{issue.detail}</p>
        </div>
        {issue.action_kind === 'reaper' ? (
          <button
            onClick={onRunReaper}
            disabled={reaperRunning}
            className="rounded-lg bg-(--color-accent) px-3 py-1.5 text-sm font-medium text-white disabled:opacity-60"
          >
            {reaperRunning ? 'Recovering...' : issue.action_label || 'Recover'}
          </button>
        ) : issue.action_href && issue.action_label ? (
          <Link
            to={issue.action_href}
            className="rounded-lg bg-(--color-bg-tertiary) px-3 py-1.5 text-sm font-medium text-(--color-text-primary) hover:bg-gray-200 dark:hover:bg-gray-600"
          >
            {issue.action_label}
          </Link>
        ) : null}
      </div>
    </Card>
  )
}

function RecentActivity({ summary }: { summary: StatusSummary | null }) {
  const failed = summary?.recent_failed_documents ?? []
  const processing = summary?.recent_processing_documents ?? []

  if (failed.length === 0 && processing.length === 0) return null

  return (
    <section className="mb-6 grid gap-4 lg:grid-cols-2">
      {failed.length > 0 && (
        <div>
          <h2 className="mb-3 text-lg font-semibold text-(--color-text-primary)">Failed documents</h2>
          <div className="space-y-2">
            {failed.map((doc) => (
              <DocumentStatusRow key={doc.doc_id} doc={doc} tone="error" />
            ))}
          </div>
        </div>
      )}
      {processing.length > 0 && (
        <div>
          <h2 className="mb-3 text-lg font-semibold text-(--color-text-primary)">Processing now</h2>
          <div className="space-y-2">
            {processing.map((doc) => (
              <DocumentStatusRow key={doc.doc_id} doc={doc} tone="running" />
            ))}
          </div>
        </div>
      )}
    </section>
  )
}

function DocumentStatusRow({ doc, tone }: { doc: StatusDoc; tone: 'error' | 'running' }) {
  return (
    <Card interactive className="p-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <Link to={`/docs/${doc.doc_id}`} className="font-medium text-blue-600 dark:text-blue-400 hover:underline">
            {doc.title}
          </Link>
          <div className="mt-0.5 truncate text-xs text-(--color-text-secondary)">
            {doc.failed_stage ? `${doc.failed_stage}: ` : ''}
            {doc.error || doc.canonical_filename || doc.pipeline_status}
          </div>
        </div>
        <StatusPill state={tone === 'error' ? 'error' : 'running'} label={doc.pipeline_status} />
      </div>
    </Card>
  )
}

function HealthCard({
  name,
  status,
  stats,
  statsLoading,
}: {
  name: string
  status: string
  stats?: ServiceStats
  statsLoading: boolean
}) {
  const ok = serviceIsOk(status)
  const statEntries = stats ? Object.entries(stats).filter(([k]) => k !== 'error') : []

  return (
    <Card className={`p-4 ${ok ? 'bg-green-50 dark:bg-green-900/20' : 'bg-red-50 dark:bg-red-900/20'}`}>
      <div className="mb-1 text-sm font-medium text-gray-700 dark:text-gray-300">{name}</div>
      <StatusPill state={ok ? 'active' : 'error'} label={ok ? 'OK' : status} />
      {statsLoading && !stats && <div className="mt-2 text-xs text-gray-400">Loading stats...</div>}
      {stats?.error && <div className="mt-2 text-xs text-red-500">{String(stats.error)}</div>}
      {statEntries.length > 0 && (
        <dl className="mt-3 space-y-1 border-t border-gray-200 dark:border-gray-700 pt-2">
          {statEntries.map(([key, value]) => (
            <div key={key} className="flex justify-between gap-3 text-xs">
              <dt className="text-gray-500 dark:text-gray-400">{STAT_LABELS[key] || key}</dt>
              <dd className="font-medium text-gray-700 dark:text-gray-300">{formatStatValue(key, value)}</dd>
            </div>
          ))}
        </dl>
      )}
    </Card>
  )
}

function statusBadgeClass(status: MailAccountResponse['status']): string {
  switch (status) {
    case 'active':
      return 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300'
    case 'paused':
      return 'bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-300'
    case 'auth_error':
    case 'key_mismatch':
      return 'bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300'
  }
}

function MailAccountsStatus({ refreshKey }: { refreshKey: number }) {
  const [accounts, setAccounts] = useState<MailAccountResponse[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const result = await listMailAccounts()
        if (!cancelled) setAccounts(result)
      } catch {
        if (!cancelled) setAccounts([])
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    void load()
    return () => {
      cancelled = true
    }
  }, [refreshKey])

  if (loading) {
    return (
      <section className="mb-6">
        <h2 className="text-base font-medium mb-2">Mail Accounts</h2>
        <p className="text-sm text-(--color-text-secondary)">Loading...</p>
      </section>
    )
  }

  if (accounts.length === 0) {
    return (
      <section className="mb-6">
        <h2 className="text-base font-medium mb-2">Mail Accounts</h2>
        <p className="text-sm text-(--color-text-secondary)">
          No mail accounts connected. Add one from{' '}
          <Link to="/folders" className="text-(--color-accent) hover:underline">
            Folders
          </Link>
          .
        </p>
      </section>
    )
  }

  return (
    <section className="mb-6">
      <h2 className="text-base font-medium mb-2">Mail Accounts</h2>
      <ul className="rounded-md border border-(--color-border) divide-y divide-(--color-border)">
        {accounts.map((a) => (
          <li key={a.account_id} className="px-3 py-2 flex items-center justify-between text-sm">
            <div>
              <div className="font-medium">{a.display_name}</div>
              <div className="text-xs text-(--color-text-secondary)">{a.imap_username}</div>
              {a.last_error && <div className="text-xs text-red-600 dark:text-red-400 mt-0.5">{a.last_error}</div>}
            </div>
            <div className="flex items-center gap-3">
              {a.last_connected_at && (
                <span className="text-xs text-(--color-text-secondary)">
                  last connected {new Date(a.last_connected_at).toLocaleString()}
                </span>
              )}
              <span className={`text-xs px-2 py-0.5 rounded-full ${statusBadgeClass(a.status)}`}>{a.status}</span>
            </div>
          </li>
        ))}
      </ul>
    </section>
  )
}

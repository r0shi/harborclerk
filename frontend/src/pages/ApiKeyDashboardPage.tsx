import { Fragment, useCallback, useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { del, get } from '../api'
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer } from 'recharts'
import { PageHeader } from '../components/PageHeader'
import { Card } from '../components/Card'

/* ---------- types ---------- */

interface ApiKeyInfo {
  key_id: string
  name: string
  is_active: boolean
  permission_tier: string
  scope_summary: string
}

type RangeMap<T> = Record<string, T>

interface UsageSummary {
  requests: RangeMap<number>
  errors: RangeMap<number>
  denials: RangeMap<number>
  rate_limited: RangeMap<number>
  last_used_at: string | null
  top_tools: RangeMap<Array<{ endpoint: string; count: number }>>
}

interface TimelineBucket {
  date: string
  ok: number
  error: number
  denied: number
  rate_limited: number
}

interface RequestEntry {
  request_id: string
  created_at: string
  request_type: string
  endpoint: string
  parameters: Record<string, unknown> | null
  status: string
  duration_ms: number
  result_summary: Record<string, unknown> | null
  status_detail: string | null
}

interface RequestPage {
  items: RequestEntry[]
  total: number
  page: number
  page_size: number
}

/* ---------- constants ---------- */

const RANGES = ['1h', '6h', '12h', '24h', '7d', '30d'] as const
const PAGE_SIZES = [25, 50, 100, 200] as const

/* ---------- helpers ---------- */

function formatTimestamp(ts: string): string {
  return new Date(ts).toLocaleString()
}

function truncate(s: string, max: number): string {
  return s.length > max ? s.slice(0, max) + '...' : s
}

function statusBadge(status: string) {
  const cls =
    status === 'ok'
      ? 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400'
      : status === 'error'
        ? 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400'
        : status === 'rate_limited'
          ? 'bg-orange-100 text-orange-700 dark:bg-orange-900/30 dark:text-orange-400'
          : 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400'
  return <span className={`rounded-md px-2 py-0.5 text-[11px] font-medium ${cls}`}>{status}</span>
}

/* ---------- component ---------- */

export default function ApiKeyDashboardPage() {
  const { keyId } = useParams<{ keyId: string }>()

  const [keyInfo, setKeyInfo] = useState<ApiKeyInfo | null>(null)
  const [usage, setUsage] = useState<UsageSummary | null>(null)
  const [timeline, setTimeline] = useState<TimelineBucket[]>([])
  const [requests, setRequests] = useState<RequestPage | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  // Single time-range selector drives every count card and the tool-breakdown
  // chart. The 30-day timeline and the request log have their own controls.
  const [range, setRange] = useState<string>('24h')

  // request log filters + pagination
  const [logPage, setLogPage] = useState(1)
  const [logPageSize, setLogPageSize] = useState(25)
  const [endpointFilter, setEndpointFilter] = useState('')
  const [statusFilter, setStatusFilter] = useState('')
  const [dateFrom, setDateFrom] = useState('')
  const [dateTo, setDateTo] = useState('')
  const [expandedRow, setExpandedRow] = useState<string | null>(null)

  // purge confirm
  const [purgeConfirm, setPurgeConfirm] = useState(false)

  const loadRequests = useCallback(async () => {
    if (!keyId) return
    try {
      const params: Record<string, string | number> = { page: logPage, page_size: logPageSize }
      if (endpointFilter) params.endpoint = endpointFilter
      if (statusFilter) params.status_filter = statusFilter
      if (dateFrom) params.date_from = dateFrom
      if (dateTo) params.date_to = dateTo
      const data = await get<RequestPage>(`/api/api-keys/${keyId}/usage/requests`, params)
      setRequests(data)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load request log')
    }
  }, [keyId, logPage, logPageSize, endpointFilter, statusFilter, dateFrom, dateTo])

  // initial data load
  useEffect(() => {
    if (!keyId) return
    let cancelled = false
    async function load() {
      setLoading(true)
      try {
        const [keys, usageData, timelineData] = await Promise.all([
          get<ApiKeyInfo[]>('/api/api-keys'),
          get<UsageSummary>(`/api/api-keys/${keyId}/usage`),
          get<TimelineBucket[]>(`/api/api-keys/${keyId}/usage/timeline`, { days: 30 }),
        ])
        if (cancelled) return
        const found = keys.find((k) => k.key_id === keyId) ?? null
        setKeyInfo(found)
        setUsage(usageData)
        setTimeline(timelineData)
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Failed to load dashboard')
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    load()
    return () => {
      cancelled = true
    }
  }, [keyId])

  // request log load
  useEffect(() => {
    async function run() {
      await loadRequests()
    }
    run()
  }, [loadRequests])

  async function handlePurge() {
    if (!keyId) return
    try {
      await del(`/api/api-keys/${keyId}/usage`)
      setPurgeConfirm(false)
      // reload everything
      const [usageData, timelineData] = await Promise.all([
        get<UsageSummary>(`/api/api-keys/${keyId}/usage`),
        get<TimelineBucket[]>(`/api/api-keys/${keyId}/usage/timeline`, { days: 30 }),
      ])
      setUsage(usageData)
      setTimeline(timelineData)
      setRequests(null)
      setLogPage(1)
      await loadRequests()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Purge failed')
    }
  }

  if (loading) return <div className="text-gray-500 dark:text-gray-400">Loading...</div>

  if (!keyInfo) {
    return (
      <div className="animate-slide-in">
        <Link to="/settings/api-keys" className="text-sm text-blue-600 dark:text-blue-400 hover:underline">
          &larr; API Keys
        </Link>
        <p className="mt-4 text-gray-500 dark:text-gray-400">API key not found.</p>
      </div>
    )
  }

  const hasData = usage && (usage.requests[range] > 0 || usage.requests['30d'] > 0 || timeline.length > 0)

  // Collect unique endpoints from top_tools across all ranges for filter dropdown
  const allEndpoints = usage
    ? [
        ...new Set(
          Object.values(usage.top_tools)
            .flat()
            .map((t) => t.endpoint),
        ),
      ].sort()
    : []

  const reqStart = requests ? (requests.page - 1) * requests.page_size + 1 : 0
  const reqEnd = requests ? Math.min(requests.page * requests.page_size, requests.total) : 0
  const totalPages = requests ? Math.ceil(requests.total / requests.page_size) : 0

  return (
    <div className="animate-slide-in space-y-6">
      {/* Header */}
      <div>
        <Link
          to="/settings/api-keys"
          className="mb-2 inline-block text-sm text-blue-600 dark:text-blue-400 hover:underline"
        >
          &larr; API Keys
        </Link>
        <PageHeader
          title={
            <span className="flex flex-wrap items-center gap-2">
              {keyInfo.name}
              <span
                className={`rounded-md px-2 py-0.5 text-[11px] font-medium ${
                  keyInfo.is_active
                    ? 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400'
                    : 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400'
                }`}
              >
                {keyInfo.is_active ? 'active' : 'revoked'}
              </span>
            </span>
          }
          subtitle={keyInfo.scope_summary}
          actions={
            <div>
              {!purgeConfirm ? (
                <button
                  onClick={() => setPurgeConfirm(true)}
                  className="rounded-lg bg-red-600 px-3 py-1.5 text-sm font-medium text-white shadow-xs hover:bg-red-700"
                >
                  Purge Events
                </button>
              ) : (
                <div className="flex items-center gap-2">
                  <span className="text-sm text-red-600 dark:text-red-400">Delete all logged events?</span>
                  <button
                    onClick={handlePurge}
                    className="rounded-lg bg-red-600 px-3 py-1.5 text-sm font-medium text-white shadow-xs hover:bg-red-700"
                  >
                    Confirm
                  </button>
                  <button
                    onClick={() => setPurgeConfirm(false)}
                    className="rounded-lg bg-gray-200 dark:bg-gray-700 px-3 py-1.5 text-sm font-medium text-gray-800 dark:text-gray-200 hover:bg-gray-300 dark:hover:bg-gray-600"
                  >
                    Cancel
                  </button>
                </div>
              )}
            </div>
          }
        />
      </div>

      {error && (
        <div className="rounded-sm bg-red-50 dark:bg-red-900/20 px-3 py-2 text-sm text-red-700 dark:text-red-400">
          {error}
        </div>
      )}

      {!hasData && !error && (
        <Card className="p-8 text-center text-gray-500 dark:text-gray-400">No events logged yet.</Card>
      )}

      {usage && (
        <>
          {/* Unified time range — drives every count card and the tool-breakdown chart */}
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h2 className="text-sm font-semibold text-(--color-text-primary)">Activity</h2>
            <label className="flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
              Time range
              <select
                value={range}
                onChange={(e) => setRange(e.target.value)}
                className="rounded border-0 bg-(--color-bg-secondary) px-2 py-1 text-xs"
              >
                {RANGES.map((r) => (
                  <option key={r} value={r}>
                    {r}
                  </option>
                ))}
              </select>
            </label>
          </div>

          {/* Summary cards */}
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-5">
            {/* Requests card */}
            <Card className="p-4">
              <span className="text-xs font-medium uppercase text-gray-500 dark:text-gray-400">Requests</span>
              <p className="mt-2 text-2xl font-bold">{usage.requests[range] ?? 0}</p>
            </Card>

            {/* Errors card */}
            <Card className={`p-4 ${(usage.errors[range] ?? 0) > 0 ? 'bg-red-50 dark:bg-red-900/10' : ''}`}>
              <span className="text-xs font-medium uppercase text-gray-500 dark:text-gray-400">Errors</span>
              <p className="mt-2 text-2xl font-bold text-red-600 dark:text-red-400">{usage.errors[range] ?? 0}</p>
            </Card>

            {/* Denials card */}
            <Card className={`p-4 ${(usage.denials[range] ?? 0) > 0 ? 'bg-amber-50 dark:bg-amber-900/10' : ''}`}>
              <span className="text-xs font-medium uppercase text-gray-500 dark:text-gray-400">Denials</span>
              <p className="mt-2 text-2xl font-bold text-amber-600 dark:text-amber-400">{usage.denials[range] ?? 0}</p>
            </Card>

            {/* Rate Limited card */}
            <Card className={`p-4 ${(usage.rate_limited[range] ?? 0) > 0 ? 'bg-orange-50 dark:bg-orange-900/20' : ''}`}>
              <span className="text-xs font-medium uppercase text-gray-500 dark:text-gray-400">Rate Limited</span>
              <p className="mt-2 text-2xl font-bold text-orange-600 dark:text-orange-400">
                {usage.rate_limited[range] ?? 0}
              </p>
            </Card>

            {/* Last Used card */}
            <Card className="p-4">
              <span className="text-xs font-medium uppercase text-gray-500 dark:text-gray-400">Last Used</span>
              <p className="mt-2 text-lg font-semibold">
                {usage.last_used_at ? formatTimestamp(usage.last_used_at) : 'Never'}
              </p>
            </Card>
          </div>

          {/* Charts */}
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
            {/* Requests over time */}
            <Card className="p-4">
              <h2 className="mb-3 text-sm font-semibold">Requests over time (30d)</h2>
              {timeline.length > 0 ? (
                <ResponsiveContainer width="100%" height={240}>
                  <BarChart data={timeline}>
                    <XAxis dataKey="date" tick={{ fontSize: 11 }} />
                    <YAxis allowDecimals={false} tick={{ fontSize: 11 }} />
                    <Tooltip />
                    <Bar dataKey="ok" stackId="a" fill="#3b82f6" name="OK" />
                    <Bar dataKey="error" stackId="a" fill="#ef4444" name="Error" />
                    <Bar dataKey="denied" stackId="a" fill="#f59e0b" name="Denied" />
                    <Bar dataKey="rate_limited" stackId="a" fill="#f97316" name="Rate Limited" />
                  </BarChart>
                </ResponsiveContainer>
              ) : (
                <p className="py-8 text-center text-sm text-gray-400">No timeline data.</p>
              )}
            </Card>

            {/* Tool breakdown — uses the unified time range from the Activity header above */}
            <Card className="p-4">
              <h2 className="mb-3 text-sm font-semibold">Tool breakdown</h2>
              {(usage.top_tools[range] ?? []).length > 0 ? (
                <ResponsiveContainer width="100%" height={240}>
                  <BarChart data={usage.top_tools[range]} layout="vertical">
                    <XAxis type="number" allowDecimals={false} tick={{ fontSize: 11 }} />
                    <YAxis dataKey="endpoint" type="category" width={140} tick={{ fontSize: 11 }} />
                    <Tooltip />
                    <Bar dataKey="count" fill="#3b82f6" name="Calls" />
                  </BarChart>
                </ResponsiveContainer>
              ) : (
                <p className="py-8 text-center text-sm text-gray-400">No tool data for this range.</p>
              )}
            </Card>
          </div>

          {/* Request log */}
          <Card className="overflow-hidden">
            <div className="border-b border-(--color-border) bg-(--color-bg-secondary) p-4">
              <h2 className="mb-3 text-sm font-semibold">Request log</h2>
              <div className="flex flex-wrap items-center gap-3">
                <select
                  value={endpointFilter}
                  onChange={(e) => {
                    setEndpointFilter(e.target.value)
                    setLogPage(1)
                  }}
                  className="rounded border-0 bg-white dark:bg-[#2c2c2e] px-2 py-1 text-xs shadow-mac"
                >
                  <option value="">All endpoints</option>
                  {allEndpoints.map((ep) => (
                    <option key={ep} value={ep}>
                      {ep}
                    </option>
                  ))}
                </select>
                <select
                  value={statusFilter}
                  onChange={(e) => {
                    setStatusFilter(e.target.value)
                    setLogPage(1)
                  }}
                  className="rounded border-0 bg-white dark:bg-[#2c2c2e] px-2 py-1 text-xs shadow-mac"
                >
                  <option value="">All statuses</option>
                  <option value="ok">ok</option>
                  <option value="error">error</option>
                  <option value="denied">denied</option>
                  <option value="rate_limited">rate limited</option>
                </select>
                <input
                  type="date"
                  value={dateFrom}
                  onChange={(e) => {
                    setDateFrom(e.target.value)
                    setLogPage(1)
                  }}
                  placeholder="From"
                  className="rounded border-0 bg-white dark:bg-[#2c2c2e] px-2 py-1 text-xs shadow-mac"
                />
                <input
                  type="date"
                  value={dateTo}
                  onChange={(e) => {
                    setDateTo(e.target.value)
                    setLogPage(1)
                  }}
                  placeholder="To"
                  className="rounded border-0 bg-white dark:bg-[#2c2c2e] px-2 py-1 text-xs shadow-mac"
                />
              </div>
            </div>

            {requests && requests.items.length > 0 ? (
              <>
                <div className="overflow-x-auto">
                  <table className="min-w-full divide-y divide-(--color-border)">
                    <thead className="bg-(--color-bg-secondary)">
                      <tr>
                        <th className="px-4 py-2 text-left text-xs font-medium uppercase text-gray-500 dark:text-gray-400">
                          Time
                        </th>
                        <th className="px-4 py-2 text-left text-xs font-medium uppercase text-gray-500 dark:text-gray-400">
                          Endpoint
                        </th>
                        <th className="px-4 py-2 text-left text-xs font-medium uppercase text-gray-500 dark:text-gray-400">
                          Params
                        </th>
                        <th className="px-4 py-2 text-left text-xs font-medium uppercase text-gray-500 dark:text-gray-400">
                          Status
                        </th>
                        <th className="px-4 py-2 text-left text-xs font-medium uppercase text-gray-500 dark:text-gray-400">
                          Duration
                        </th>
                        <th className="px-4 py-2 text-left text-xs font-medium uppercase text-gray-500 dark:text-gray-400">
                          Result
                        </th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-(--color-border)">
                      {requests.items.map((r) => {
                        const paramsStr = r.parameters ? JSON.stringify(r.parameters) : ''
                        const resultStr = r.result_summary ? JSON.stringify(r.result_summary) : ''
                        const borderCls =
                          r.status === 'error'
                            ? 'border-l-2 border-red-400'
                            : r.status === 'denied'
                              ? 'border-l-2 border-amber-400'
                              : r.status === 'rate_limited'
                                ? 'border-l-2 border-orange-400'
                                : ''
                        const isExpanded = expandedRow === r.request_id
                        return (
                          <Fragment key={r.request_id}>
                            <tr
                              className={`cursor-pointer hover:bg-black/3 dark:hover:bg-white/3 ${borderCls}`}
                              onClick={() => setExpandedRow(isExpanded ? null : r.request_id)}
                            >
                              <td className="whitespace-nowrap px-4 py-2 text-xs text-gray-600 dark:text-gray-300">
                                {formatTimestamp(r.created_at)}
                              </td>
                              <td className="px-4 py-2 text-xs font-mono">{r.endpoint}</td>
                              <td className="px-4 py-2 text-xs text-gray-500 dark:text-gray-400">
                                {paramsStr ? truncate(paramsStr, 60) : '-'}
                              </td>
                              <td className="px-4 py-2">{statusBadge(r.status)}</td>
                              <td className="px-4 py-2 text-xs text-gray-500 dark:text-gray-400">
                                {r.duration_ms != null ? `${r.duration_ms}ms` : '-'}
                              </td>
                              <td className="px-4 py-2 text-xs text-gray-500 dark:text-gray-400">
                                {resultStr ? truncate(resultStr, 60) : '-'}
                              </td>
                            </tr>
                            {isExpanded && (
                              <tr key={`${r.request_id}-detail`}>
                                <td colSpan={6} className="bg-(--color-bg-secondary) px-4 py-3">
                                  <div className="space-y-2 text-xs font-mono">
                                    {r.parameters && (
                                      <div>
                                        <span className="font-semibold">Parameters:</span>
                                        <pre className="mt-1 overflow-x-auto rounded bg-white dark:bg-[#1e1e1f] p-2">
                                          {JSON.stringify(r.parameters, null, 2)}
                                        </pre>
                                      </div>
                                    )}
                                    {(r.result_summary || r.status_detail) && (
                                      <div>
                                        <span className="font-semibold">{r.status_detail ? 'Error:' : 'Result:'}</span>
                                        <pre className="mt-1 overflow-x-auto rounded bg-white dark:bg-[#1e1e1f] p-2">
                                          {r.status_detail ?? JSON.stringify(r.result_summary, null, 2)}
                                        </pre>
                                      </div>
                                    )}
                                  </div>
                                </td>
                              </tr>
                            )}
                          </Fragment>
                        )
                      })}
                    </tbody>
                  </table>
                </div>

                {/* Pagination */}
                <div className="flex flex-wrap items-center justify-between border-t border-(--color-border) px-4 py-3">
                  <span className="text-xs text-gray-500 dark:text-gray-400">
                    Showing {reqStart}-{reqEnd} of {requests.total}
                  </span>
                  <div className="flex items-center gap-3">
                    <select
                      value={logPageSize}
                      onChange={(e) => {
                        setLogPageSize(Number(e.target.value))
                        setLogPage(1)
                      }}
                      className="rounded border-0 bg-(--color-bg-secondary) px-1.5 py-0.5 text-xs"
                    >
                      {PAGE_SIZES.map((s) => (
                        <option key={s} value={s}>
                          {s} / page
                        </option>
                      ))}
                    </select>
                    <button
                      onClick={() => setLogPage((p) => Math.max(1, p - 1))}
                      disabled={logPage <= 1}
                      className="rounded bg-(--color-bg-secondary) px-2 py-1 text-xs disabled:opacity-40"
                    >
                      Prev
                    </button>
                    <span className="text-xs text-gray-500 dark:text-gray-400">
                      {logPage} / {totalPages || 1}
                    </span>
                    <button
                      onClick={() => setLogPage((p) => Math.min(totalPages, p + 1))}
                      disabled={logPage >= totalPages}
                      className="rounded bg-(--color-bg-secondary) px-2 py-1 text-xs disabled:opacity-40"
                    >
                      Next
                    </button>
                  </div>
                </div>
              </>
            ) : (
              <p className="px-4 py-6 text-center text-sm text-gray-500 dark:text-gray-400">
                No requests match the current filters.
              </p>
            )}
          </Card>
        </>
      )}
    </div>
  )
}

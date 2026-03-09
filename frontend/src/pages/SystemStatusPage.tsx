import { useEffect, useState } from 'react'
import { get } from '../api'

interface HealthCheck {
  status: string
  checks: {
    postgres: string
    storage: string
    tika: string
  }
}

interface ServiceStats {
  [key: string]: number | string | null | undefined
}

interface StatsResponse {
  postgres?: ServiceStats
  storage?: ServiceStats
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
  if (value == null) return '\u2014'
  if (typeof value === 'string') return value
  if (key.endsWith('_mb')) return `${value} MB`
  if (key === 'cache_hit_ratio') return `${(value * 100).toFixed(1)}%`
  return value.toLocaleString()
}

export default function SystemStatusPage() {
  const [health, setHealth] = useState<HealthCheck | null>(null)
  const [stats, setStats] = useState<StatsResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [statsLoading, setStatsLoading] = useState(true)
  const [error, setError] = useState('')

  async function loadHealth() {
    try {
      const data = await get<HealthCheck>('/api/system/health')
      setHealth(data)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load health')
    } finally {
      setLoading(false)
    }
  }

  async function loadStats() {
    setStatsLoading(true)
    try {
      const data = await get<StatsResponse>('/api/system/stats')
      setStats(data)
    } catch {
      // Stats are non-critical; silently ignore (user may not be admin)
    } finally {
      setStatsLoading(false)
    }
  }

  useEffect(() => {
    loadHealth()
    loadStats()
  }, [])

  function handleRefresh() {
    loadHealth()
    loadStats()
  }

  if (loading) return <div className="text-gray-500 dark:text-gray-400">Loading...</div>

  return (
    <div className="animate-slide-in">
      <div className="mb-4 flex items-center justify-between">
        <h1 className="text-xl font-bold">System Status</h1>
        <button
          onClick={handleRefresh}
          className="rounded-lg bg-(--color-bg-tertiary) px-4 py-2 text-sm font-medium text-gray-700 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-600"
        >
          Refresh
        </button>
      </div>

      {error && (
        <div className="mb-4 rounded-sm bg-red-50 dark:bg-red-900/20 px-3 py-2 text-sm text-red-700 dark:text-red-400">
          {error}
        </div>
      )}

      <h2 className="mb-3 text-lg font-semibold">Health Checks</h2>
      {health && (
        <div className="mb-6 grid grid-cols-3 gap-4">
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
        </div>
      )}

      {health && (
        <div className="mb-6">
          <span
            className={`rounded-md px-3 py-1 text-[11px] font-medium ${
              health.status === 'healthy'
                ? 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400'
                : 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400'
            }`}
          >
            Overall: {health.status}
          </span>
        </div>
      )}
    </div>
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
  const ok = status === 'ok'
  const statEntries = stats ? Object.entries(stats).filter(([k]) => k !== 'error') : []

  return (
    <div
      className={`rounded-xl shadow-mac ring-1 ring-(--color-border) p-4 ${
        ok ? 'bg-green-50 dark:bg-green-900/20' : 'bg-red-50 dark:bg-red-900/20'
      }`}
    >
      <div className="text-sm font-medium text-gray-700 dark:text-gray-300">{name}</div>
      <div
        className={`mt-1 text-lg font-bold ${ok ? 'text-green-700 dark:text-green-400' : 'text-red-700 dark:text-red-400'}`}
      >
        {ok ? 'OK' : status}
      </div>
      {statsLoading && !stats && <div className="mt-2 text-xs text-gray-400">Loading stats...</div>}
      {stats?.error && <div className="mt-2 text-xs text-red-500">{String(stats.error)}</div>}
      {statEntries.length > 0 && (
        <dl className="mt-3 space-y-1 border-t border-gray-200 dark:border-gray-700 pt-2">
          {statEntries.map(([key, value]) => (
            <div key={key} className="flex justify-between text-xs">
              <dt className="text-gray-500 dark:text-gray-400">{STAT_LABELS[key] || key}</dt>
              <dd className="font-medium text-gray-700 dark:text-gray-300">{formatStatValue(key, value)}</dd>
            </div>
          ))}
        </dl>
      )}
    </div>
  )
}

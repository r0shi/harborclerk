import { Fragment, useEffect, useRef, useState } from 'react'
import { useAuth } from '../auth'
import { del, get, post, put } from '../api'
import { useLLMStatusContext } from '../components/LLMStatusBanner'
import { PageHeader } from '../components/PageHeader'
import { Card } from '../components/Card'
import { StatusPill } from '../components/StatusPill'
import { modelGuidance } from '../utils/modelGuidance'

interface ModelInfo {
  id: string
  name: string
  size_bytes: number
  context_window: number
  supports_tools: boolean
  supports_research: boolean
  downloaded: boolean
  active: boolean
  downloading: boolean
  yarn_available: boolean
  yarn_extended_context: number | null
}

interface OrphanInfo {
  filename: string
  size_bytes: number
}

function formatSize(bytes: number): string {
  if (bytes >= 1e9) return `${(bytes / 1e9).toFixed(1)} GB`
  if (bytes >= 1e6) return `${(bytes / 1e6).toFixed(0)} MB`
  return `${bytes} B`
}

export default function ModelsPage() {
  const { token } = useAuth()
  const { markTransitioning } = useLLMStatusContext()
  const [models, setModels] = useState<ModelInfo[]>([])
  const [orphans, setOrphans] = useState<OrphanInfo[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [downloading, setDownloading] = useState<Set<string>>(new Set())
  const [downloadProgress, setDownloadProgress] = useState<Map<string, number>>(new Map())
  const [yarnEnabled, setYarnEnabled] = useState(false)
  const [summaryForceAfm, setSummaryForceAfm] = useState(false)

  const loadModelsRef = useRef(loadModels)
  useEffect(() => {
    loadModelsRef.current = loadModels
  })

  async function loadModels() {
    try {
      const data = await get<ModelInfo[]>('/api/chat/models')
      setModels(data)
      const yarnStatus = await get<{ yarn_enabled: boolean }>('/api/chat/models/yarn')
      setYarnEnabled(yarnStatus.yarn_enabled)
      const afmStatus = await get<{ summary_force_apple_intelligence: boolean }>('/api/chat/models/summary-afm')
      setSummaryForceAfm(afmStatus.summary_force_apple_intelligence)
      const orphanList = await get<OrphanInfo[]>('/api/chat/models/orphaned')
      setOrphans(orphanList)
      // Seed downloading set from server state
      setDownloading((prev) => {
        const next = new Set(prev)
        for (const m of data) {
          if (m.downloading) next.add(m.id)
        }
        return next
      })
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load models')
    } finally {
      setLoading(false)
    }
  }

  async function handleDeleteOrphan(filename: string) {
    setError('')
    try {
      await del(`/api/chat/models/orphaned/${encodeURIComponent(filename)}`)
      loadModels()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Delete failed')
    }
  }

  useEffect(() => {
    loadModels()
  }, [])

  // Subscribe to download progress via SSE with auto-reconnect
  const tokenRef = useRef(token)
  useEffect(() => {
    tokenRef.current = token
  }, [token])
  useEffect(() => {
    if (!token) return

    const controller = new AbortController()
    let reconnectTimer: ReturnType<typeof setTimeout> | undefined

    async function connect() {
      const currentToken = tokenRef.current
      if (!currentToken) return
      try {
        const res = await fetch('/api/chat/models/download-progress', {
          headers: { Authorization: `Bearer ${currentToken}` },
          signal: controller.signal,
        })
        if (!res.ok || !res.body) return

        const reader = res.body.getReader()
        const decoder = new TextDecoder()
        let buffer = ''

        while (true) {
          const { done, value } = await reader.read()
          if (done) break

          buffer += decoder.decode(value, { stream: true })
          const lines = buffer.split('\n')
          buffer = lines.pop() || ''

          for (const line of lines) {
            if (!line.startsWith('data: ')) continue
            try {
              const event = JSON.parse(line.slice(6))
              if (event.status === 'downloading') {
                setDownloading((prev) => new Set(prev).add(event.model_id))
                if (event.progress != null) {
                  setDownloadProgress((prev) => new Map(prev).set(event.model_id, event.progress))
                }
              } else if (event.status === 'complete') {
                setDownloading((prev) => {
                  const next = new Set(prev)
                  next.delete(event.model_id)
                  return next
                })
                setDownloadProgress((prev) => {
                  const next = new Map(prev)
                  next.delete(event.model_id)
                  return next
                })
                loadModelsRef.current()
              } else if (event.status === 'error') {
                setDownloading((prev) => {
                  const next = new Set(prev)
                  next.delete(event.model_id)
                  return next
                })
                setDownloadProgress((prev) => {
                  const next = new Map(prev)
                  next.delete(event.model_id)
                  return next
                })
                setError(`Download failed for ${event.model_id}: ${event.error}`)
              }
            } catch {
              // ignore
            }
          }
        }
      } catch (e) {
        if (e instanceof DOMException && e.name === 'AbortError') return
      }
      // Reconnect after delay (unless aborted)
      if (!controller.signal.aborted) {
        reconnectTimer = setTimeout(connect, 5000)
      }
    }

    connect()
    return () => {
      controller.abort()
      if (reconnectTimer) clearTimeout(reconnectTimer)
    }
  }, [token])

  async function handleDownload(modelId: string) {
    setError('')
    setDownloading((prev) => new Set(prev).add(modelId))
    setDownloadProgress((prev) => new Map(prev).set(modelId, 0))
    try {
      const result = await post<{ status: string }>(`/api/chat/models/${modelId}/download`)
      if (result.status === 'already_downloading' || result.status === 'already_downloaded') {
        setDownloading((prev) => {
          const next = new Set(prev)
          next.delete(modelId)
          return next
        })
        setDownloadProgress((prev) => {
          const next = new Map(prev)
          next.delete(modelId)
          return next
        })
        if (result.status === 'already_downloaded') {
          loadModels()
        }
      }
    } catch (e) {
      setDownloading((prev) => {
        const next = new Set(prev)
        next.delete(modelId)
        return next
      })
      setDownloadProgress((prev) => {
        const next = new Map(prev)
        next.delete(modelId)
        return next
      })
      setError(e instanceof Error ? e.message : 'Download failed')
    }
  }

  async function handleActivate(modelId: string) {
    setError('')
    try {
      await put(`/api/chat/models/${modelId}/activate`)
      // Tell the LLM-status banner to expect a transition. The
      // backend just wrote config; the Swift host will pick it up
      // within a few seconds and restart llama-server, which then
      // takes another 30-60s to load weights for big models.
      markTransitioning()
      loadModels()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Activation failed')
    }
  }

  async function handleDelete(modelId: string) {
    setError('')
    try {
      await del(`/api/chat/models/${modelId}`)
      loadModels()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Delete failed')
    }
  }

  async function handleYarnToggle(enabled: boolean) {
    setError('')
    try {
      await put(`/api/chat/models/yarn?enabled=${enabled}`)
      setYarnEnabled(enabled)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to toggle YaRN')
    }
  }

  async function handleSummaryForceAfmToggle(enabled: boolean) {
    setError('')
    try {
      await put(`/api/chat/models/summary-afm?enabled=${enabled}`)
      setSummaryForceAfm(enabled)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to toggle Apple Intelligence summaries')
    }
  }

  async function handleDeactivate() {
    setError('')
    try {
      await put('/api/chat/models/deactivate')
      // Deactivation also triggers an llm_restart on the host side
      // (Swift stops llama-server). System feels sluggish for a few
      // seconds while ~22 GB unmaps; banner conveys that.
      markTransitioning(15_000)
      loadModels()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Deactivation failed')
    }
  }

  if (loading) {
    return <div className="text-sm text-gray-500 dark:text-gray-400">Loading models...</div>
  }

  return (
    <div className="animate-slide-in">
      <PageHeader
        title="Models"
        subtitle="Choose local AI models for cited answers, research, and document summaries."
      />

      {error && (
        <div className="mb-4 rounded-sm bg-red-50 dark:bg-red-900/20 px-3 py-2 text-sm text-red-700 dark:text-red-400">
          {error}
        </div>
      )}

      <Card className="mb-4 p-4">
        <div className="text-sm font-medium text-gray-900 dark:text-gray-100">Download, then activate one model.</div>
        <div className="mt-2 grid gap-3 text-xs leading-relaxed text-gray-500 dark:text-gray-400 md:grid-cols-[minmax(0,1fr)_minmax(220px,0.45fr)]">
          <div className="space-y-2">
            <p>
              Ask and Research need one active local AI model. Larger models are better for research and synthesis;
              smaller models are still useful for quick lookup.
            </p>
            <p>
              You can skip this setup and still use Search, Documents, Folders, and Integrations. Important AI answers
              should be checked against citations.
            </p>
          </div>
          <ol className="space-y-1 border-l-2 border-blue-500/70 pl-4 text-gray-600 dark:text-gray-300">
            <li>1. Pick a model for this Mac.</li>
            <li>2. Download it.</li>
            <li>3. Activate it when ready.</li>
          </ol>
        </div>
      </Card>

      <Card className="overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-(--color-bg-secondary)">
            <tr>
              <th className="px-4 py-3 text-left font-medium text-gray-700 dark:text-gray-300">Model</th>
              <th className="px-4 py-3 text-left font-medium text-gray-700 dark:text-gray-300">Size</th>
              <th className="px-4 py-3 text-left font-medium text-gray-700 dark:text-gray-300">Context</th>
              <th className="px-4 py-3 text-left font-medium text-gray-700 dark:text-gray-300">Tools</th>
              <th className="px-4 py-3 text-left font-medium text-gray-700 dark:text-gray-300">Research</th>
              <th className="px-4 py-3 text-left font-medium text-gray-700 dark:text-gray-300">Status</th>
              <th className="px-4 py-3 text-right font-medium text-gray-700 dark:text-gray-300">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-(--color-border)">
            {(
              [
                { label: '16 GB RAM', min: 0, max: 4_000_000_000 },
                { label: '32 GB RAM', min: 4_000_000_000, max: 12_000_000_000 },
                { label: '48+ GB RAM', min: 12_000_000_000, max: Infinity },
              ] as const
            )
              .map((tier) => ({
                ...tier,
                items: models
                  .filter((m) => m.size_bytes >= tier.min && m.size_bytes < tier.max)
                  .sort((a, b) => a.size_bytes - b.size_bytes),
              }))
              .filter((tier) => tier.items.length > 0)
              .map((tier) => (
                <Fragment key={tier.label}>
                  <tr>
                    <td
                      colSpan={7}
                      className="bg-(--color-bg-secondary) px-4 py-2 text-left text-xs font-bold uppercase tracking-wider text-gray-500 dark:text-gray-400"
                    >
                      {tier.label}
                    </td>
                  </tr>
                  {tier.items.map((model) => {
                    const progress = downloadProgress.get(model.id)
                    const guidance = modelGuidance(model)
                    return (
                      <tr key={model.id} className="bg-white dark:bg-[#2c2c2e]">
                        <td className="px-4 py-3">
                          <div className="font-medium text-gray-900 dark:text-gray-100">{model.name}</div>
                          <div className="text-xs text-gray-500 dark:text-gray-400">{model.id}</div>
                          <div className="mt-1.5 inline-flex rounded-md bg-blue-50 px-2 py-0.5 text-[11px] font-medium text-blue-700 dark:bg-blue-900/25 dark:text-blue-300">
                            {guidance.label}
                          </div>
                          <div className="mt-1 max-w-xs text-xs leading-snug text-gray-500 dark:text-gray-400">
                            {guidance.note}
                          </div>
                          {guidance.warning && (
                            <div className="mt-1 max-w-xs text-[11px] leading-snug text-amber-700 dark:text-amber-400">
                              {guidance.warning}
                            </div>
                          )}
                        </td>
                        <td className="px-4 py-3 text-gray-700 dark:text-gray-300">{formatSize(model.size_bytes)}</td>
                        <td className="px-4 py-3 text-gray-700 dark:text-gray-300">
                          <span>{model.context_window.toLocaleString()}</span>
                          {model.yarn_available && model.yarn_extended_context && (
                            <span className="ml-1 text-xs text-gray-400 dark:text-gray-500">
                              ({yarnEnabled ? '' : '→ '}
                              {model.yarn_extended_context.toLocaleString()}
                              {yarnEnabled ? ' via YaRN' : ' w/ YaRN'})
                            </span>
                          )}
                        </td>
                        <td className="px-4 py-3">
                          {model.supports_tools ? (
                            <span className="inline-flex items-center rounded-md bg-green-100 dark:bg-green-900/30 px-2 py-0.5 text-[11px] font-medium text-green-700 dark:text-green-400">
                              Yes
                            </span>
                          ) : (
                            <span className="inline-flex items-center rounded-md bg-gray-100 dark:bg-gray-700 px-2 py-0.5 text-[11px] text-gray-500 dark:text-gray-400">
                              No
                            </span>
                          )}
                        </td>
                        <td className="px-4 py-3">
                          {model.supports_research ? (
                            <span className="inline-flex items-center rounded-md bg-green-100 dark:bg-green-900/30 px-2 py-0.5 text-[11px] font-medium text-green-700 dark:text-green-400">
                              Yes
                            </span>
                          ) : (
                            <span
                              className="inline-flex items-center rounded-md bg-amber-100 dark:bg-amber-900/30 px-2 py-0.5 text-[11px] font-medium text-amber-700 dark:text-amber-400"
                              title="Not recommended for Research mode — chat works fine"
                            >
                              Chat only
                            </span>
                          )}
                        </td>
                        <td className="px-4 py-3">
                          {model.active ? (
                            <StatusPill state="active" label="Active" />
                          ) : model.downloaded ? (
                            <StatusPill state="idle" label="Ready" />
                          ) : downloading.has(model.id) ? (
                            <div className="flex items-center gap-2">
                              <div className="h-1.5 w-24 overflow-hidden rounded-full bg-gray-200 dark:bg-gray-700">
                                <div
                                  className="h-full rounded-full bg-amber-500 transition-all duration-300"
                                  style={{
                                    width: `${Math.min(progress ?? 0, 100)}%`,
                                  }}
                                />
                              </div>
                              <span className="text-xs tabular-nums text-amber-600 dark:text-amber-400">
                                {Math.round(progress ?? 0)}%
                              </span>
                            </div>
                          ) : (
                            <StatusPill state="idle" label="Not downloaded" />
                          )}
                        </td>
                        <td className="px-4 py-3 text-right">
                          <div className="flex items-center justify-end gap-2">
                            {model.active && (
                              <button
                                onClick={() => handleDeactivate()}
                                className="rounded-lg border border-gray-400 px-3 py-1 text-xs font-medium text-gray-600 shadow-xs hover:bg-gray-50 dark:text-gray-300 dark:border-gray-500 dark:hover:bg-gray-700/50"
                              >
                                Deactivate
                              </button>
                            )}
                            {!model.downloaded && !downloading.has(model.id) && (
                              <button
                                onClick={() => handleDownload(model.id)}
                                className="rounded-lg bg-blue-600 px-3 py-1 text-xs font-medium text-white shadow-xs hover:bg-blue-700"
                              >
                                Download
                              </button>
                            )}
                            {model.downloaded && !model.active && (
                              <>
                                <button
                                  onClick={() => handleActivate(model.id)}
                                  className="rounded-lg border border-blue-600 px-3 py-1 text-xs font-medium text-blue-600 shadow-xs hover:bg-blue-50 dark:text-blue-400 dark:border-blue-400 dark:hover:bg-blue-900/20"
                                >
                                  Activate
                                </button>
                                <button
                                  onClick={() => handleDelete(model.id)}
                                  className="rounded-lg bg-red-600 px-3 py-1 text-xs font-medium text-white shadow-xs hover:bg-red-700"
                                >
                                  Delete
                                </button>
                              </>
                            )}
                          </div>
                        </td>
                      </tr>
                    )
                  })}
                </Fragment>
              ))}
          </tbody>
        </table>
      </Card>

      {/* Orphaned model files */}
      {orphans.length > 0 && (
        <Card className="mt-6 p-4">
          <div className="font-medium text-gray-900 dark:text-gray-100">Orphaned model files</div>
          <p className="mt-0.5 mb-3 text-xs text-gray-500 dark:text-gray-400">
            GGUF files on disk that are no longer in the model registry — typically left over from removed or replaced
            models. Safe to delete to reclaim space.
          </p>
          <ul className="divide-y divide-(--color-border)">
            {orphans.map((o) => (
              <li key={o.filename} className="flex items-center justify-between py-2">
                <div>
                  <div className="font-mono text-xs text-gray-700 dark:text-gray-300">{o.filename}</div>
                  <div className="text-xs text-gray-500 dark:text-gray-400">{formatSize(o.size_bytes)}</div>
                </div>
                <button
                  onClick={() => handleDeleteOrphan(o.filename)}
                  className="rounded-lg bg-red-600 px-3 py-1 text-xs font-medium text-white shadow-xs hover:bg-red-700"
                >
                  Delete
                </button>
              </li>
            ))}
          </ul>
        </Card>
      )}

      {/* YaRN Extended Context */}
      {models.some((m) => m.yarn_available) && (
        <Card className="mt-6 p-4">
          <div className="flex items-center justify-between">
            <div>
              <div className="font-medium text-gray-900 dark:text-gray-100">Extended Context (YaRN)</div>
              <p className="mt-0.5 text-xs text-gray-500 dark:text-gray-400">
                Extends context window from 32K to 131K tokens for supported models. Uses more memory and may reduce
                quality on shorter inputs.
              </p>
            </div>
            <button
              onClick={() => handleYarnToggle(!yarnEnabled)}
              className={`relative ml-4 inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full transition-colors duration-200 ${
                yarnEnabled ? 'bg-blue-600' : 'bg-gray-300 dark:bg-gray-600'
              }`}
              role="switch"
              aria-checked={yarnEnabled}
            >
              <span
                className={`inline-block h-5 w-5 transform rounded-full bg-white shadow-sm transition-transform duration-200 ${
                  yarnEnabled ? 'translate-x-[22px]' : 'translate-x-[2px]'
                } mt-[2px]`}
              />
            </button>
          </div>
          {yarnEnabled && (
            <p className="mt-2 text-[11px] text-amber-600 dark:text-amber-400">
              LLM server will restart with extended context. Not recommended if most prompts are under 32K tokens.
            </p>
          )}
        </Card>
      )}

      {/* Always use Apple Intelligence for summaries */}
      <Card className="mt-4 p-4">
        <div className="flex items-center justify-between">
          <div>
            <div className="font-medium text-gray-900 dark:text-gray-100">
              Always use Apple Intelligence for summaries
            </div>
            <p className="mt-0.5 text-xs text-gray-500 dark:text-gray-400">
              Skips the local LLM for the summarize stage and uses Apple Intelligence instead. Faster and lower-power on
              Apple Silicon. Chat and research still use the active model; document-type classification uses Apple
              Intelligence or file-type fallback.
            </p>
          </div>
          <button
            onClick={() => handleSummaryForceAfmToggle(!summaryForceAfm)}
            className={`relative ml-4 inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full transition-colors duration-200 ${
              summaryForceAfm ? 'bg-blue-600' : 'bg-gray-300 dark:bg-gray-600'
            }`}
            role="switch"
            aria-checked={summaryForceAfm}
          >
            <span
              className={`inline-block h-5 w-5 transform rounded-full bg-white shadow-sm transition-transform duration-200 ${
                summaryForceAfm ? 'translate-x-[22px]' : 'translate-x-[2px]'
              } mt-[2px]`}
            />
          </button>
        </div>
      </Card>
    </div>
  )
}

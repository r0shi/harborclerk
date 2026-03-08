import { useEffect, useRef, useState } from 'react'
import { useAuth } from '../auth'
import { del, get, post, put } from '../api'

interface ModelInfo {
  id: string
  name: string
  size_bytes: number
  context_window: number
  supports_tools: boolean
  downloaded: boolean
  active: boolean
  downloading: boolean
  yarn_available: boolean
  yarn_extended_context: number | null
}

function formatSize(bytes: number): string {
  if (bytes >= 1e9) return `${(bytes / 1e9).toFixed(1)} GB`
  if (bytes >= 1e6) return `${(bytes / 1e6).toFixed(0)} MB`
  return `${bytes} B`
}

export default function ModelsPage() {
  const { token } = useAuth()
  const [models, setModels] = useState<ModelInfo[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [downloading, setDownloading] = useState<Set<string>>(new Set())
  const [downloadProgress, setDownloadProgress] = useState<Map<string, number>>(new Map())
  const [yarnEnabled, setYarnEnabled] = useState(false)

  const loadModelsRef = useRef(loadModels)
  loadModelsRef.current = loadModels

  async function loadModels() {
    try {
      const data = await get<ModelInfo[]>('/api/chat/models')
      setModels(data)
      const yarnStatus = await get<{ yarn_enabled: boolean }>('/api/chat/models/yarn')
      setYarnEnabled(yarnStatus.yarn_enabled)
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

  useEffect(() => {
    loadModels()
  }, [])

  // Subscribe to download progress via SSE with auto-reconnect
  const tokenRef = useRef(token)
  tokenRef.current = token
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

  async function handleDeactivate() {
    setError('')
    try {
      await put('/api/chat/models/deactivate')
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
      <h1 className="mb-4 text-xl font-bold">LLM Models</h1>
      <p className="mb-4 text-sm text-gray-500 dark:text-gray-400">
        Download and manage models for the built-in chat assistant. Models run locally on this machine.
      </p>

      {error && (
        <div className="mb-4 rounded-sm bg-red-50 dark:bg-red-900/20 px-3 py-2 text-sm text-red-700 dark:text-red-400">
          {error}
        </div>
      )}

      <div className="overflow-hidden rounded-xl bg-white dark:bg-[#2c2c2e] shadow-mac">
        <table className="w-full text-sm">
          <thead className="bg-(--color-bg-secondary)">
            <tr>
              <th className="px-4 py-3 text-left font-medium text-gray-700 dark:text-gray-300">Model</th>
              <th className="px-4 py-3 text-left font-medium text-gray-700 dark:text-gray-300">Size</th>
              <th className="px-4 py-3 text-left font-medium text-gray-700 dark:text-gray-300">Context</th>
              <th className="px-4 py-3 text-left font-medium text-gray-700 dark:text-gray-300">Tools</th>
              <th className="px-4 py-3 text-left font-medium text-gray-700 dark:text-gray-300">Status</th>
              <th className="px-4 py-3 text-right font-medium text-gray-700 dark:text-gray-300">Actions</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-200 dark:divide-gray-700">
            {models.map((model) => {
              const progress = downloadProgress.get(model.id)
              return (
                <tr key={model.id} className="bg-white dark:bg-[#2c2c2e]">
                  <td className="px-4 py-3">
                    <div className="font-medium text-gray-900 dark:text-gray-100">{model.name}</div>
                    <div className="text-xs text-gray-500 dark:text-gray-400">{model.id}</div>
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
                    {model.active ? (
                      <span className="inline-flex items-center rounded-md bg-blue-100 dark:bg-blue-900/30 px-2 py-0.5 text-[11px] font-medium text-blue-700 dark:text-blue-400">
                        Active
                      </span>
                    ) : model.downloaded ? (
                      <span className="inline-flex items-center rounded-md bg-green-100 dark:bg-green-900/30 px-2 py-0.5 text-[11px] font-medium text-green-700 dark:text-green-400">
                        Ready
                      </span>
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
                      <span className="text-xs text-gray-400">Not downloaded</span>
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
          </tbody>
        </table>
      </div>

      {/* YaRN Extended Context */}
      {models.some((m) => m.yarn_available) && (
        <div className="mt-6 rounded-xl bg-white dark:bg-[#2c2c2e] shadow-mac p-4">
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
        </div>
      )}
    </div>
  )
}

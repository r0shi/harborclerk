import { useCallback, useEffect, useState } from 'react'
import type { Dispatch, SetStateAction } from 'react'
import { del, get, patch, post } from '../api'

interface ToolStatus {
  status: 'not_installed' | 'installed' | 'failed'
  size_bytes?: number | null
}

interface LanguageSummary {
  code: string
  display_name: string
  built_in: boolean
  enabled: boolean
  tools: Record<string, ToolStatus>
}

interface ApiResponse {
  languages: LanguageSummary[]
}

interface InstallResultItem {
  tool: string
  status: string
  error: string | null
}

interface InstallResponse {
  results: InstallResultItem[]
}

function markBusy(setBusy: Dispatch<SetStateAction<Set<string>>>, code: string, busy: boolean) {
  setBusy((prev) => {
    const next = new Set(prev)
    if (busy) next.add(code)
    else next.delete(code)
    return next
  })
}

function formatBytes(n: number | null | undefined): string {
  if (n == null) return ''
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`
  return `${(n / (1024 * 1024)).toFixed(1)} MB`
}

function totalDownloadSize(lang: LanguageSummary): number {
  return Object.values(lang.tools).reduce((acc, t) => acc + (t.size_bytes ?? 0), 0)
}

export default function LanguagesPage() {
  const [languages, setLanguages] = useState<LanguageSummary[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState<Set<string>>(new Set()) // language codes currently mid-action

  const reload = useCallback(async () => {
    try {
      const data = await get<ApiResponse>('/api/languages')
      setLanguages(data.languages)
      setError('')
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load languages')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    // ESLint's react-hooks/set-state-in-effect can't see through useCallback,
    // so it flags any direct call here even though setState only runs after
    // the awaited fetch resolves. Wrapping in an inline async fn (the
    // documented codebase pattern — see MEMORY.md "ESLint react-hooks/
    // set-state-in-effect") sidesteps the false positive.
    let cancelled = false
    async function loadInitial() {
      try {
        const data = await get<ApiResponse>('/api/languages')
        if (cancelled) return
        setLanguages(data.languages)
        setError('')
      } catch (e) {
        if (cancelled) return
        setError(e instanceof Error ? e.message : 'Failed to load languages')
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    loadInitial()
    return () => {
      cancelled = true
    }
  }, [])

  // Persist enabled_languages whenever the user toggles a language. The
  // backend normalises (always inserts 'en' first, dedupes, validates),
  // so we just send the new desired set.
  const setEnabled = useCallback(
    async (code: string, enable: boolean) => {
      const next = enable
        ? Array.from(new Set([...languages.filter((l) => l.enabled).map((l) => l.code), code]))
        : languages.filter((l) => l.enabled && l.code !== code).map((l) => l.code)
      try {
        await patch('/api/me/preferences', { enabled_languages: next })
        await reload()
      } catch (e) {
        setError(e instanceof Error ? e.message : 'Failed to update enabled languages')
      }
    },
    [languages, reload],
  )

  const installTools = useCallback(
    async (code: string, tools: string[]) => {
      markBusy(setBusy, code, true)
      try {
        const result = await post<InstallResponse>(`/api/languages/${code}/install`, { tools })
        const failures = result.results.filter((r) => r.status === 'failed')
        if (failures.length > 0) {
          setError(`Install failed for ${failures.map((f) => `${code}/${f.tool}: ${f.error ?? 'unknown'}`).join(', ')}`)
        }
        await reload()
      } catch (e) {
        setError(e instanceof Error ? e.message : 'Failed to install language pack')
      } finally {
        markBusy(setBusy, code, false)
      }
    },
    [reload],
  )

  const removeTool = useCallback(
    async (code: string, tool: string) => {
      markBusy(setBusy, code, true)
      try {
        await del(`/api/languages/${code}/install/${tool}`)
        await reload()
      } catch (e) {
        setError(e instanceof Error ? e.message : 'Failed to remove language pack')
      } finally {
        markBusy(setBusy, code, false)
      }
    },
    [reload],
  )

  const installAndEnable = useCallback(
    async (lang: LanguageSummary) => {
      const tools = Object.entries(lang.tools)
        .filter(([, t]) => t.status !== 'installed')
        .map(([name]) => name)
      if (tools.length > 0) {
        await installTools(lang.code, tools)
      }
      if (!lang.enabled) {
        await setEnabled(lang.code, true)
      }
    },
    [installTools, setEnabled],
  )

  if (loading) return <div className="text-gray-500 dark:text-gray-400">Loading languages...</div>

  return (
    <div>
      <h1 className="mb-4 text-xl font-bold">Languages</h1>
      <p className="mb-4 text-sm text-(--color-text-secondary)">
        Choose which languages Harbor Clerk should process. English is built-in. Other languages download per-tool model
        files (Tesseract OCR, spaCy entities) on demand.
      </p>

      {error && (
        <div className="mb-3 rounded-lg bg-red-50 dark:bg-red-900/20 px-3 py-2 text-sm text-red-700 dark:text-red-400">
          {error}
        </div>
      )}

      <div className="rounded-xl bg-white dark:bg-[#2c2c2e] shadow-mac ring-1 ring-(--color-border) overflow-hidden divide-y divide-(--color-border)">
        {languages.map((lang) => {
          const isBusy = busy.has(lang.code)
          const installedToolCount = Object.values(lang.tools).filter((t) => t.status === 'installed').length
          const totalToolCount = Object.keys(lang.tools).length
          const allInstalled = totalToolCount > 0 && installedToolCount === totalToolCount

          return (
            <div key={lang.code} className="px-4 py-3.5">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="text-[14px] font-medium text-(--color-text-primary)">{lang.display_name}</span>
                    <span className="text-[11px] font-mono text-gray-400">{lang.code}</span>
                    {lang.built_in && (
                      <span className="rounded-md bg-blue-100 dark:bg-blue-900/30 px-1.5 py-0.5 text-[10px] font-medium text-blue-700 dark:text-blue-400">
                        built-in
                      </span>
                    )}
                    {lang.enabled && (
                      <span className="rounded-md bg-green-100 dark:bg-green-900/30 px-1.5 py-0.5 text-[10px] font-medium text-green-700 dark:text-green-400">
                        enabled
                      </span>
                    )}
                  </div>

                  {!lang.built_in && (
                    <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[12px] text-(--color-text-secondary)">
                      {Object.entries(lang.tools).map(([toolName, status]) => (
                        <ToolStatusBadge
                          key={toolName}
                          tool={toolName}
                          status={status}
                          onRemove={isBusy ? undefined : () => removeTool(lang.code, toolName)}
                          onInstall={
                            isBusy || status.status === 'installed'
                              ? undefined
                              : () => installTools(lang.code, [toolName])
                          }
                        />
                      ))}
                      {totalToolCount > 0 && (
                        <span className="text-gray-400">total {formatBytes(totalDownloadSize(lang))}</span>
                      )}
                    </div>
                  )}
                </div>

                <div className="flex shrink-0 items-center gap-2">
                  {lang.built_in ? (
                    <span className="text-[11px] text-gray-400">always on</span>
                  ) : !allInstalled && !lang.enabled ? (
                    <button
                      type="button"
                      disabled={isBusy}
                      onClick={() => installAndEnable(lang)}
                      className="rounded-md bg-blue-600 px-3 py-1.5 text-[12px] font-medium text-white shadow-xs hover:bg-blue-700 disabled:opacity-50"
                    >
                      {isBusy ? 'Working...' : 'Install & enable'}
                    </button>
                  ) : (
                    <button
                      type="button"
                      disabled={isBusy}
                      onClick={() => setEnabled(lang.code, !lang.enabled)}
                      className={`rounded-md px-3 py-1.5 text-[12px] font-medium shadow-xs disabled:opacity-50 ${
                        lang.enabled
                          ? 'bg-(--color-bg-tertiary) text-(--color-text-secondary) hover:opacity-80'
                          : 'bg-blue-600 text-white hover:bg-blue-700'
                      }`}
                    >
                      {isBusy ? 'Working...' : lang.enabled ? 'Disable' : 'Enable'}
                    </button>
                  )}
                </div>
              </div>
            </div>
          )
        })}
      </div>

      <p className="mt-4 text-[11px] text-gray-400">
        Worker restart required to pick up newly-installed packs. Disabling a language stops new documents from using it
        for OCR; existing documents keep their previously-extracted text.
      </p>
    </div>
  )
}

function ToolStatusBadge({
  tool,
  status,
  onInstall,
  onRemove,
}: {
  tool: string
  status: ToolStatus
  onInstall?: () => void
  onRemove?: () => void
}) {
  const installed = status.status === 'installed'
  return (
    <span className="inline-flex items-center gap-1">
      <span className="font-mono uppercase text-[10px] text-gray-500">{tool}</span>
      <span
        className={`rounded-md px-1.5 py-0.5 text-[10px] font-medium ${
          installed
            ? 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400'
            : 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300'
        }`}
      >
        {installed ? 'installed' : `download ${formatBytes(status.size_bytes)}`}
      </span>
      {installed && onRemove && (
        <button
          type="button"
          onClick={onRemove}
          className="text-[10px] text-gray-400 hover:text-red-500 underline-offset-2 hover:underline"
          title={`Remove ${tool} pack`}
        >
          remove
        </button>
      )}
      {!installed && onInstall && (
        <button
          type="button"
          onClick={onInstall}
          className="text-[10px] text-gray-400 hover:text-blue-600 underline-offset-2 hover:underline"
          title={`Install ${tool} pack`}
        >
          install
        </button>
      )}
    </span>
  )
}

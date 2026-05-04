import { useCallback, useEffect, useState } from 'react'
import { get, patch, post, ApiError } from '../api'

interface SystemInfo {
  platform: 'macos' | 'docker'
  picker: 'native' | 'none'
  watch_root: string | null
}

interface LanguageRow {
  code: string
  display_name: string
  built_in: boolean
  enabled: boolean
  tools: Record<string, { status: string; size_bytes?: number | null }>
}

interface LanguagesListResponse {
  languages: LanguageRow[]
}

// window.harborclerk typing lives in src/types/harborclerk.d.ts (shared
// across pages/components that touch the native bridge).

interface Props {
  onComplete: () => void
}

/**
 * First-run onboarding modal. Triggered by Layout.tsx when the logged-in
 * user's preferences.onboardingComplete is missing or false. All dismissal
 * paths (X, Skip, backdrop click, Get Started) call onComplete, which
 * writes preferences.onboardingComplete = true via updatePreferences().
 *
 * Three pages, structured so adding more (with screenshots) is additive.
 *
 * Page 1: welcome + platform-aware folder CTA.
 *   - macOS: "Pick a folder to watch" → window.harborclerk.pickFolder() →
 *     POST /api/watch/folders → advance to page 2 on success. Cancelling
 *     NSOpenPanel keeps the user on page 1.
 *   - Docker: "Read folder setup docs" → opens /docs/watched-folders-docker
 *     in a new tab. Page 2 reachable via Next regardless.
 *
 * Page 2: optional non-English language picker. English is bundled and
 * always on. Other languages download per-tool model files (~15-20 MB
 * each) — selected ones get installed AND added to enabled_languages
 * preferences in one go. Skip-friendly.
 *
 * Page 3: where-to-watch-progress (menubar status window + Observatory tab).
 */
export default function OnboardingWizard({ onComplete }: Props) {
  const [page, setPage] = useState(1)
  const [system, setSystem] = useState<SystemInfo | null>(null)
  const [error, setError] = useState('')
  const [picking, setPicking] = useState(false)
  const [languages, setLanguages] = useState<LanguageRow[]>([])
  const [selectedLangs, setSelectedLangs] = useState<Set<string>>(new Set())
  const [installingLangs, setInstallingLangs] = useState(false)

  useEffect(() => {
    get<SystemInfo>('/api/watch/system')
      .then(setSystem)
      .catch(() => {})
    get<LanguagesListResponse>('/api/languages')
      .then((data) => setLanguages(data.languages))
      .catch(() => {})
  }, [])

  const installAndContinue = useCallback(async () => {
    setError('')
    if (selectedLangs.size === 0) {
      setPage(3)
      return
    }
    setInstallingLangs(true)

    // Track which selections actually installed; partial failures must
    // not strand on-disk packs without a corresponding preference. If
    // German fails but French succeeds, we still want the operator's
    // enabled_languages to include French — otherwise the pack is on
    // disk but never used, and the operator has to discover the gap
    // themselves.
    const installed: string[] = []
    const failures: string[] = []

    // Install each selected language's tools sequentially. Sequential
    // (rather than parallel) avoids hammering the upstream host-name on
    // small networks and lets a single failure not abort the rest of
    // the batch.
    for (const code of selectedLangs) {
      const lang = languages.find((l) => l.code === code)
      if (!lang) continue
      const tools = Object.keys(lang.tools)
      if (tools.length === 0) {
        installed.push(code)
        continue
      }
      try {
        await post(`/api/languages/${code}/install`, { tools })
        installed.push(code)
      } catch (e) {
        const msg = e instanceof ApiError || e instanceof Error ? e.message : 'install failed'
        failures.push(`${code} (${msg})`)
      }
    }

    if (installed.length > 0) {
      try {
        // Backend normalises (always inserts 'en' first, dedupes, validates).
        await patch('/api/me/preferences', { enabled_languages: installed })
      } catch (e) {
        // Pref-save failed but packs are on disk. Surface so the
        // operator can flip the toggle in System Settings → Languages.
        failures.push(`preferences save (${e instanceof Error ? e.message : 'unknown'})`)
      }
    }

    setInstallingLangs(false)

    if (failures.length > 0) {
      setError(
        `Some languages failed to install: ${failures.join(', ')}. ` +
          `Successfully installed languages are still available — manage them under System Settings → Languages.`,
      )
      // Stay on this page so the user sees the error; they can click
      // Skip to advance once they've read it.
      return
    }

    setPage(3)
  }, [selectedLangs, languages])

  async function handlePickFolder() {
    if (system?.picker !== 'native' || !window.harborclerk) return
    setError('')
    setPicking(true)
    try {
      const path = await window.harborclerk.pickFolder()
      if (!path) {
        // User cancelled NSOpenPanel — stay on page 1.
        return
      }
      await post('/api/watch/folders', { path })
      setPage(2)
    } catch (e) {
      setError(e instanceof ApiError || e instanceof Error ? e.message : 'Failed to add folder')
    } finally {
      setPicking(false)
    }
  }

  function handleDockerDocs() {
    window.open('/docs/watched-folders-docker', '_blank', 'noopener,noreferrer')
  }

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="onboarding-title"
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4"
      onClick={onComplete}
    >
      <div
        className="relative w-full max-w-[520px] rounded-2xl bg-white dark:bg-[#2c2c2e] p-6 shadow-mac ring-1 ring-(--color-border)"
        onClick={(e) => e.stopPropagation()}
      >
        <button
          aria-label="Close"
          onClick={onComplete}
          className="absolute right-3 top-3 rounded-md p-1 text-gray-400 hover:bg-black/5 dark:hover:bg-white/5 hover:text-gray-700 dark:hover:text-gray-200"
        >
          <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
          </svg>
        </button>

        {page === 1 && (
          <>
            <h2 id="onboarding-title" className="mb-3 text-lg font-bold">
              Welcome to Harbor Clerk
            </h2>
            <p className="mb-4 text-sm text-(--color-text-secondary)">
              Drop documents into a folder on this Mac, Harbor Clerk indexes them locally, and you can query them from
              chat or search. Your files never leave this machine. To get started, pick a folder to watch.
            </p>
            {error && (
              <div className="mb-3 rounded-md bg-red-50 dark:bg-red-900/20 px-3 py-2 text-xs text-red-700 dark:text-red-400">
                {error}
              </div>
            )}
            <div className="mb-6">
              {system?.picker === 'native' ? (
                <button
                  onClick={handlePickFolder}
                  disabled={picking}
                  className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white shadow-xs hover:bg-blue-700 disabled:opacity-50"
                >
                  {picking ? 'Picking…' : 'Pick a folder to watch'}
                </button>
              ) : system?.picker === 'none' ? (
                <button
                  onClick={handleDockerDocs}
                  className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white shadow-xs hover:bg-blue-700"
                >
                  Read folder setup docs
                </button>
              ) : (
                // System info still loading
                <button disabled className="rounded-lg bg-gray-300 dark:bg-gray-700 px-4 py-2 text-sm text-white">
                  Loading…
                </button>
              )}
            </div>
            <div className="flex items-center justify-between">
              <button
                onClick={onComplete}
                className="text-xs text-(--color-text-secondary) underline hover:no-underline"
              >
                Skip for now
              </button>
              <button
                onClick={() => setPage(2)}
                className="rounded-lg border border-gray-300 dark:border-gray-600 px-3 py-1.5 text-xs font-medium hover:bg-black/5 dark:hover:bg-white/5"
              >
                Next →
              </button>
            </div>
          </>
        )}

        {page === 2 && (
          <>
            <h2 id="onboarding-title" className="mb-3 text-lg font-bold">
              Add languages (optional)
            </h2>
            <p className="mb-3 text-sm text-(--color-text-secondary)">
              English is built-in. If you have documents in other languages, pick them now and Harbor Clerk will
              download the OCR + entity model files (~15-20 MB per language). You can change this later in System
              Settings → Languages.
            </p>
            {error && (
              <div className="mb-3 rounded-md bg-red-50 dark:bg-red-900/20 px-3 py-2 text-xs text-red-700 dark:text-red-400">
                {error}
              </div>
            )}
            <div className="mb-5 max-h-60 overflow-y-auto rounded-md border border-(--color-border) divide-y divide-(--color-border)">
              {languages.length === 0 ? (
                <div className="px-3 py-3 text-xs text-(--color-text-secondary)">Loading languages…</div>
              ) : (
                languages
                  .filter((l) => !l.built_in)
                  .map((lang) => {
                    const checked = selectedLangs.has(lang.code)
                    const totalSize = Object.values(lang.tools).reduce((acc, t) => acc + (t.size_bytes ?? 0), 0)
                    const sizeMB = totalSize > 0 ? `~${(totalSize / (1024 * 1024)).toFixed(0)} MB` : ''
                    return (
                      <label
                        key={lang.code}
                        className="flex cursor-pointer items-center gap-2 px-3 py-2 hover:bg-black/4 dark:hover:bg-white/4 text-sm"
                      >
                        <input
                          type="checkbox"
                          checked={checked}
                          disabled={installingLangs}
                          onChange={(e) => {
                            const next = new Set(selectedLangs)
                            if (e.target.checked) next.add(lang.code)
                            else next.delete(lang.code)
                            setSelectedLangs(next)
                          }}
                          className="h-4 w-4"
                        />
                        <span className="flex-1">{lang.display_name}</span>
                        <span className="text-[11px] text-gray-400 font-mono">{lang.code}</span>
                        {sizeMB && <span className="text-[11px] text-gray-400">{sizeMB}</span>}
                      </label>
                    )
                  })
              )}
            </div>
            <div className="flex items-center justify-between">
              <button
                onClick={() => setPage(1)}
                className="text-xs text-(--color-text-secondary) underline hover:no-underline"
                disabled={installingLangs}
              >
                ← Back
              </button>
              <div className="flex items-center gap-2">
                <button
                  onClick={() => setPage(3)}
                  className="text-xs text-(--color-text-secondary) underline hover:no-underline"
                  disabled={installingLangs}
                >
                  Skip
                </button>
                <button
                  onClick={installAndContinue}
                  disabled={installingLangs}
                  className="rounded-lg bg-blue-600 px-4 py-1.5 text-sm font-medium text-white shadow-xs hover:bg-blue-700 disabled:opacity-50"
                >
                  {installingLangs
                    ? 'Installing…'
                    : selectedLangs.size === 0
                      ? 'Continue'
                      : `Install ${selectedLangs.size} & continue`}
                </button>
              </div>
            </div>
          </>
        )}

        {page === 3 && (
          <>
            <h2 id="onboarding-title" className="mb-3 text-lg font-bold">
              Track ingestion progress
            </h2>
            <p className="mb-3 text-sm text-(--color-text-secondary)">
              Documents go through a 7-stage pipeline (extract → OCR → chunk → entities → embed → summarize → finalize).
              Two places to watch:
            </p>
            <ul className="mb-6 space-y-2 text-sm text-(--color-text-secondary)">
              <li>
                <strong className="text-(--color-text-primary)">Server menubar</strong> — click the Harbor Clerk icon in
                your menu bar to see backend services running locally (Postgres, workers, the LLM if active).
              </li>
              <li>
                <strong className="text-(--color-text-primary)">Observatory tab</strong> — visit <code>/stats</code> for
                the live pipeline diagram and per-stage timing charts.
              </li>
            </ul>
            <div className="flex items-center justify-between">
              <button
                onClick={() => setPage(2)}
                className="text-xs text-(--color-text-secondary) underline hover:no-underline"
              >
                ← Back
              </button>
              <button
                onClick={onComplete}
                className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white shadow-xs hover:bg-blue-700"
              >
                Get Started
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  )
}

import { useEffect, useState } from 'react'
import { get, post, ApiError } from '../api'

interface SystemInfo {
  platform: 'macos' | 'docker'
  picker: 'native' | 'none'
  watch_root: string | null
}

declare global {
  interface Window {
    harborclerk?: { pickFolder: () => Promise<string | null> }
  }
}

interface Props {
  onComplete: () => void
}

/**
 * First-run onboarding modal. Triggered by Layout.tsx when the logged-in
 * user's preferences.onboardingComplete is missing or false. All four
 * dismissal paths (X, Skip, backdrop click, Get Started) call onComplete,
 * which writes preferences.onboardingComplete = true via updatePreferences().
 *
 * Two pages now, structured so adding pages 3 and 4 (with screenshots) is
 * additive: bump TOTAL_PAGES, render a new step in the switch.
 *
 * Page 1: welcome + platform-aware folder CTA.
 *   - macOS: "Pick a folder to watch" → window.harborclerk.pickFolder() →
 *     POST /api/watch/folders → advance to page 2 on success. Cancelling
 *     NSOpenPanel keeps the user on page 1.
 *   - Docker: "Read folder setup docs" → opens /docs/watched-folders-docker
 *     in a new tab. Page 2 reachable via Next regardless.
 *
 * Page 2: where-to-watch-progress (menubar status window + Observatory tab).
 */
export default function OnboardingWizard({ onComplete }: Props) {
  const [page, setPage] = useState(1)
  const [system, setSystem] = useState<SystemInfo | null>(null)
  const [error, setError] = useState('')
  const [picking, setPicking] = useState(false)

  useEffect(() => {
    get<SystemInfo>('/api/watch/system')
      .then(setSystem)
      .catch(() => {})
  }, [])

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
              Track ingestion progress
            </h2>
            <p className="mb-3 text-sm text-(--color-text-secondary)">
              Documents go through a 7-stage pipeline (extract → OCR → chunk → entities → embed → summarize →
              finalize). Two places to watch:
            </p>
            <ul className="mb-6 space-y-2 text-sm text-(--color-text-secondary)">
              <li>
                <strong className="text-(--color-text-primary)">Server menubar</strong> — click the Harbor Clerk icon
                in your menu bar to see backend services running locally (Postgres, workers, the LLM if active).
              </li>
              <li>
                <strong className="text-(--color-text-primary)">Observatory tab</strong> — visit{' '}
                <code>/stats</code> for the live pipeline diagram and per-stage timing charts.
              </li>
            </ul>
            <div className="flex items-center justify-between">
              <button
                onClick={() => setPage(1)}
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

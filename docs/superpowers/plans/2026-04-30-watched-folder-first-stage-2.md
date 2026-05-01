# Watched-Folder-First Stage 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Retire the direct-upload web UI, add a one-time onboarding wizard, and replace the legacy "redirect-to-upload-when-empty" hack with a persistent floating banner that has different copy for "no folders" vs "folders but no documents."

**Architecture:** Two new frontend components (`OnboardingWizard` and `CorpusEmptyBanner`) plus one hook (`useCorpusBannerState`), all rendered by `Layout.tsx` so they appear on every authenticated route. Wizard trigger is a per-user `User.preferences.onboardingComplete` boolean stored in the existing JSONB column — no schema migration. Banner polls `/api/watch/folders` and `/api/docs?limit=0` every 30 s. `/api/uploads/*` REST endpoints stay alive (locked decision: kept for non-user-facing sources). UploadPage and its supporting client-side helpers are deleted entirely.

**Tech Stack:** React 19, React Router 7, Tailwind v4, TypeScript. No new dependencies. No backend changes.

**Spec:** [`docs/superpowers/specs/2026-04-30-watched-folder-first-stage-2-design.md`](../specs/2026-04-30-watched-folder-first-stage-2-design.md)

**Frontend test infra status:** verified — the project has no Vitest / React Testing Library / Jest setup and no `*.test.*` files. Component tests are out of scope for this plan; verification relies on `npm run lint && tsc --noEmit && npm run format:check && npm run build` plus manual smoke testing. (Same pattern as Stages 1's frontend tasks.)

---

## File Structure

**New files:**
- `frontend/src/hooks/useCorpusBannerState.ts` — polling hook returning `'no-folders' | 'no-documents' | null`
- `frontend/src/components/CorpusEmptyBanner.tsx` — floating pill, two-state copy
- `frontend/src/components/OnboardingWizard.tsx` — centered modal, 2 pages

**Modified files:**
- `frontend/src/auth.tsx` — extend `User.preferences` type with `onboardingComplete?: boolean`
- `frontend/src/components/Layout.tsx` — render banner + wizard with the suppression rules
- `frontend/src/pages/HomePage.tsx` — drop the `navigate('/upload')` redirect
- `frontend/src/pages/DocumentsPage.tsx` — replace two `Link to="/upload"` with `Link to="/folders"`, update copy
- `frontend/src/components/BackButton.tsx` — remove `'/upload'` from the hardcoded list
- `frontend/src/App.tsx` — drop the `import UploadPage` and the `<Route path="/upload" …>`
- `frontend/src/api.ts` — delete the upload-session helpers + their types
- (Audit pass) `frontend/src/pages/ChatPage.tsx`, `ResearchPage.tsx`, `SearchPage.tsx`, `ExplorePage.tsx` — sweep for any remaining `/upload` links and replace with `/folders`

**Deleted files:**
- `frontend/src/pages/UploadPage.tsx`

---

## Task 1: Type bump for `User.preferences`

**Files:**
- Modify: `frontend/src/auth.tsx` (the `User` type)

The wizard reads `user.preferences.onboardingComplete` to decide whether to render. The `User` type in `auth.tsx` declares `preferences` as `{ theme?: string; page_size?: number }` — TypeScript needs the new key to compile when we set or read it later.

This task is intentionally tiny and committed alone so the type change is reviewable in isolation.

- [ ] **Step 1: Find the `User` type**

Run: `grep -n "preferences:" frontend/src/auth.tsx`
Expected: a single hit, line 8 in current code: `preferences: { theme?: string; page_size?: number }`.

- [ ] **Step 2: Extend the `preferences` shape**

Edit `frontend/src/auth.tsx`:

```diff
   preferences: { theme?: string; page_size?: number }
```
becomes
```ts
  preferences: { theme?: string; page_size?: number; onboardingComplete?: boolean }
```

- [ ] **Step 3: Verify typecheck still passes**

Run: `cd frontend && npm run type-check`
Expected: clean (no errors). The new key is optional, so no consumers break.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/auth.tsx
git commit -m "feat(auth): add onboardingComplete to User.preferences type"
```

---

## Task 2: `useCorpusBannerState` hook

**Files:**
- Create: `frontend/src/hooks/useCorpusBannerState.ts`

This hook returns `'no-folders' | 'no-documents' | null` based on polling two endpoints every 30 s. The banner component subscribes to it; nothing else does.

- [ ] **Step 1: Create the file**

Write `frontend/src/hooks/useCorpusBannerState.ts`:

```ts
import { useEffect, useState } from 'react'
import { get } from '../api'
import { useAuth } from '../auth'

export type CorpusBannerState = 'no-folders' | 'no-documents' | null

const POLL_INTERVAL_MS = 30_000

interface FolderRow {
  folder_id: string
}
interface DocsCount {
  total: number
}

/**
 * Polls /api/watch/folders and /api/docs?limit=0 every 30 s and reduces
 * the result to a banner state:
 *
 *   - folder_count === 0                          → 'no-folders'
 *   - folder_count > 0  AND doc_count === 0       → 'no-documents'
 *   - folder_count > 0  AND doc_count > 0         → null (no banner)
 *
 * Also returns null while the auth context is still resolving (no token
 * yet) so the banner doesn't flash before login completes.
 */
export function useCorpusBannerState(): CorpusBannerState {
  const { token } = useAuth()
  const [state, setState] = useState<CorpusBannerState>(null)

  useEffect(() => {
    if (!token) return

    let cancelled = false

    async function poll() {
      try {
        const [folders, docs] = await Promise.all([
          get<FolderRow[]>('/api/watch/folders'),
          get<DocsCount>('/api/docs', { limit: 0 }),
        ])
        if (cancelled) return
        if (folders.length === 0) setState('no-folders')
        else if (docs.total === 0) setState('no-documents')
        else setState(null)
      } catch {
        // Network blip — keep prior state, next tick will retry.
      }
    }

    poll()
    const id = setInterval(poll, POLL_INTERVAL_MS)
    return () => {
      cancelled = true
      clearInterval(id)
    }
  }, [token])

  return state
}
```

- [ ] **Step 2: Lint + typecheck**

Run: `cd frontend && npm run lint && npm run type-check`
Expected: 13 pre-existing lint warnings, 0 errors. tsc clean.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/hooks/useCorpusBannerState.ts
git commit -m "feat(frontend): useCorpusBannerState hook for empty-corpus banner"
```

---

## Task 3: `CorpusEmptyBanner` component

**Files:**
- Create: `frontend/src/components/CorpusEmptyBanner.tsx`

Floating pill at the top of the content area with two-state copy. Subscribes to the hook from Task 2.

- [ ] **Step 1: Create the component file**

Write `frontend/src/components/CorpusEmptyBanner.tsx`:

```tsx
import { Link } from 'react-router-dom'
import type { CorpusBannerState } from '../hooks/useCorpusBannerState'

interface Props {
  state: CorpusBannerState
}

/**
 * Floating pill banner pinned at the top of the content area. Persistent
 * (not user-dismissible); auto-disappears when its state transitions to
 * null. Suppression by route is handled by the caller (Layout.tsx).
 *
 * Copy varies with the empty-corpus state:
 *   - 'no-folders'   → user has configured zero folders
 *   - 'no-documents' → folders exist but nothing has been ingested
 *
 * Visual treatment matches LLMStatusBanner's subtle rounded-pill aesthetic
 * but pinned at the top of the content area rather than the bottom-right.
 */
export default function CorpusEmptyBanner({ state }: Props) {
  if (state === null) return null

  const message =
    state === 'no-folders'
      ? 'No watched folders yet — add one to start indexing documents.'
      : 'Your watched folders are empty — drop documents into them to begin indexing.'
  const linkLabel = state === 'no-folders' ? 'Go to Folders' : 'View folders'

  return (
    <div className="mt-2 flex justify-center px-4">
      <div
        role="status"
        className="flex items-center gap-2 rounded-full bg-amber-50 dark:bg-amber-900/20 px-4 py-1.5 text-xs text-amber-800 dark:text-amber-200 shadow-sm ring-1 ring-amber-200/60 dark:ring-amber-700/40"
      >
        <span>{message}</span>
        <Link to="/folders" className="font-medium text-amber-700 dark:text-amber-300 underline hover:no-underline">
          {linkLabel}
        </Link>
      </div>
    </div>
  )
}
```

- [ ] **Step 2: Lint + typecheck**

Run: `cd frontend && npm run lint && npm run type-check`
Expected: clean (13 pre-existing warnings, 0 errors).

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/CorpusEmptyBanner.tsx
git commit -m "feat(frontend): CorpusEmptyBanner component"
```

---

## Task 4: `OnboardingWizard` component

**Files:**
- Create: `frontend/src/components/OnboardingWizard.tsx`

Centered modal, 2 pages. Page 1: welcome + platform-aware folder CTA. Page 2: where-to-watch-progress. Top-right X, "Skip for now" on page 1, "Next →" / "← Back" / "Get Started" navigation. All four dismissal paths (X, Skip, backdrop click, Get Started) call `onComplete`.

- [ ] **Step 1: Create the component file**

Write `frontend/src/components/OnboardingWizard.tsx`:

```tsx
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
```

- [ ] **Step 2: Lint + typecheck**

Run: `cd frontend && npm run lint && npm run type-check`
Expected: clean.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/OnboardingWizard.tsx
git commit -m "feat(frontend): OnboardingWizard modal — welcome page + progress-locations page"
```

---

## Task 5: Wire wizard + banner into `Layout.tsx`

**Files:**
- Modify: `frontend/src/components/Layout.tsx`

Render the banner near the top of the content area (suppressed on `/folders`, `/admin/*`, `/preferences`). Render the wizard at the end of the layout tree (it's a fixed-position modal, so DOM order doesn't matter visually but z-index layering does).

- [ ] **Step 1: Read the current Layout.tsx structure**

Run: `grep -n "Outlet\|return\|export default\|<main\|<div" frontend/src/components/Layout.tsx | head -20`

Identify (a) the JSX root element, (b) where `<Outlet />` renders, and (c) the existing `LLMStatusBanner` placement (we'll mirror its pattern).

- [ ] **Step 2: Add imports**

In `frontend/src/components/Layout.tsx`, near the existing imports, add:

```tsx
import { useLocation } from 'react-router-dom'
import { useCorpusBannerState } from '../hooks/useCorpusBannerState'
import CorpusEmptyBanner from './CorpusEmptyBanner'
import OnboardingWizard from './OnboardingWizard'
import { patch } from '../api'
```

`useLocation` is already exported by `react-router-dom`; verify it's not already imported, and merge the named import if so. The other four are new.

- [ ] **Step 3: Add the wizard + banner render logic**

Inside the Layout component (after the existing `useAuth()` call), add:

```tsx
  const location = useLocation()
  const bannerState = useCorpusBannerState()
  const bannerSuppressedPath =
    location.pathname === '/folders' ||
    location.pathname.startsWith('/admin') ||
    location.pathname.startsWith('/preferences')
  const showBanner = bannerState !== null && !bannerSuppressedPath
  const showWizard = !!user && !user.preferences?.onboardingComplete

  async function handleOnboardingComplete() {
    try {
      await patch('/api/me/preferences', { onboardingComplete: true })
    } catch {
      // Best-effort persistence — if the patch fails the wizard still
      // closes locally; it'll re-show on next launch, which is acceptable.
    }
    // Force a reload of the user from auth so the wizard stops rendering.
    // Easiest: just optimistically mutate via updatePreferences if available.
  }
```

The simplest reliable hook to update the in-memory user is the existing `updatePreferences` from `useAuth()`. Pull it in:

```tsx
  const { user, logout, isAdmin, updatePreferences } = useAuth()
```

(replacing the existing destructure that only had the first three) and rewrite `handleOnboardingComplete`:

```tsx
  async function handleOnboardingComplete() {
    try {
      await updatePreferences({ onboardingComplete: true })
    } catch {
      // Best-effort persistence; wizard closes locally regardless.
    }
  }
```

- [ ] **Step 4: Render the banner and wizard in the JSX**

Find the existing `<Outlet />` in the Layout's return. Wrap it (or place adjacent siblings) so that:

- The banner renders BEFORE `<Outlet />` (at the top of the content area)
- The wizard renders AFTER `<Outlet />` (it's a fixed-position modal; DOM order matters only for tab order)

Concretely, insert immediately above the existing `<Outlet />`:

```tsx
{showBanner && bannerState && <CorpusEmptyBanner state={bannerState} />}
```

And immediately after `<Outlet />`:

```tsx
{showWizard && <OnboardingWizard onComplete={handleOnboardingComplete} />}
```

Both are guarded by their respective show flags, so they render or don't render purely based on state.

- [ ] **Step 5: Lint + typecheck + format**

Run: `cd frontend && npm run lint && npm run type-check && npm run format:check`
Expected: clean. If format:check fails on the modified file, run `npx prettier --write src/components/Layout.tsx` and re-check.

- [ ] **Step 6: Build to confirm Vite compiles**

Run: `cd frontend && npm run build`
Expected: `built in X ms` with the standard chunk-size warning (pre-existing). No errors.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/components/Layout.tsx
git commit -m "feat(frontend): wire OnboardingWizard + CorpusEmptyBanner into Layout"
```

---

## Task 6: Drop the HomePage redirect to `/upload`

**Files:**
- Modify: `frontend/src/pages/HomePage.tsx`

`HomePage.tsx` currently checks the doc count and redirects to `/upload` when zero. Replace with an unconditional `ChatPage` render — the new banner and wizard handle empty-corpus signaling.

- [ ] **Step 1: Replace the file body**

Overwrite `frontend/src/pages/HomePage.tsx` with:

```tsx
import ChatPage from './ChatPage'

/**
 * Home route ('/'). Renders the chat experience unconditionally; the
 * empty-corpus signaling is handled globally by Layout.tsx's
 * <CorpusEmptyBanner /> and the first-run <OnboardingWizard />.
 *
 * Stage 2 dropped the previous "redirect to /upload when doc count is
 * zero" behaviour — that route is gone, and yanking users off chat on
 * arrival was confusing once direct upload became internal-only.
 */
export default function HomePage() {
  return <ChatPage />
}
```

- [ ] **Step 2: Lint + typecheck**

Run: `cd frontend && npm run lint && npm run type-check`
Expected: clean. The unused `useState`, `useEffect`, `useNavigate`, `get` imports from the previous version are gone, so no eslint `no-unused-vars` complaints.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/pages/HomePage.tsx
git commit -m "refactor(frontend): HomePage renders chat unconditionally; banner handles empty-corpus signal"
```

---

## Task 7: Sweep `/upload` references across all pages

**Files:**
- Modify: any of `frontend/src/pages/DocumentsPage.tsx`, `ChatPage.tsx`, `ResearchPage.tsx`, `SearchPage.tsx`, `ExplorePage.tsx`, plus `frontend/src/components/BackButton.tsx`

The legacy direct-upload page had links from several places. Replace every `/upload` reference with `/folders` (or remove, if it's a stale reference to a CTA we no longer want). Also adjust the surrounding copy where it still says "Upload your first document" — the action is now "Add a watched folder."

- [ ] **Step 1: Locate every remaining `/upload` reference**

Run: `grep -rn "\"/upload\"\|'/upload'" frontend/src/`
Expected output (current as of plan-writing):
- `frontend/src/components/BackButton.tsx:5` — entry in a hardcoded list
- `frontend/src/pages/DocumentsPage.tsx:575` and `:757` — `Link to="/upload"` in empty-state CTAs

Anything else found here needs the same treatment.

- [ ] **Step 2: Update `BackButton.tsx`**

Read the surrounding array around line 5. Remove the `'/upload'` entry from the list. The list is hardcoded routes that show the back button vs. don't; with `/upload` gone, the entry has no purpose.

If the surrounding array is, say:
```ts
const ROUTES_WITH_BACK_BUTTON = [
  '/upload',
  '/docs/...',
  ...
]
```
remove just the `/upload` line, keeping commas correct.

- [ ] **Step 3: Update `DocumentsPage.tsx` empty-state CTAs**

Read `frontend/src/pages/DocumentsPage.tsx` lines 570-580 and 750-770 to see the surrounding copy. There are two empty-state branches:

For each `Link to="/upload"`:
- Change `to="/upload"` to `to="/folders"`.
- Change the surrounding copy from anything containing "Upload" to mention folders. Concrete suggested copy for the "no docs yet" branch:
  - Old: `"No documents yet. "` + link `"Upload your first document"`
  - New: `"No documents yet. "` + link `"Add a watched folder"`
- For any branch that says "Upload more documents" or similar, change to "Manage folders" linking to `/folders`.

Read each empty-state region in full before editing so the resulting copy reads naturally.

- [ ] **Step 4: Re-grep to confirm nothing's left**

Run: `grep -rn "\"/upload\"\|'/upload'\|to=\"/upload\|to='/upload" frontend/src/`
Expected: zero results.

- [ ] **Step 5: Lint + typecheck**

Run: `cd frontend && npm run lint && npm run type-check`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/components/BackButton.tsx frontend/src/pages/DocumentsPage.tsx
git commit -m "refactor(frontend): redirect remaining /upload links to /folders"
```

If other pages also had references and were modified, add their paths to the `git add` line.

---

## Task 8: Delete `UploadPage.tsx` + the route + the api.ts helpers

**Files:**
- Delete: `frontend/src/pages/UploadPage.tsx`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/api.ts`

After Task 7, nothing imports `UploadPage` except `App.tsx`. The api.ts helpers (`createUploadSession`, `getUploadSession`, `uploadFileToSession`, `confirmSession`, `cancelSession`, `getResumeInfo`, plus the types `UploadSessionInfo` and `SessionFileResult`) are imported only by `UploadPage.tsx`. We can safely delete all of it.

- [ ] **Step 1: Confirm no other consumers of UploadPage**

Run: `grep -rn "UploadPage\|from.*pages/UploadPage" frontend/src/`
Expected: only `frontend/src/App.tsx` (the import and the `<Route>`).

- [ ] **Step 2: Confirm no other consumers of the upload-session helpers**

Run: `grep -rn "createUploadSession\|getUploadSession\|uploadFileToSession\|confirmSession\|cancelSession\|getResumeInfo\|UploadSessionInfo\|SessionFileResult" frontend/src/`
Expected: only `frontend/src/api.ts` (the definitions) and `frontend/src/pages/UploadPage.tsx` (the consumer that we're about to delete). If anything else shows up, stop and resolve before continuing — DO NOT delete helpers something else uses.

- [ ] **Step 3: Update `App.tsx`**

Edit `frontend/src/App.tsx`:
- Remove the `import UploadPage from './pages/UploadPage'` line near the top.
- Remove the `<Route path="/upload" element={<UploadPage />} />` line in the routes block.

The other routes in the same block stay untouched.

- [ ] **Step 4: Delete `UploadPage.tsx`**

```bash
rm frontend/src/pages/UploadPage.tsx
```

- [ ] **Step 5: Strip the upload-session helpers from `api.ts`**

In `frontend/src/api.ts`, find each of the following exported declarations and delete them along with their leading comment / blank line:

- `export interface UploadSessionInfo { ... }`
- `export interface SessionFileResult { ... }`
- `export async function createUploadSession(...)`
- `export async function getUploadSession(...)`
- `export async function uploadFileToSession(...)`
- `export async function confirmSession(...)`
- `export async function cancelSession(...)`
- `export async function getResumeInfo(...)`

Run `grep -n "createUploadSession\|getUploadSession\|uploadFileToSession\|confirmSession\|cancelSession\|getResumeInfo\|UploadSessionInfo\|SessionFileResult" frontend/src/api.ts` first to list the exact line ranges, then delete top-down so line numbers stay stable.

- [ ] **Step 6: Lint + typecheck + build**

Run: `cd frontend && npm run lint && npm run type-check && npm run build`
Expected: clean. If anything imports something we just deleted, tsc will fail loudly with the file and symbol — fix the unexpected consumer (which Step 1/2 should have caught), or revert and re-investigate.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/App.tsx frontend/src/api.ts
git rm frontend/src/pages/UploadPage.tsx
git commit -m "refactor(frontend): retire UploadPage and its api.ts helpers"
```

---

## Task 9: Final verification + open PR

This task has no code changes — only verification commands and the PR submission.

- [ ] **Step 1: Full frontend check**

```bash
cd frontend && npm run lint && npm run type-check && npm run format:check && npm run build
```
Expected: 13 pre-existing lint warnings (0 errors), tsc clean, prettier clean, build succeeds with the standard chunk-size warning.

- [ ] **Step 2: Backend sanity (no backend changes, but confirm we didn't bleed)**

```bash
cd /Users/alex/mcp-gateway && uv run pytest tests/ 2>&1 | tail -3 && uv run ruff check . 2>&1 | tail -3
```
Expected: 384 passed (+/- new tests if any landed since), ruff clean. If anything regresses, that's a separate bug — back out whatever change broke it.

- [ ] **Step 3: Manual smoke checklist (macOS)**

After installing on top of this branch:
1. Reset preferences for one user (`UPDATE users SET preferences = '{}' WHERE email = '<test>'` if convenient, or invite a fresh admin) → log in → wizard appears.
2. Click "Pick a folder to watch" → NSOpenPanel opens → choose a tmp dir → wizard advances to page 2.
3. Click "Get Started" → wizard closes; banner is gone (folder count > 0; doc count flips > 0 once initial scan completes).
4. Reset preferences again. Log in. Click Skip on page 1 → wizard closes; banner shows `'no-folders'` state on `/`.
5. Add a folder via `/folders` → banner switches to `'no-documents'` until first ingestion.
6. Drop a PDF into the folder → banner disappears within ~30 s.
7. Visit `/upload` directly via address bar → falls through to whatever the SPA does for unmatched routes.

- [ ] **Step 4: Manual smoke checklist (Docker, optional)**

`docker compose up`, log in as a fresh user, confirm the wizard's page 1 shows "Read folder setup docs" rather than "Pick a folder to watch", and that mounting + dropping files clears the banner the same way.

- [ ] **Step 5: Open PR**

```bash
git push -u origin <branch-name>
gh pr create --title "feat: watched-folder-first stage 2 — retire UploadPage + onboarding wizard + corpus banner" --body-file <(cat <<'EOF'
## Summary

Stage 2 of the watched-folder-first refactor (spec at `docs/superpowers/specs/2026-04-30-watched-folder-first-stage-2-design.md`).

- New `<OnboardingWizard />` modal: 2 pages (welcome + folder CTA, then progress-locations). Triggered once per user via `User.preferences.onboardingComplete`. Skip / X / backdrop click / Get Started all set the flag.
- New `<CorpusEmptyBanner />` floating pill rendered by Layout.tsx with two-state copy: `'no-folders'` (zero folders configured) vs `'no-documents'` (folders exist but nothing ingested). Suppressed on `/folders`, `/admin/*`, `/preferences`. 30 s polling on `/api/watch/folders` + `/api/docs?limit=0`.
- `HomePage` redirect to `/upload` deleted — chat page renders unconditionally; banner handles the empty-corpus signal globally.
- `UploadPage.tsx` deleted, `/upload` route removed from `App.tsx`, upload-session helpers removed from `api.ts`, BackButton entry removed.
- All `/upload` Link references in DocumentsPage swept to `/folders` with appropriately re-worded copy.
- No backend changes. `/api/uploads/*` REST endpoints stay alive for non-user-facing sources (locked decision).

## Test plan
- [x] `npm run lint` (13 pre-existing warnings, 0 errors), `tsc --noEmit`, `format:check`, `build` — all clean
- [x] `uv run pytest tests/` — clean (no backend changes, smoke check)
- [ ] Manual smoke (macOS): wizard fires once on first login, all four dismissal paths set the flag, banner copy switches correctly across the no-folders → no-documents → resolved progression
- [ ] Manual smoke (Docker): wizard shows the docs-link CTA on page 1, banner behaviour identical otherwise
- [ ] `/upload` no longer reachable

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)
```

- [ ] **Step 6: Watch CI, fix anything that flags, merge**

```bash
gh pr checks --watch
gh pr merge --squash --delete-branch
```

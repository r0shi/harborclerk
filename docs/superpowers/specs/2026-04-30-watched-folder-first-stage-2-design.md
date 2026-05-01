# Watched-Folder-First Refactor — Stage 2 Design

## Context

Stage 1 ([2026-04-30 design](2026-04-30-watched-folder-first-stage-1-design.md), shipped as PR #245 + follow-ups) brought watched folders to Docker, replaced the macOS Swift FSEvents watcher with a cross-platform Python `watchdog` daemon, and graduated folder management UI into a top-level `/folders` web tab. Direct upload via `/upload` is still mounted — that route was unlinked from the nav in Stage 1 but the page itself remains.

Stage 2 finishes the refactor: kill the direct-upload UI entirely, commit fully to the watched-folder model as the only user-facing ingest path, and add a one-time onboarding wizard that explains the new model to first-time users. Direct-upload REST endpoints (`/api/uploads/*`) stay alive for non-user-facing sources (future email ingestion, automation, tests).

## Goals

- Remove the direct-upload UI and all references to it from the SPA.
- Add a first-run onboarding wizard so a new user lands somewhere that explains "you watch folders, files get indexed."
- Replace the legacy "redirect to upload when corpus is empty" hack with a persistent floating banner that nudges users without blocking them.
- Different banner copy for "no folders configured" vs "folders configured but no documents yet" so the user knows whether the gap is in their setup or just because they haven't dropped files in yet.

## Non-goals

- Decommissioning `/api/uploads/*` REST endpoints (locked: stays for email/automation/tests).
- Removing the upload-session `Upload` ORM model or `uploads` table (legacy direct-uploaded doc audit rows still live there).
- Migrating legacy direct-uploaded docs to a "Legacy" folder or re-keying them to anything (locked: they stay queryable, become unfoldered).
- Folder filter on chat/search (locked: folds into the future Projects/Collections work, task #97).
- Stage 3 (flatten document model — separate spec).

## Architecture

Two new frontend components plus targeted edits:

### 1. `<OnboardingWizard />`

Modal dialog rendered conditionally by `Layout.tsx` (which already wraps every authenticated route). Triggered when the logged-in user's `User.preferences.onboardingComplete` is missing or `false`.

**Storage:** Single boolean flag in the existing `User.preferences` JSONB column. No schema migration. No new table. The existing `updatePreferences()` helper in `auth.tsx` PATCHes `/api/me/preferences`; we reuse it to flip `onboardingComplete: true` on any of the four dismissal paths (X, Skip, Get-Started, backdrop click). Backdrop click DOES count as dismissal — matches typical macOS modal behaviour and avoids the user-frustration case where they think the wizard didn't go away. If the user wants to come back, the flag is per-user-resettable from the future "Show welcome tour again" affordance (not in scope here).

**Per-user, not per-install.** A second admin invited later sees the wizard once, on their first login. The first admin's flag doesn't bleed across users.

**Shape:**
- Centered dialog, fixed width ~520px, light/dark backdrop with click-to-dismiss.
- 2 pages now, structured so adding pages 3 and 4 later (with screenshots) is purely additive.

**Page 1 — Welcome + first folder:**
- Title: "Welcome to Harbor Clerk"
- ~3 sentences explaining the local-indexing model. Suggested copy:
  > Drop documents into a folder on this Mac, Harbor Clerk indexes them locally, and you can query them from chat or search. Your files never leave this machine. To get started, pick a folder to watch.
- Primary CTA varies by platform (`system.picker`):
  - **macOS** (`picker === 'native'`): button "Pick a folder to watch" → fires `window.harborclerk.pickFolder()` → on success, POST `/api/watch/folders` with the chosen path → modal advances to page 2 automatically. **If `pickFolder()` returns null** (user cancelled NSOpenPanel): stay on page 1, do not advance, do not update the flag. The user can retry the button or click Next / Skip / X.
  - **Docker** (`picker === 'none'`): button "Read folder setup docs" → opens `/docs/watched-folders-docker` in a new tab. Page 2 is reachable via Next regardless.
- Bottom-left: "Skip for now" link (sets `onboardingComplete: true`, closes modal).
- Bottom-right: "Next →" button (advances to page 2 without picking).
- Top-right: X (sets `onboardingComplete: true`, closes modal).

**Page 2 — Where to watch progress:**
- Title: "Track ingestion progress"
- ~2 sentences explaining the 7-stage pipeline (extract → OCR → chunk → entities → embed → summarize → finalize) and the two places to watch:
  > **Server menubar** — click the Harbor Clerk icon in your menu bar to see backend services running locally (Postgres, workers, the LLM if active).
  > **Observatory tab** — visit `/stats` for the live pipeline diagram and per-stage timing charts.
- Bottom-left: "← Back" (returns to page 1, doesn't change the flag).
- Bottom-right: "Get Started" button (sets `onboardingComplete: true`, closes modal). Text is `"Get Started"` rather than `"Done"` to match the welcome framing.
- Top-right: X (same semantics as page 1).

**Re-entry:** none in this stage. Once `onboardingComplete: true`, the modal never re-renders for that user. If a future task wants to add "Show welcome tour again," it can ship a button somewhere in System Settings that calls `updatePreferences({onboardingComplete: false})`.

### 2. `<CorpusEmptyBanner />`

Floating pill at the top of the content area. Rendered by `Layout.tsx` underneath the nav, suppressed only when the active route is `/folders` (the destination the banner links to). Persistent — auto-disappears when its trigger condition no longer holds. Not user-dismissible.

**State machine** — driven by a small hook `useCorpusBannerState()`:

| Condition | Banner state | Copy | Link |
|---|---|---|---|
| `folder_count === 0` | `'no-folders'` | "No watched folders yet — **Go to Folders** to add one and start indexing documents." | `/folders` |
| `folder_count > 0 AND doc_count === 0` | `'no-documents'` | "Your watched folders are empty — drop documents into them to begin indexing. **View folders**" | `/folders` |
| `folder_count > 0 AND doc_count > 0` | `null` | (no banner) | — |

Once a `Document` row exists (regardless of pipeline stage), the banner stays hidden — the page's natural in-progress UX takes over. The `'no-documents'` state only shows when the watcher has registered the folder but nothing's been dropped in yet, or in a small window before the watcher emits its first `created` event.

**Data source:** the hook polls two endpoints every 30 seconds:
- `/api/watch/folders` — returns the array of `WatchedFolder` rows; we read `length` for `folder_count`. (Disabled or unmounted folders still count toward `folder_count > 0` for this banner — the goal is to nag users who've literally configured nothing, not to second-guess their disabled-folder choices.)
- `/api/docs?limit=0` — returns `{total, …}`; we read `total` for `doc_count`.

30-second polling is enough to dismiss the banner promptly after a folder add or first-doc ingestion without hammering the API. SSE-based reactivity is overkill for a banner; the user-perceived latency for "I just added a folder, the banner should disappear" is acceptable in the seconds range.

**Visual treatment:** rounded pill, subtle bg matching design tokens, similar in weight to the existing `LLMStatusBanner` but pinned at the top of the content area rather than the bottom-right. Single line of text with the link inline.

### 3. `Layout.tsx` integration

Layout wraps every authenticated route. We render the wizard and banner here so they show on all pages without per-page wiring:

```tsx
function Layout() {
  const { user } = useAuth()
  const showWizard = user && !user.preferences?.onboardingComplete
  const location = useLocation()
  const bannerState = useCorpusBannerState()
  // Suppress the banner on /folders (the link target) and on
  // configuration screens where the corpus state is irrelevant chrome.
  const bannerSuppressedPath =
    location.pathname === '/folders' ||
    location.pathname.startsWith('/admin') ||
    location.pathname.startsWith('/preferences')
  const showBanner = bannerState !== null && !bannerSuppressedPath

  return (
    <div>
      <Nav />
      {showBanner && <CorpusEmptyBanner state={bannerState} />}
      <Outlet />
      {showWizard && <OnboardingWizard onComplete={...} />}
    </div>
  )
}
```

(Pseudocode — actual implementation will follow the existing Layout.tsx structure.)

**Wizard-vs-banner z-order:** when both want to render (rare — only on first launch where `onboardingComplete` is false AND the user has zero folders), the wizard's modal backdrop covers the banner visually. The banner stays mounted underneath; once the wizard closes it becomes visible. No special suppression logic — the modal's stacking context handles it.

## Files

**Deleted:**
- `frontend/src/pages/UploadPage.tsx` — the page itself.
- `frontend/src/api.ts` upload-session helpers: `createUploadSession`, `getUploadSession`, `uploadFileToSession`, `confirmSession`, `cancelSession`, `getResumeInfo`, plus the `UploadSessionInfo` and `SessionFileResult` types. Nothing imports these after this change.

**New:**
- `frontend/src/components/OnboardingWizard.tsx` — the modal dialog with page 1 / page 2.
- `frontend/src/components/CorpusEmptyBanner.tsx` — the floating pill.
- `frontend/src/hooks/useCorpusBannerState.ts` — polls folder + doc counts, returns `'no-folders' | 'no-documents' | null`.

**Modified:**
- `frontend/src/App.tsx` — drop the `import UploadPage` line and the `<Route path="/upload" element={<UploadPage />} />` line.
- `frontend/src/components/Layout.tsx` — render `<CorpusEmptyBanner />` and `<OnboardingWizard />` per the snippet above.
- `frontend/src/pages/HomePage.tsx` — drop the `navigate('/upload')` redirect entirely. HomePage renders the chat page unconditionally; the banner and wizard handle the empty-corpus signaling.
- `frontend/src/pages/DocumentsPage.tsx` — replace both `Link to="/upload"` instances (lines ~575, ~757) with `Link to="/folders"`. Update the in-page empty-state copy: when the page is empty AND folder_count > 0, say "Documents will appear here as your watched folders are scanned." with a link to `/folders` for users who want to add more. The banner above already handles the `folder_count === 0` case.
- `frontend/src/components/BackButton.tsx` — remove `'/upload'` from the hardcoded list.
- `frontend/src/auth.tsx` — extend the `User.preferences` type to include `onboardingComplete?: boolean`. The existing `updatePreferences()` API already accepts arbitrary partial preferences, so no functional change beyond the type.
- `frontend/src/pages/ChatPage.tsx`, `frontend/src/pages/ResearchPage.tsx`, `frontend/src/pages/SearchPage.tsx`, `frontend/src/pages/ExplorePage.tsx` — audit each for any "No documents" / "Upload your first document" copy that links to `/upload`. Replace with `/folders`. Most won't need changes (they're search-style views), but worth a sweep.

**No backend changes.** All routes from Stage 1 stay; `/api/uploads/*` stays mounted but unused by the SPA.

## Data model

No migration. `User.preferences` is a JSONB column already used for `theme`, `page_size`. Adding one boolean key is type-safe at the JSON layer. The frontend type (`User.preferences.onboardingComplete?: boolean`) extends what's already defined in `auth.tsx`.

## API surface

No new endpoints. Existing endpoints used:

- `GET /api/watch/folders` — already returns the folder array; banner reads `length`.
- `GET /api/docs?limit=0` — already returns `{total, …}`; banner reads `total`.
- `GET /api/watch/system` — already used by FoldersPage and the wizard to switch between macOS picker and Docker docs link.
- `POST /api/watch/folders` — already used by FoldersPage; the wizard calls it with the path returned by `pickFolder()`.
- `PATCH /api/users/me/preferences` (or wherever `updatePreferences()` posts) — already wired; we send `{onboardingComplete: true}`.

## Frontend behaviour by surface

| Page | Folder=0 | Folder>0, Doc=0 | Folder>0, Doc>0 |
|---|---|---|---|
| `/` (chat) | Banner top, wizard if first-run, chat empty-state body | Banner top, chat empty-state body | No banner, normal chat |
| `/c/:id` | Banner top, normal chat | Banner top, normal chat | No banner, normal chat |
| `/docs` | Banner top, "no documents yet" body | Banner top, "documents will appear as folders scan" body | No banner, doc list |
| `/search` | Banner top, search shell | Banner top, search shell | No banner, search shell |
| `/explore` | Banner top, explore shell | Banner top, explore shell | No banner, explore shell |
| `/research/...` | Banner top, normal research | Banner top, normal research | No banner, normal research |
| `/folders` | (existing in-page empty state, no banner) | (folder list, no banner) | (folder list, no banner) |
| `/stats` | Banner top, observatory | Banner top, observatory | No banner, observatory |
| `/admin/*` | (banner suppressed — admin views) | (banner suppressed) | (banner suppressed) |

The admin-views row is a small judgment call: the banner is about the corpus and is meaningless on Users / API Keys / Models pages. Suppressing on `/admin*` and `/preferences` keeps the chrome quiet on configuration screens.

## Testing

- **Component tests** (Vitest + React Testing Library if present, otherwise defer):
  - `<OnboardingWizard />` renders page 1 by default; Skip / X both call `onComplete` with no folder picked; Next advances to page 2; "Get Started" calls `onComplete`.
  - `<OnboardingWizard />` page-1 CTA renders "Pick a folder to watch" when `picker === 'native'` and "Read folder setup docs" when `picker === 'none'`.
  - `<CorpusEmptyBanner />` renders correct copy for each state.
  - `useCorpusBannerState` returns the expected sentinel for each `(folder_count, doc_count)` permutation.
- **Lint / typecheck / build** — Stage 2 must keep `npm run lint`, `tsc --noEmit`, `npm run format:check`, `npm run build` clean. No new ESLint warnings.
- **Manual smoke (macOS native install):**
  1. Reset preferences (or invite a new admin) so `onboardingComplete` is unset → log in → wizard appears.
  2. Click "Pick a folder to watch" → NSOpenPanel → choose dir → wizard advances to page 2.
  3. "Get Started" → wizard closes, banner is hidden because folder count > 0 and (eventually) doc count > 0.
  4. Skip on page 1 → wizard closes, banner shows `'no-folders'` state on `/`.
  5. Add a folder via `/folders` → banner switches to `'no-documents'` until first ingestion.
  6. Drop a PDF → banner disappears (within 30 s).
  7. Visit `/upload` directly via address bar → 404 / route-not-matched.
- **Manual smoke (Docker compose):**
  1. Fresh user → wizard appears with "Read folder setup docs" CTA.
  2. Skip → banner shows `'no-folders'` (no auto-discovered folders yet).
  3. Mount a dir under `WATCH_ROOT` per the docs → wait one rescan cycle → banner switches to `'no-documents'`.
  4. Drop a file → banner disappears.

## Verification

End-to-end manual checks before merging:
- Wizard appears exactly once per user, dismissible four ways, all four set the flag.
- Banner appears on every authenticated page except `/folders`, with correct copy per state, switches automatically as the underlying counts change (within ~30 s).
- `/upload` route returns the SPA's not-found / fallback (whatever React Router does when no route matches) — the page is gone.
- No ESLint or TS errors introduced. No backend tests need to change.

## Rollout

Single PR. Schema-free (no migration). Frontend-only diff plus the type bump for `User.preferences`. Same `make apps` install path as Stage 1 — users get the new chrome on the next launch.

## Follow-ups not addressed here

- **"Show welcome tour again" affordance.** Not needed yet. If a future user requests it, add a button in System Settings → Preferences that resets the flag.
- **Wizard pages 3 and 4 with screenshots.** The 2-page structure is designed to extend additively. When we have stable screenshots of the menubar status window and the Observatory page, drop them onto a page 3 (between page 1 and 2) and bump the page count.
- **Decommissioning `/api/uploads/*`.** Locked out of scope here; revisit when email ingestion ships and we know whether anything else depends on the endpoints.
- **Removing the `Upload` ORM model + `uploads` table.** Same — defer until the legacy direct-uploaded audit data is provably unused.
- **Stage 3** — flatten document model (eliminate `document_versions`). Independent of Stage 2; can ship in any order.

# UI Tab Redesign & Documents Enhancements Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Reorganize the tab bar (new order, connected-tab styling, System Settings hub), enhance the Documents page (pagination, live filter, download), and add a download endpoint.

**Architecture:** Frontend-only changes for tabs/routing/layout plus one new backend endpoint. System Settings becomes a hub page that links to sub-routes. Chat becomes the default "/" route (with empty-store redirect to /upload). Documents page gets client-side pagination and filtering over the already-fetched doc list.

**Tech Stack:** React 19, React Router 7, Tailwind CSS 3.4, FastAPI, SQLAlchemy async

---

### Task 1: Reroute — Move Chat to `/` and Upload to `/upload`

**Files:**
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/pages/ChatPage.tsx` (internal navigation references)

**Step 1: Update App.tsx routes**

Replace the route definitions inside the `<Layout />` route:

```tsx
// Old
<Route path="/" element={<UploadPage />} />
<Route path="/chat" element={<ChatPage />} />
<Route path="/chat/:conversationId" element={<ChatPage />} />

// New
<Route path="/" element={<ChatPage />} />
<Route path="/:conversationId" element={<ChatPage />} />
<Route path="/upload" element={<UploadPage />} />
```

Note: The `/chat` path should still work — add a redirect:
```tsx
import { Navigate } from 'react-router-dom'
// Add inside Layout routes:
<Route path="/chat" element={<Navigate to="/" replace />} />
<Route path="/chat/:conversationId" element={<Navigate to="/:conversationId" replace />} />
```

Actually, the `/:conversationId` wildcard route will be greedy and match other paths. Instead, keep chat conversations under a prefix:

```tsx
<Route path="/" element={<ChatPage />} />
<Route path="/c/:conversationId" element={<ChatPage />} />
<Route path="/upload" element={<UploadPage />} />
// Redirects for old paths:
<Route path="/chat" element={<Navigate to="/" replace />} />
<Route path="/chat/:conversationId" element={<ChatRedirect />} />
```

Create a small redirect component at the top of App.tsx:
```tsx
function ChatRedirect() {
  const { conversationId } = useParams()
  return <Navigate to={`/c/${conversationId}`} replace />
}
```

**Step 2: Update ChatPage.tsx navigation references**

In `ChatPage.tsx`, replace all occurrences of:
- `/chat/${...}` → `/c/${...}`
- `/chat` → `/`
- `navigate('/chat')` → `navigate('/')`
- `navigate('/chat/${activeConvId}',...)` → `navigate(`/c/${activeConvId}`,...)`

**Step 3: Update links pointing to Upload**

Search all frontend files for `to="/"` or `to={"/"}` that refer to the Upload page and change them to `to="/upload"`. Key locations:
- `frontend/src/pages/DocumentsPage.tsx` — the "Upload" button link (line 90) and empty state link (line 99)

**Step 4: Verify build**

Run: `cd frontend && npm run build`
Expected: Build succeeds with no errors

**Step 5: Commit**

```bash
git add frontend/src/App.tsx frontend/src/pages/ChatPage.tsx frontend/src/pages/DocumentsPage.tsx
git commit -m "feat: reroute chat to / and upload to /upload"
```

---

### Task 2: Update Tab Bar — New Order, Labels, and Connected-Tab Styling

**Files:**
- Modify: `frontend/src/components/Layout.tsx`
- Modify: `frontend/src/components/BackButton.tsx`

**Step 1: Replace the linkClass function and nav links in Layout.tsx**

Replace the entire `linkClass` function (lines 7-12) with a new tab styling approach. The active tab should visually connect to the content area below:

```tsx
function TabLink({ to, end, children }: { to: string; end?: boolean; children: React.ReactNode }) {
  return (
    <NavLink
      to={to}
      end={end}
      className={({ isActive }) =>
        `relative px-3 py-1.5 text-[13px] font-medium transition-colors ${
          isActive
            ? 'text-[var(--color-text-primary)]'
            : 'text-[var(--color-text-secondary)] hover:text-[var(--color-text-primary)]'
        }`
      }
    >
      {({ isActive }) => (
        <>
          {children}
          {isActive && (
            <span className="absolute inset-x-0 -bottom-[7px] h-[2px] bg-[var(--color-accent)] rounded-full" />
          )}
        </>
      )}
    </NavLink>
  )
}
```

This uses an accent-colored bottom indicator on the active tab (like a proper tab bar). The navbar bottom border stays, and the active indicator overlaps it.

Replace the nav links section (lines 49-76) with the new order:

```tsx
<TabLink to="/" end>Harbor Clerk</TabLink>
<TabLink to="/upload">Upload</TabLink>
<TabLink to="/docs">Documents</TabLink>
<TabLink to="/search">Raw Search</TabLink>
{isAdmin && (
  <TabLink to="/admin">System Settings</TabLink>
)}
```

Remove the old `linkClass` function since `TabLink` replaces it.

**Step 2: Update BackButton.tsx top-level paths**

Update the `TOP_LEVEL` set in `BackButton.tsx` to match new routes:

```tsx
const TOP_LEVEL = new Set([
  '/',
  '/upload',
  '/docs',
  '/search',
  '/admin',
  '/preferences',
  '/login',
  '/setup',
])
```

Update `isTopLevel` to handle admin sub-routes properly — admin sub-pages should NOT be top-level (they should show a back button to `/admin`):

```tsx
function isTopLevel(pathname: string): boolean {
  if (TOP_LEVEL.has(pathname)) return true
  // Chat conversation paths are top-level (no back button needed)
  if (pathname.startsWith('/c/')) return true
  return false
}
```

**Step 3: Verify build**

Run: `cd frontend && npm run build`

**Step 4: Commit**

```bash
git add frontend/src/components/Layout.tsx frontend/src/components/BackButton.tsx
git commit -m "feat: new tab order, labels, and connected-tab styling"
```

---

### Task 3: System Settings Hub Page

**Files:**
- Create: `frontend/src/pages/SystemSettingsPage.tsx`
- Modify: `frontend/src/App.tsx` (add route)

**Step 1: Create SystemSettingsPage.tsx**

```tsx
import { Link } from 'react-router-dom'
import { useAuth } from '../auth'

const ITEMS = [
  { to: '/admin/users', label: 'Users', description: 'Manage user accounts and roles' },
  { to: '/admin/keys', label: 'API Keys', description: 'Create and revoke API keys' },
  { to: '/admin/system/status', label: 'System Status', description: 'Health checks and statistics' },
  { to: '/admin/system/maintenance', label: 'System Maintenance', description: 'Purge, reaper, and cleanup' },
  { to: '/admin/models', label: 'Models', description: 'Download and manage LLM models' },
]

export default function SystemSettingsPage() {
  return (
    <div>
      <h1 className="mb-4 text-xl font-bold">System Settings</h1>
      <div className="rounded-xl bg-white dark:bg-[#2c2c2e] shadow-mac overflow-hidden divide-y divide-[var(--color-border)]">
        {ITEMS.map((item) => (
          <Link
            key={item.to}
            to={item.to}
            className="flex items-center justify-between px-4 py-3.5 hover:bg-black/[0.02] dark:hover:bg-white/[0.02] transition-colors"
          >
            <div>
              <div className="text-[14px] font-medium text-[var(--color-text-primary)]">
                {item.label}
              </div>
              <div className="text-[12px] text-[var(--color-text-secondary)] mt-0.5">
                {item.description}
              </div>
            </div>
            <svg className="h-4 w-4 text-gray-300 dark:text-gray-600 flex-shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M8.25 4.5l7.5 7.5-7.5 7.5" />
            </svg>
          </Link>
        ))}
      </div>
    </div>
  )
}
```

**Step 2: Add route in App.tsx**

Import `SystemSettingsPage` and add the route inside the `<AdminRoute>` block:

```tsx
import SystemSettingsPage from './pages/SystemSettingsPage'

// Inside AdminRoute:
<Route path="/admin" element={<SystemSettingsPage />} />
```

**Step 3: Verify build**

Run: `cd frontend && npm run build`

**Step 4: Commit**

```bash
git add frontend/src/pages/SystemSettingsPage.tsx frontend/src/App.tsx
git commit -m "feat: add System Settings hub page"
```

---

### Task 4: Split SystemPage into Status and Maintenance

**Files:**
- Create: `frontend/src/pages/SystemStatusPage.tsx`
- Create: `frontend/src/pages/SystemMaintenancePage.tsx`
- Modify: `frontend/src/App.tsx` (update routes)
- Delete content from: `frontend/src/pages/SystemPage.tsx` (keep as re-export or remove)

**Step 1: Create SystemStatusPage.tsx**

Extract the health checks and stats display from SystemPage (lines 42-156 and the HealthCard component lines 183-235). The page should contain:
- `loadHealth()` and `loadStats()` fetching
- Health cards grid
- Overall status badge
- A "Refresh" button

Keep the interfaces (`HealthCheck`, `ServiceStats`, `StatsResponse`), `STAT_LABELS`, `formatStatValue`, and `HealthCard` in this file.

```tsx
import { useEffect, useState } from 'react'
import { get } from '../api'

// Copy interfaces, STAT_LABELS, formatStatValue, HealthCard from SystemPage.tsx

export default function SystemStatusPage() {
  const [health, setHealth] = useState<HealthCheck | null>(null)
  const [stats, setStats] = useState<StatsResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [statsLoading, setStatsLoading] = useState(true)
  const [error, setError] = useState('')

  async function loadHealth() { /* same as SystemPage */ }
  async function loadStats() { /* same as SystemPage */ }

  useEffect(() => { loadHealth(); loadStats() }, [])

  if (loading) return <div className="text-gray-500 dark:text-gray-400">Loading...</div>

  return (
    <div>
      <h1 className="mb-4 text-xl font-bold">System Status</h1>
      {error && (/* error banner */)}
      <h2 className="mb-3 text-lg font-semibold">Health Checks</h2>
      {/* health cards grid — same as current SystemPage lines 123-141 */}
      {/* overall status badge — same as current SystemPage lines 144-156 */}
      <div className="mt-4">
        <button onClick={() => { loadHealth(); loadStats() }}
          className="rounded-lg bg-[var(--color-bg-tertiary)] px-4 py-2 text-sm font-medium text-gray-700 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-600">
          Refresh
        </button>
      </div>
    </div>
  )
}
```

**Step 2: Create SystemMaintenancePage.tsx**

Extract the maintenance actions (purge, reaper) from SystemPage:

```tsx
import { useState } from 'react'
import { post } from '../api'

export default function SystemMaintenancePage() {
  const [error, setError] = useState('')
  const [actionResult, setActionResult] = useState('')

  async function handlePurge() { /* same as SystemPage */ }
  async function handleReaper() { /* same as SystemPage */ }

  return (
    <div>
      <h1 className="mb-4 text-xl font-bold">System Maintenance</h1>
      {error && (/* error banner */)}
      {actionResult && (/* success banner */)}
      <div className="flex space-x-3">
        {/* Purge and Reaper buttons — same as current SystemPage lines 160-177 */}
      </div>
    </div>
  )
}
```

**Step 3: Update App.tsx routes**

Replace the old System route with the two new ones:

```tsx
import SystemStatusPage from './pages/SystemStatusPage'
import SystemMaintenancePage from './pages/SystemMaintenancePage'

// Remove: <Route path="/admin/system" element={<SystemPage />} />
// Add:
<Route path="/admin/system/status" element={<SystemStatusPage />} />
<Route path="/admin/system/maintenance" element={<SystemMaintenancePage />} />
// Add redirect for old path:
<Route path="/admin/system" element={<Navigate to="/admin/system/status" replace />} />
```

Remove the `SystemPage` import from App.tsx.

**Step 4: Delete or gut SystemPage.tsx**

Delete `frontend/src/pages/SystemPage.tsx` since it's fully replaced.

**Step 5: Verify build**

Run: `cd frontend && npm run build`

**Step 6: Commit**

```bash
git add frontend/src/pages/SystemStatusPage.tsx frontend/src/pages/SystemMaintenancePage.tsx frontend/src/App.tsx
git rm frontend/src/pages/SystemPage.tsx
git commit -m "feat: split System into Status and Maintenance pages"
```

---

### Task 5: Empty Store Redirect

**Files:**
- Create: `frontend/src/pages/HomePage.tsx`
- Modify: `frontend/src/App.tsx`

**Step 1: Create HomePage.tsx wrapper**

This component checks the doc count and renders ChatPage or redirects to /upload:

```tsx
import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { get } from '../api'
import ChatPage from './ChatPage'

export default function HomePage() {
  const navigate = useNavigate()
  const [ready, setReady] = useState(false)

  useEffect(() => {
    let cancelled = false
    get<{ doc_id: string }[]>('/api/docs')
      .then((docs) => {
        if (cancelled) return
        if (docs.length === 0) {
          navigate('/upload', { replace: true })
        } else {
          setReady(true)
        }
      })
      .catch(() => {
        if (!cancelled) setReady(true) // on error, show chat anyway
      })
    return () => { cancelled = true }
  }, [navigate])

  if (!ready) return null
  return <ChatPage />
}
```

**Step 2: Update App.tsx**

Replace the `"/"` route:

```tsx
import HomePage from './pages/HomePage'

// Old: <Route path="/" element={<ChatPage />} />
// New: <Route path="/" element={<HomePage />} />
```

Keep the `/c/:conversationId` route pointing directly at `ChatPage` (no redirect needed — if user has a conversation URL they should go straight there).

**Step 3: Verify build**

Run: `cd frontend && npm run build`

**Step 4: Commit**

```bash
git add frontend/src/pages/HomePage.tsx frontend/src/App.tsx
git commit -m "feat: redirect to upload when document store is empty"
```

---

### Task 6: Download Endpoint (Backend)

**Files:**
- Modify: `src/harbor_clerk/api/routes/documents.py`

**Step 1: Add the download endpoint**

Add after the existing `get_document_content` endpoint (after line 228):

```python
from fastapi.responses import Response

@router.get("/docs/{doc_id}/download")
async def download_document(
    doc_id: uuid.UUID,
    principal: Principal = Depends(require_read_access),
    session: AsyncSession = Depends(get_session),
):
    """Download the original file for the latest version of a document."""
    result = await session.execute(
        select(Document)
        .where(Document.doc_id == doc_id, Document.status == "active")
        .options(selectinload(Document.versions))
    )
    doc = result.scalar_one_or_none()
    if doc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Document not found"
        )

    version_id = doc.latest_version_id
    if version_id is None and doc.versions:
        version_id = doc.versions[-1].version_id
    if version_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No versions available"
        )

    ver_result = await session.execute(
        select(DocumentVersion).where(DocumentVersion.version_id == version_id)
    )
    version = ver_result.scalar_one()

    import posixpath
    from harbor_clerk.storage import get_storage

    storage = get_storage()
    obj = storage.get_object(version.original_bucket, version.original_object_key)
    filename = posixpath.basename(version.original_object_key)
    content_type = version.mime_type or "application/octet-stream"

    return Response(
        content=obj.data,
        media_type=content_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )
```

Note: `StorageResponse` has a `.data` attribute (bytes). Import `Response` from fastapi.responses at the top of the file.

**Step 2: Verify the server starts**

Run: `cd /Users/alex/mcp-gateway && uv run harbor-clerk-api &` (or check with existing dev process)

**Step 3: Commit**

```bash
git add src/harbor_clerk/api/routes/documents.py
git commit -m "feat: add document download endpoint"
```

---

### Task 7: Documents Page — Pagination, Filter, Download Button

**Files:**
- Modify: `frontend/src/pages/DocumentsPage.tsx`

**Step 1: Add pagination, filter, and download to DocumentsPage**

The full rewrite of `DocumentsPage.tsx`:

1. **Import `useAuth`** (already imported) to get `user.preferences.page_size`
2. **Add state:** `filter` (string), `currentPage` (number)
3. **Compute filtered docs:** `docs.filter(d => d.title.toLowerCase().includes(filter) || d.canonical_filename?.toLowerCase().includes(filter))`
4. **Compute paginated slice:** `filteredDocs.slice((currentPage - 1) * pageSize, currentPage * pageSize)`
5. **Add filter input** above the table:

```tsx
<input
  type="text"
  placeholder="Filter by filename..."
  value={filter}
  onChange={(e) => { setFilter(e.target.value); setCurrentPage(1) }}
  className="w-64 rounded-lg border border-[var(--color-border)] bg-white dark:bg-[#2c2c2e] px-3 py-1.5 text-sm text-[var(--color-text-primary)] placeholder-[var(--color-text-secondary)] focus:outline-none focus:ring-2 focus:ring-[var(--color-accent)]/30"
/>
```

6. **Add download button** in each row — a small icon that opens `/api/docs/${doc.doc_id}/download` in a new tab or uses an `<a>` tag:

```tsx
<a
  href={`/api/docs/${doc.doc_id}/download`}
  className="rounded p-1 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"
  title="Download original"
  onClick={(e) => e.stopPropagation()}
>
  <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
    <path strokeLinecap="round" strokeLinejoin="round" d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5M16.5 12L12 16.5m0 0L7.5 12m4.5 4.5V3" />
  </svg>
</a>
```

Note: The download link needs the auth token. Since the endpoint uses `require_read_access` which checks the Bearer token, a simple `<a href>` won't work (no auth header). Instead, use a click handler that fetches with auth and triggers a download:

```tsx
async function handleDownload(docId: string, filename?: string) {
  try {
    const response = await fetch(`/api/docs/${docId}/download`, {
      headers: { 'Authorization': `Bearer ${token}` },
    })
    if (!response.ok) throw new Error('Download failed')
    const blob = await response.blob()
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename || 'download'
    a.click()
    URL.revokeObjectURL(url)
  } catch (e) {
    setError(e instanceof Error ? e.message : 'Download failed')
  }
}
```

This needs the token from auth context. Import `useAuth` and destructure `{ user, isAdmin }` — but the token is managed in the `api.ts` module. The simplest approach: use the `get` helper which returns JSON, but we need raw bytes. Add a `downloadBlob` helper to `api.ts`:

In `api.ts`, add:
```tsx
export async function downloadBlob(url: string): Promise<{ blob: Blob; filename: string }> {
  const res = await request(url, { method: 'GET' })
  const disposition = res.headers.get('Content-Disposition') || ''
  const match = disposition.match(/filename="?([^"]+)"?/)
  const filename = match?.[1] || 'download'
  const blob = await res.blob()
  return { blob, filename }
}
```

Wait — the current `request()` function in api.ts parses JSON. We need the raw response. Check the api.ts implementation to determine if we can get the raw Response object. Looking at the api.ts code: `request()` already returns `Response` and `get()` calls `.json()` on it. So we can add:

```tsx
export async function downloadBlob(url: string): Promise<{ blob: Blob; filename: string }> {
  const res = await request(url, { method: 'GET' })
  const disposition = res.headers.get('Content-Disposition') || ''
  const match = disposition.match(/filename="?([^"]+)"?/)
  const filename = match?.[1] || 'download'
  const blob = await res.blob()
  return { blob, filename }
}
```

Then in DocumentsPage:
```tsx
import { get, post, downloadBlob } from '../api'

async function handleDownload(docId: string) {
  try {
    const { blob, filename } = await downloadBlob(`/api/docs/${docId}/download`)
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = filename
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(url)
  } catch (e) {
    setError(e instanceof Error ? e.message : 'Download failed')
  }
}
```

7. **Add pagination controls** below the table — reuse the same pattern from DocumentDetailPage:

```tsx
function Pagination({ currentPage, totalPages, onPageChange }: {
  currentPage: number; totalPages: number; onPageChange: (p: number) => void
}) {
  if (totalPages <= 1) return null

  const pages: (number | '...')[] = []
  for (let i = 1; i <= totalPages; i++) {
    if (i === 1 || i === totalPages || Math.abs(i - currentPage) <= 1) {
      pages.push(i)
    } else if (pages[pages.length - 1] !== '...') {
      pages.push('...')
    }
  }

  return (
    <div className="mt-4 flex items-center justify-center gap-1">
      <button
        onClick={() => onPageChange(currentPage - 1)}
        disabled={currentPage <= 1}
        className="rounded-lg px-2 py-1 text-sm text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-30"
      >
        Prev
      </button>
      {pages.map((p, i) =>
        p === '...' ? (
          <span key={`e${i}`} className="px-2 text-sm text-gray-400">...</span>
        ) : (
          <button
            key={p}
            onClick={() => onPageChange(p)}
            className={`rounded-lg px-2.5 py-1 text-sm font-medium ${
              p === currentPage
                ? 'bg-[var(--color-accent)] text-white'
                : 'text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700'
            }`}
          >
            {p}
          </button>
        )
      )}
      <button
        onClick={() => onPageChange(currentPage + 1)}
        disabled={currentPage >= totalPages}
        className="rounded-lg px-2 py-1 text-sm text-gray-500 hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-30"
      >
        Next
      </button>
    </div>
  )
}
```

8. **Reset currentPage to 1** when filter changes.

**Step 2: Add `downloadBlob` to api.ts**

Modify: `frontend/src/api.ts`

Add the `downloadBlob` export function after the existing `del` function.

**Step 3: Verify build**

Run: `cd frontend && npm run build`

**Step 4: Commit**

```bash
git add frontend/src/pages/DocumentsPage.tsx frontend/src/api.ts
git commit -m "feat: documents page pagination, live filter, and download"
```

---

### Task 8: Final Integration and Cleanup

**Files:**
- Modify: `frontend/src/App.tsx` (verify all routes)
- Modify: `frontend/src/pages/UploadPage.tsx` (update any links pointing to old routes)
- Verify: All internal links use correct new paths

**Step 1: Audit all internal links**

Search for any remaining references to old paths:
- `to="/"` that should be `to="/upload"` (in pages that link to Upload)
- `to="/chat"` that should be `to="/"`
- `to="/admin/system"` that should be `to="/admin/system/status"`
- Any `navigate('/chat')` or `navigate('/')` calls

Key files to check:
- `UploadPage.tsx` — may have links back to documents
- `DocumentDetailPage.tsx` — may reference old routes
- `SearchPage.tsx` — search result links go to `/docs/{id}` (unchanged, fine)

**Step 2: Verify the full route table in App.tsx**

Final route structure should be:
```
/setup                    → SetupPage
/login                    → LoginPage
(protected)
  (Layout)
    /                     → HomePage (redirects to /upload if empty, else ChatPage)
    /c/:conversationId    → ChatPage
    /upload               → UploadPage
    /docs                 → DocumentsPage
    /docs/:id             → DocumentDetailPage
    /search               → SearchPage (labeled "Raw Search" in tab)
    /preferences          → PreferencesPage
    /chat                 → redirect to /
    /chat/:conversationId → redirect to /c/:conversationId
    (admin)
      /admin              → SystemSettingsPage
      /admin/users        → UsersPage
      /admin/keys         → ApiKeysPage
      /admin/system/status     → SystemStatusPage
      /admin/system/maintenance → SystemMaintenancePage
      /admin/system       → redirect to /admin/system/status
      /admin/models       → ModelsPage
```

**Step 3: Verify full build**

Run: `cd frontend && npm run build`

**Step 4: Commit**

```bash
git add -A
git commit -m "feat: final route cleanup for UI tab redesign"
```

---

### Verification Checklist

After all tasks are complete:

1. `cd frontend && npm run build` — succeeds
2. Navigate to `/` — shows chat (or redirects to `/upload` if no docs)
3. Tab bar shows: Harbor Clerk | Upload | Documents | Raw Search | System Settings
4. Active tab has accent underline indicator
5. Click "System Settings" → shows hub with 5 items
6. Click "System Status" → shows health + stats with back button
7. Click "System Maintenance" → shows purge/reaper with back button
8. Documents page has filter field, pagination, download buttons
9. Download button successfully downloads a file
10. Old `/chat` URLs redirect to `/`
11. Old `/admin/system` redirects to `/admin/system/status`

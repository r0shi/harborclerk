# UI Tab Redesign & Documents Enhancements

## Tab Bar

- **Order:** Harbor Clerk | Upload | Documents | Raw Search | System Settings
- **Harbor Clerk tab** = existing ChatPage, becomes default route `/`
- **Chat tab removed** as separate entity
- **Raw Search** = renamed from "Search"
- **System Settings** = single admin tab replacing Users/API Keys/System/Models
- **Active tab styling:** "Connected tab" pattern — active tab gets content area background color (white/dark card), rounded top corners, navbar bottom border removed under active tab. Creates visual merge with content below.

## Default Route

- `/` renders ChatPage unless document store is empty
- If no documents exist, `/` redirects to `/upload`
- Check cached per session to avoid repeated API calls

## System Settings Hub

- `/admin` — landing page with list of clickable rows (iOS Settings style)
- Sub-routes: `/admin/users`, `/admin/keys`, `/admin/system/status`, `/admin/system/maintenance`, `/admin/models`
- Current SystemPage split into SystemStatusPage (health + stats) and SystemMaintenancePage (purge/reaper)
- BackButton handles navigation back to `/admin`

## Documents Page

- **Pagination:** Uses `user.preferences.page_size` (default 10). Standard prev/next + page numbers with ellipsis.
- **Live filter:** Client-side text input filtering on filename/title. Instant on keystroke, no debounce needed.
- **Download button:** Icon button per row, triggers `GET /api/docs/{doc_id}/download`.

## Download Endpoint

- `GET /api/docs/{doc_id}/download` — new backend route
- Looks up latest version's `original_bucket` + `original_object_key`
- Returns `StreamingResponse` with `Content-Disposition: attachment; filename="<original>"`
- Frontend triggers via `<a href>` or `window.open`

## Route Changes

| Old | New |
|-----|-----|
| `/` (Upload) | `/upload` |
| `/chat` | `/` (Harbor Clerk) |
| `/search` | `/search` (label: "Raw Search") |
| `/admin/system` | `/admin/system/status` + `/admin/system/maintenance` |
| N/A | `/admin` (System Settings hub) |

## Files Modified

- `Layout.tsx` — tab bar styling, order, labels
- `App.tsx` — route restructuring
- `DocumentsPage.tsx` — pagination, filter, download button
- `documents.py` — download endpoint

## Files Created

- `SystemSettingsPage.tsx` — admin hub landing
- `SystemStatusPage.tsx` — health checks + stats (from SystemPage)
- `SystemMaintenancePage.tsx` — purge/reaper (from SystemPage)
- Home route wrapper component (empty store redirect)

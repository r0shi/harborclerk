# Folder Picker Display Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix two display issues in PR #415's folder picker — popover overflow at viewport bottom and indistinguishable labels for folders sharing a basename — via a positioning bugfix, label auto-disambiguation, hover tooltips, and an operator-editable folder alias.

**Architecture:** Four mostly-independent pieces in one PR. A new shared `disambiguateLabels()` utility computes unique display labels from a folder list and is consumed by both `FolderPicker` and `ScopeChip`. The picker measures viewport space on open and flips the popover upward when needed. Folder rows and chips render the disambiguated label and carry a `title=fullPath` tooltip. The Folders tab gains an inline pencil-edit affordance that calls the existing `PATCH /api/watch/folders/{id}` route, which is extended to accept `display_name`. No schema migration.

**Tech Stack:** FastAPI + Pydantic v2 (backend), React 19 + TanStack Query (frontend), vitest + RTL (frontend tests), pytest (backend tests).

**Spec:** `docs/superpowers/specs/2026-05-27-folder-picker-display-fixes-design.md`

---

## Pre-flight

- [ ] **Step 0.1: Confirm branch state**

Run: `git rev-parse --abbrev-ref HEAD && git log --oneline -1`
Expected: branch `feat/folder-picker-display-fixes`, HEAD is `9e68dcf docs(spec): folder picker display fixes — positioning + disambiguation + alias`.

- [ ] **Step 0.2: Confirm clean working tree**

Run: `git status -s`
Expected: empty (or only the pre-existing untracked CLI-experiment spec file in `docs/superpowers/specs/`).

---

## Task 1: Extend `FolderPatch` + `patch_folder` to accept `display_name`

**Why:** The Folders-tab rename UI in Task 6 needs an API to write to. The existing PATCH route only accepts `enabled` and `last_event_id`. Add `display_name` with empty-string-reverts-to-default semantics and a length cap.

**Files:**
- Modify: `src/harbor_clerk/api/routes/watch.py:45` (FolderPatch class) and `src/harbor_clerk/api/routes/watch.py:387` (patch_folder handler body)
- Test: `tests/test_api_watch.py` (existing file — add tests there to follow project convention)

- [ ] **Step 1.1: Write the failing tests**

Add to `tests/test_api_watch.py`:

```python
async def test_patch_folder_sets_display_name(client, admin_token, two_folder_corpus):
    """PATCH with display_name='My Folder' updates the row and returns 200."""
    folder_a, _, _, _ = two_folder_corpus
    r = await client.patch(
        f"/api/watch/folders/{folder_a.folder_id}",
        json={"display_name": "Receipts"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200

    list_r = await client.get(
        "/api/watch/folders",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    folder_row = next(f for f in list_r.json() if f["folder_id"] == str(folder_a.folder_id))
    assert folder_row["display_name"] == "Receipts"


async def test_patch_folder_empty_display_name_reverts_to_basename(
    client, admin_token, two_folder_corpus
):
    """PATCH with display_name='' (or whitespace) writes Path(folder.path).name."""
    from pathlib import Path

    folder_a, _, _, _ = two_folder_corpus

    # First alias to something custom, then clear it
    await client.patch(
        f"/api/watch/folders/{folder_a.folder_id}",
        json={"display_name": "Custom"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    r = await client.patch(
        f"/api/watch/folders/{folder_a.folder_id}",
        json={"display_name": ""},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200

    list_r = await client.get(
        "/api/watch/folders",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    folder_row = next(f for f in list_r.json() if f["folder_id"] == str(folder_a.folder_id))
    assert folder_row["display_name"] == Path(folder_a.path).name


async def test_patch_folder_whitespace_display_name_reverts_to_basename(
    client, admin_token, two_folder_corpus
):
    """PATCH with display_name='   ' is treated as empty → reverts."""
    from pathlib import Path

    folder_a, _, _, _ = two_folder_corpus
    r = await client.patch(
        f"/api/watch/folders/{folder_a.folder_id}",
        json={"display_name": "   "},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200
    list_r = await client.get(
        "/api/watch/folders",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    folder_row = next(f for f in list_r.json() if f["folder_id"] == str(folder_a.folder_id))
    assert folder_row["display_name"] == Path(folder_a.path).name


async def test_patch_folder_too_long_display_name_returns_422(
    client, admin_token, two_folder_corpus
):
    """display_name longer than 200 chars → 422."""
    folder_a, _, _, _ = two_folder_corpus
    r = await client.patch(
        f"/api/watch/folders/{folder_a.folder_id}",
        json={"display_name": "x" * 201},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 422


async def test_patch_folder_omitted_display_name_leaves_field_unchanged(
    client, admin_token, two_folder_corpus
):
    """A PATCH that doesn't include display_name must not alter it."""
    folder_a, _, _, _ = two_folder_corpus

    # Set a custom alias first
    await client.patch(
        f"/api/watch/folders/{folder_a.folder_id}",
        json={"display_name": "Sentinel"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    # Now PATCH only enabled
    r = await client.patch(
        f"/api/watch/folders/{folder_a.folder_id}",
        json={"enabled": True},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200

    list_r = await client.get(
        "/api/watch/folders",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    folder_row = next(f for f in list_r.json() if f["folder_id"] == str(folder_a.folder_id))
    assert folder_row["display_name"] == "Sentinel"
```

The `client`, `admin_token`, and `two_folder_corpus` fixtures are already in `tests/conftest.py` from PR #415's work — verify they exist before running.

- [ ] **Step 1.2: Run the tests, verify they fail**

Run: `uv run pytest tests/test_api_watch.py -v -k patch_folder`
Expected: 5 FAILs — the existing PATCH doesn't accept `display_name`, so requests with that field either succeed without writing the field (display_name returned remains the default) or get rejected, depending on whether Pydantic's extras policy on FolderPatch ignores or forbids unknowns. Either way, the assertions fail.

- [ ] **Step 1.3: Extend `FolderPatch` schema**

Modify `src/harbor_clerk/api/routes/watch.py:45-47` (add the new field):

```python
class FolderPatch(BaseModel):
    enabled: bool | None = None
    last_event_id: int | None = None
    display_name: str | None = None
```

- [ ] **Step 1.4: Extend `patch_folder` handler**

Modify `src/harbor_clerk/api/routes/watch.py` (the `patch_folder` function around line 387). After the `last_event_id` block and before the `enabled_changed_to` notification block, add:

```python
    if body.display_name is not None:
        from pathlib import Path

        new_name = body.display_name.strip()
        if not new_name:
            new_name = Path(folder.path).name
        if len(new_name) > 200:
            raise HTTPException(status_code=422, detail="display_name exceeds 200 chars")
        folder.display_name = new_name
```

The final shape of the function body (relevant section):

```python
    enabled_changed_to: bool | None = None
    if body.enabled is not None and body.enabled != folder.enabled:
        folder.enabled = body.enabled
        enabled_changed_to = body.enabled
    if body.last_event_id is not None:
        folder.last_event_id = body.last_event_id
    if body.display_name is not None:
        from pathlib import Path

        new_name = body.display_name.strip()
        if not new_name:
            new_name = Path(folder.path).name
        if len(new_name) > 200:
            raise HTTPException(status_code=422, detail="display_name exceeds 200 chars")
        folder.display_name = new_name

    if enabled_changed_to is not None:
        action = "enabled" if enabled_changed_to else "disabled"
        await notify_folder_change_async(session, folder.folder_id, action=action)

    await session.commit()
    return {"status": "updated"}
```

- [ ] **Step 1.5: Run the tests, verify they pass**

Run: `uv run pytest tests/test_api_watch.py -v -k patch_folder`
Expected: 5 PASS.

- [ ] **Step 1.6: Regression on the broader watch suite**

Run: `uv run pytest tests/test_api_watch.py -v`
Expected: all pass — no other watch behavior changed.

- [ ] **Step 1.7: Commit**

```bash
git add src/harbor_clerk/api/routes/watch.py tests/test_api_watch.py
git commit -m "feat(watch): PATCH /folders/{id} accepts display_name with revert-to-basename semantics"
```

---

## Task 2: `disambiguateLabels()` utility — new shared pure function

**Why:** Both `FolderPicker` and `ScopeChip` need to render unique labels for folders. Centralizing this in one tested, React-free utility is cleaner than duplicating it twice.

**Files:**
- Create: `frontend/src/components/folderLabels.ts`
- Test: `frontend/src/components/folderLabels.test.ts`

- [ ] **Step 2.1: Write the failing tests**

Create `frontend/src/components/folderLabels.test.ts`:

```ts
import { describe, it, expect } from 'vitest'
import { disambiguateLabels, type FolderLike } from './folderLabels'

function f(folder_id: string, path: string, display_name: string | null = null): FolderLike {
  return { folder_id, path, display_name }
}

describe('disambiguateLabels', () => {
  it('uses display_name as-is when no collisions', () => {
    const folders = [
      f('1', '/work/contracts', 'Contracts'),
      f('2', '/work/legal', 'Legal'),
    ]
    const labels = disambiguateLabels(folders)
    expect(labels.get('1')).toBe('Contracts')
    expect(labels.get('2')).toBe('Legal')
  })

  it('walks up one path segment when two labels collide', () => {
    const folders = [
      f('1', '/work/cuad/ingest', 'ingest'),
      f('2', '/work/qwen3-20b/cuad/ingest', 'ingest'),
    ]
    const labels = disambiguateLabels(folders)
    expect(labels.get('1')).toBe('cuad/ingest')
    expect(labels.get('2')).toBe('qwen3-20b/cuad/ingest')
  })

  it('extends as many segments as needed for three+ folders sharing a basename', () => {
    const folders = [
      f('1', '/work/cuad/ingest', 'ingest'),
      f('2', '/work/qwen3-20b/cuad/ingest', 'ingest'),
      f('3', '/work/gpt-oss-20b/cuad/ingest', 'ingest'),
    ]
    const labels = disambiguateLabels(folders)
    expect(labels.get('1')).toBe('cuad/ingest')
    expect(labels.get('2')).toBe('qwen3-20b/cuad/ingest')
    expect(labels.get('3')).toBe('gpt-oss-20b/cuad/ingest')
  })

  it('mixes unique and colliding correctly', () => {
    const folders = [
      f('1', '/work/contracts', 'Contracts'),    // unique
      f('2', '/work/cuad/ingest', 'ingest'),     // colliding group
      f('3', '/work/qwen3-20b/cuad/ingest', 'ingest'),
    ]
    const labels = disambiguateLabels(folders)
    expect(labels.get('1')).toBe('Contracts')
    expect(labels.get('2')).toBe('cuad/ingest')
    expect(labels.get('3')).toBe('qwen3-20b/cuad/ingest')
  })

  it('falls back to path basename when display_name is null', () => {
    const folders = [
      f('1', '/work/contracts', null),
      f('2', '/work/legal', null),
    ]
    const labels = disambiguateLabels(folders)
    expect(labels.get('1')).toBe('contracts')
    expect(labels.get('2')).toBe('legal')
  })

  it('falls back to UUID suffix when two folders have identical paths (degenerate)', () => {
    const folders = [
      f('aaaaaaaa-1111-2222-3333-444444444444', '/work/dup', 'dup'),
      f('bbbbbbbb-5555-6666-7777-888888888888', '/work/dup', 'dup'),
    ]
    const labels = disambiguateLabels(folders)
    // Each label must be unique and end with a UUID suffix
    const a = labels.get('aaaaaaaa-1111-2222-3333-444444444444')
    const b = labels.get('bbbbbbbb-5555-6666-7777-888888888888')
    expect(a).not.toBe(b)
    expect(a).toContain('dup')
    expect(b).toContain('dup')
  })
})
```

- [ ] **Step 2.2: Run the tests, verify they fail with module-not-found**

Run: `cd frontend && npm test -- folderLabels --run`
Expected: FAIL — `Failed to load url ./folderLabels` (module does not exist yet).

- [ ] **Step 2.3: Implement the utility**

Create `frontend/src/components/folderLabels.ts`:

```ts
export interface FolderLike {
  folder_id: string
  path: string
  display_name: string | null
}

function basename(path: string): string {
  const parts = path.split('/').filter(Boolean)
  return parts[parts.length - 1] ?? path
}

function pathSegments(path: string): string[] {
  return path.split('/').filter(Boolean)
}

function candidate(folder: FolderLike): string {
  if (folder.display_name && folder.display_name.trim() !== '') {
    return folder.display_name
  }
  return basename(folder.path)
}

/**
 * Build a unique display label for each folder.
 *
 * Default label is `display_name` (or path basename when display_name is null/empty).
 * When two or more folders share the same default label, prepend just enough of
 * the parent path to each so that all labels in the colliding group are unique
 * (file-manager / VS Code tab pattern).
 *
 * In the degenerate case where two folders have identical paths, falls back to
 * appending the last 6 characters of the folder UUID.
 */
export function disambiguateLabels(folders: FolderLike[]): Map<string, string> {
  const labels = new Map<string, string>()

  // Group by candidate label
  const groups = new Map<string, FolderLike[]>()
  for (const folder of folders) {
    const key = candidate(folder)
    const existing = groups.get(key) ?? []
    existing.push(folder)
    groups.set(key, existing)
  }

  for (const [key, group] of groups) {
    if (group.length === 1) {
      labels.set(group[0].folder_id, key)
      continue
    }

    // Disambiguate by extending each folder's path one segment at a time
    // until all labels in the group are unique.
    const groupLabels = new Map<string, string>() // folder_id -> current label
    for (const f of group) {
      groupLabels.set(f.folder_id, key)
    }

    let depth = 2 // start with parent/basename
    const maxDepth = Math.max(...group.map((f) => pathSegments(f.path).length))

    while (depth <= maxDepth) {
      // Recompute each group member's label at the current depth
      const tentative = new Map<string, string>()
      for (const f of group) {
        const segs = pathSegments(f.path)
        const tail = segs.slice(-depth).join('/')
        // If candidate was a custom alias (not the basename), keep the alias
        // as the leaf and prepend path segments above the basename.
        // Implementation: when display_name overrides basename, replace the
        // last segment of the slice with the alias.
        const cand = candidate(f)
        const segsForLabel =
          cand === basename(f.path)
            ? segs.slice(-depth)
            : [...segs.slice(-depth, -1), cand]
        tentative.set(f.folder_id, segsForLabel.join('/'))
      }
      // Check for uniqueness within the group
      const seen = new Set<string>()
      let allUnique = true
      for (const label of tentative.values()) {
        if (seen.has(label)) {
          allUnique = false
          break
        }
        seen.add(label)
      }
      if (allUnique) {
        for (const [fid, label] of tentative) {
          groupLabels.set(fid, label)
        }
        break
      }
      depth += 1
    }

    // If we reached maxDepth and still have collisions (e.g., identical paths),
    // fall back to UUID suffix on the duplicates.
    const finalSeen = new Map<string, string[]>()
    for (const [fid, label] of groupLabels) {
      const ids = finalSeen.get(label) ?? []
      ids.push(fid)
      finalSeen.set(label, ids)
    }
    for (const [label, ids] of finalSeen) {
      if (ids.length > 1) {
        for (const fid of ids) {
          groupLabels.set(fid, `${label} (${fid.slice(-6)})`)
        }
      }
    }

    for (const [fid, label] of groupLabels) {
      labels.set(fid, label)
    }
  }

  return labels
}
```

- [ ] **Step 2.4: Run the tests, verify they pass**

Run: `cd frontend && npm test -- folderLabels --run`
Expected: 6 PASS.

- [ ] **Step 2.5: Ruff/lint check**

Run: `cd frontend && npm run lint && npm run type-check`
Expected: clean — no new errors.

- [ ] **Step 2.6: Commit**

```bash
git add frontend/src/components/folderLabels.ts frontend/src/components/folderLabels.test.ts
git commit -m "feat(scope): disambiguateLabels utility — shared by FolderPicker and ScopeChip"
```

---

## Task 3: `FolderPicker` — popover auto-flip on viewport overflow

**Why:** Popover gets clipped at the bottom of the viewport in the Ask chat input row. Detect proximity to viewport bottom and open upward when there's more room above.

**Files:**
- Modify: `frontend/src/components/FolderPicker.tsx`

- [ ] **Step 3.1: Read the current FolderPicker structure**

Open `frontend/src/components/FolderPicker.tsx`. Identify:
- The trigger button (the `<button>` showing "Folders: All" / current selection).
- The popover container (the `<div>` rendered when `open` is true).
- The current direction classes (likely `top-full mt-1` or similar).

- [ ] **Step 3.2: Add a useRef for the trigger and useState for direction**

In the component body (near the existing `useState` calls), add:

```tsx
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'

// inside the component:
const triggerRef = useRef<HTMLButtonElement>(null)
const [popDirection, setPopDirection] = useState<'down' | 'up'>('down')
```

- [ ] **Step 3.3: Add the layout-effect that measures and chooses direction**

After the existing useEffect / useState hooks, add:

```tsx
useLayoutEffect(() => {
  if (!open || !triggerRef.current) return
  const rect = triggerRef.current.getBoundingClientRect()
  const popoverMaxHeight = 360 // matches the max-h cap on the popover div
  const margin = 8
  const spaceBelow = window.innerHeight - rect.bottom - margin
  const spaceAbove = rect.top - margin
  setPopDirection(
    spaceBelow >= popoverMaxHeight || spaceBelow >= spaceAbove ? 'down' : 'up',
  )
}, [open])
```

- [ ] **Step 3.4: Attach the ref to the trigger button**

Find the trigger `<button>` element (the one with text content `summarize(value, folders)`) and add `ref={triggerRef}` to it.

- [ ] **Step 3.5: Swap popover positioning classes based on direction**

Find the popover container div (the one rendered conditionally inside `{open && (...)}`). It currently has classes like `absolute top-full ... mt-1 ...`. Swap to conditional positioning:

```tsx
<div
  className={`absolute z-30 ${
    popDirection === 'up' ? 'bottom-full mb-1' : 'top-full mt-1'
  } left-0 min-w-[260px] max-h-[360px] overflow-hidden rounded-md border border-[var(--color-border)] bg-[var(--color-bg-secondary)] shadow-lg`}
  role="listbox"
  aria-multiselectable
>
  ...
</div>
```

Keep all other props (role, aria-multiselectable, etc.) as they currently are. The key change is `bottom-full mb-1` vs `top-full mt-1` and the explicit `max-h-[360px]`.

- [ ] **Step 3.6: Verify the existing FolderPicker tests still pass**

Run: `cd frontend && npm test -- FolderPicker --run`
Expected: all 8 existing tests still pass. JSDOM's `getBoundingClientRect` returns zeros, so `popDirection` will resolve to its default `'down'` in tests — the existing assertions remain valid.

- [ ] **Step 3.7: Lint + typecheck**

Run: `cd frontend && npm run lint && npm run type-check`
Expected: clean.

- [ ] **Step 3.8: Manual verification**

Start the dev server and Harbor Clerk API:
```bash
cd frontend && npm run dev
# in another terminal:
uv run harbor-clerk-api
```

In the browser:
1. Open the Ask page (`/`) with the window at default height. Open the folder picker via the "Folders: All" chip. Confirm the popover opens DOWNWARD and the folder list is visible/scrollable.
2. Shrink the browser window vertically so the chat input is at the very bottom of the viewport. Open the picker. Confirm the popover now flips UPWARD and remains fully visible.
3. Open the Research page. The folder picker is mid-page; popover should open downward.
4. Open the Search page. Folder picker is near the top; popover should open downward.

If any of those don't work, fix and re-verify before committing.

- [ ] **Step 3.9: Commit**

```bash
git add frontend/src/components/FolderPicker.tsx
git commit -m "fix(scope): FolderPicker popover flips upward when viewport bottom is near"
```

---

## Task 4: `FolderPicker` — consume `disambiguateLabels` + add tooltips

**Why:** Replace the inline `display_name ?? folder_id` rendering in `FolderPicker` rows and the `summarize()` trigger label with the new utility's output. Add `title=fullPath` on each row.

**Files:**
- Modify: `frontend/src/components/FolderPicker.tsx`
- Test: `frontend/src/components/FolderPicker.test.tsx` (add tests, keep existing)

- [ ] **Step 4.1: Write the failing tests**

Append to `frontend/src/components/FolderPicker.test.tsx`:

```ts
it('disambiguates labels when two folders share display_name', () => {
  const colliding = [
    {
      folder_id: 'a',
      path: '/work/cuad/ingest',
      display_name: 'ingest',
      unavailable_reason: null,
      enabled: true,
      auto_discovered: false,
      skipped_count: 0,
      skipped_extensions: [],
    },
    {
      folder_id: 'b',
      path: '/work/qwen3-20b/cuad/ingest',
      display_name: 'ingest',
      unavailable_reason: null,
      enabled: true,
      auto_discovered: false,
      skipped_count: 0,
      skipped_extensions: [],
    },
  ]
  render(<FolderPicker value={[]} onChange={() => {}} folders={colliding} />)
  fireEvent.click(screen.getAllByRole('button')[0])
  expect(screen.getByText('cuad/ingest')).toBeInTheDocument()
  expect(screen.getByText('qwen3-20b/cuad/ingest')).toBeInTheDocument()
})

it('renders a title attribute with the full path on each row', () => {
  render(<FolderPicker value={[]} onChange={() => {}} folders={folders} />)
  fireEvent.click(screen.getAllByRole('button')[0])
  // folders[0] is /c (test fixture at top of file)
  const row = screen.getByText('Contracts').closest('label')
  expect(row).toHaveAttribute('title', '/c')
})

it('uses disambiguated labels in the trigger button summary', () => {
  const colliding = [
    {
      folder_id: 'a',
      path: '/work/cuad/ingest',
      display_name: 'ingest',
      unavailable_reason: null,
      enabled: true,
      auto_discovered: false,
      skipped_count: 0,
      skipped_extensions: [],
    },
    {
      folder_id: 'b',
      path: '/work/qwen3-20b/cuad/ingest',
      display_name: 'ingest',
      unavailable_reason: null,
      enabled: true,
      auto_discovered: false,
      skipped_count: 0,
      skipped_extensions: [],
    },
  ]
  render(<FolderPicker value={['a', 'b']} onChange={() => {}} folders={colliding} />)
  expect(screen.getByRole('button')).toHaveTextContent(
    'Folders: cuad/ingest, qwen3-20b/cuad/ingest (2)',
  )
})
```

- [ ] **Step 4.2: Run the tests, verify they fail**

Run: `cd frontend && npm test -- FolderPicker --run`
Expected: 3 new FAILs (one renders raw "ingest" twice, one finds no title, one shows duplicate "ingest" in summary).

- [ ] **Step 4.3: Wire the utility into FolderPicker**

In `frontend/src/components/FolderPicker.tsx`:

Add the import near the top:

```tsx
import { disambiguateLabels } from './folderLabels'
```

Inside the component, after the existing destructured state, compute the label map:

```tsx
const labelMap = useMemo(() => disambiguateLabels(folders), [folders])
```

Replace the `summarize` function to take the labelMap:

```tsx
function summarize(
  value: string[],
  folders: WatchedFolderInfo[],
  labelMap: Map<string, string>,
): string {
  if (value.length === 0) return 'Folders: All'
  const labels = value
    .map((id) => labelMap.get(id) ?? folders.find((f) => f.folder_id === id)?.display_name ?? '?')
    .slice(0, 3)
  const suffix = value.length > 1 ? ` (${value.length})` : ''
  return `Folders: ${labels.join(', ')}${suffix}`
}
```

Update the call site of `summarize`:

```tsx
const buttonText = noFolders ? 'No folders to scope to' : summarize(value, folders, labelMap)
```

Find the row rendering loop. Each row's label currently renders `{f.display_name ?? f.folder_id}` and `aria-label={f.display_name ?? f.folder_id}`. Replace with the labelMap:

```tsx
<label
  className="..."
  title={f.path}
>
  <input
    type="checkbox"
    checked={value.includes(f.folder_id)}
    onChange={() => toggle(f.folder_id)}
    aria-label={labelMap.get(f.folder_id) ?? f.folder_id}
  />
  <span className="text-sm text-[var(--color-text-primary)] truncate">
    {labelMap.get(f.folder_id) ?? f.folder_id}
  </span>
</label>
```

Also update the search-filter predicate to match the disambiguated label (so users can type a fragment of `cuad/ingest` and find it):

```tsx
const visible = useMemo(
  () =>
    folders.filter((f) => {
      if (filter.trim() === '') return true
      const label = labelMap.get(f.folder_id) ?? f.display_name ?? ''
      return label.toLowerCase().includes(filter.toLowerCase())
    }),
  [folders, filter, labelMap],
)
```

- [ ] **Step 4.4: Run the tests, verify they pass**

Run: `cd frontend && npm test -- FolderPicker --run`
Expected: 11 PASS (8 original + 3 new).

- [ ] **Step 4.5: Lint + typecheck**

Run: `cd frontend && npm run lint && npm run type-check`
Expected: clean.

- [ ] **Step 4.6: Commit**

```bash
git add frontend/src/components/FolderPicker.tsx frontend/src/components/FolderPicker.test.tsx
git commit -m "feat(scope): FolderPicker uses disambiguateLabels + full-path tooltips"
```

---

## Task 5: `ScopeChip` — consume `disambiguateLabels` + tooltip

**Why:** The read-only chip in conversation/research headers shows the active scope. Must use the same disambiguation as the picker so the rendered labels stay consistent.

**Files:**
- Modify: `frontend/src/components/ScopeChip.tsx`
- Test: `frontend/src/components/ScopeChip.test.tsx`

- [ ] **Step 5.1: Write the failing test**

Append to `frontend/src/components/ScopeChip.test.tsx`:

```ts
it('disambiguates colliding display_names', () => {
  const colliding = [
    { folder_id: 'a', path: '/work/cuad/ingest', display_name: 'ingest',
      unavailable_reason: null, enabled: true, auto_discovered: false,
      skipped_count: 0, skipped_extensions: [] },
    { folder_id: 'b', path: '/work/qwen3-20b/cuad/ingest', display_name: 'ingest',
      unavailable_reason: null, enabled: true, auto_discovered: false,
      skipped_count: 0, skipped_extensions: [] },
  ]
  render(<ScopeChip scope={{ folder_ids: ['a', 'b'] }} folders={colliding} />)
  expect(screen.getByText(/cuad\/ingest.*qwen3-20b\/cuad\/ingest/)).toBeInTheDocument()
})

it('sets a title attribute with full paths, one per line', () => {
  render(<ScopeChip scope={{ folder_ids: ['a'] }} folders={folders} />)
  const chip = screen.getByText(/Contracts/)
  expect(chip.getAttribute('title')).toContain('/c')
})
```

- [ ] **Step 5.2: Run the tests, verify they fail**

Run: `cd frontend && npm test -- ScopeChip --run`
Expected: 2 FAILs.

- [ ] **Step 5.3: Wire the utility into ScopeChip**

Modify `frontend/src/components/ScopeChip.tsx`:

```tsx
import type { WatchedFolderInfo } from '../hooks/useWatchedFolders'
import { disambiguateLabels } from './folderLabels'

export interface ScopeChipProps {
  scope: { folder_ids?: string[] } | null | undefined
  folders: WatchedFolderInfo[]
  className?: string
}

export function ScopeChip({ scope, folders, className }: ScopeChipProps) {
  const ids = scope?.folder_ids ?? []
  if (ids.length === 0) {
    return <span className={className ?? 'scope-chip'}>Folders: All</span>
  }
  const labelMap = disambiguateLabels(folders)
  const labels = ids.map((id) => labelMap.get(id) ?? '?').slice(0, 3)
  const suffix = ids.length > 1 ? ` (${ids.length})` : ''
  const fullPaths = ids
    .map((id) => folders.find((f) => f.folder_id === id)?.path)
    .filter((p): p is string => p != null)
    .join('\n')
  return (
    <span className={className ?? 'scope-chip'} title={fullPaths}>
      {`Folders: ${labels.join(', ')}${suffix}`}
    </span>
  )
}
```

- [ ] **Step 5.4: Run the tests, verify they pass**

Run: `cd frontend && npm test -- ScopeChip --run`
Expected: 6 PASS (4 existing + 2 new).

- [ ] **Step 5.5: Lint + typecheck**

Run: `cd frontend && npm run lint && npm run type-check`
Expected: clean.

- [ ] **Step 5.6: Commit**

```bash
git add frontend/src/components/ScopeChip.tsx frontend/src/components/ScopeChip.test.tsx
git commit -m "feat(scope): ScopeChip uses disambiguateLabels + full-path tooltip"
```

---

## Task 6: `FoldersPage` — inline rename affordance

**Why:** Operators need to alias a folder. Inline pencil-edit on the existing Folders tab is the lowest-friction UX and reuses the PATCH route from Task 1.

**Files:**
- Modify: `frontend/src/pages/FoldersPage.tsx`

This task does not add new tests (the spec explicitly says manual verification is sufficient for the rename UI). Existing FoldersPage tests, if any, should not regress.

- [ ] **Step 6.1: Read FoldersPage and identify the folder-row rendering site**

Open `frontend/src/pages/FoldersPage.tsx`. Search for the JSX that renders each watched folder row (likely a `.map((folder) => <div ...>` or similar). Note:
- Where the folder's display name renders
- Where the path is shown (or if it's only in a tooltip currently)
- How API calls are made (look for existing `patch`, `post`, or `put` helpers — `frontend/src/api.ts` likely exposes a `patch()` function)
- How the folders list is fetched and refreshed (TanStack Query cache key — likely `['watch', 'folders']`)

- [ ] **Step 6.2: Add rename state to the page**

At the top of the FoldersPage component, add:

```tsx
const [editingId, setEditingId] = useState<string | null>(null)
const [draftName, setDraftName] = useState('')
const [renameError, setRenameError] = useState<string | null>(null)
const queryClient = useQueryClient()
```

Add the import:

```tsx
import { useQueryClient } from '@tanstack/react-query'
```

- [ ] **Step 6.3: Add the rename handler**

Add a `saveAlias` function near the other handlers in the component:

```tsx
async function saveAlias(folderId: string, alias: string) {
  setRenameError(null)
  try {
    await patch(`/api/watch/folders/${folderId}`, { display_name: alias })
    setEditingId(null)
    setDraftName('')
    queryClient.invalidateQueries({ queryKey: ['watch', 'folders'] })
  } catch (err) {
    setRenameError(err instanceof Error ? err.message : 'Rename failed')
  }
}
```

Import the `patch` helper from `frontend/src/api.ts` (verify the export name — it should be alongside `get` and `post`). If the project uses `request` or a different pattern, adapt to match.

- [ ] **Step 6.4: Replace the folder-name display with edit-or-display**

Find the JSX block that renders the folder name in each row. Replace it with:

```tsx
<div className="folder-row__identity">
  {editingId === folder.folder_id ? (
    <div className="flex items-center gap-2">
      <input
        autoFocus
        value={draftName}
        onChange={(e) => setDraftName(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') saveAlias(folder.folder_id, draftName)
          if (e.key === 'Escape') {
            setEditingId(null)
            setDraftName('')
            setRenameError(null)
          }
        }}
        onBlur={() => saveAlias(folder.folder_id, draftName)}
        maxLength={200}
        className="rounded border border-[var(--color-border)] bg-[var(--color-bg-primary)] px-2 py-1 text-sm"
      />
      {renameError ? (
        <span className="text-xs text-red-400">{renameError}</span>
      ) : null}
    </div>
  ) : (
    <div className="flex items-center gap-2">
      <span className="font-medium" title={folder.path}>
        {folder.display_name ?? folder.path.split('/').filter(Boolean).pop() ?? folder.folder_id}
      </span>
      <button
        type="button"
        onClick={() => {
          setDraftName(folder.display_name ?? '')
          setEditingId(folder.folder_id)
          setRenameError(null)
        }}
        aria-label={`Rename ${folder.display_name ?? folder.folder_id}`}
        className="text-[var(--color-text-secondary)] hover:text-[var(--color-text-primary)]"
      >
        ✎
      </button>
    </div>
  )}
  <div className="text-xs text-[var(--color-text-secondary)] truncate" title={folder.path}>
    {folder.path}
  </div>
</div>
```

The secondary line showing `folder.path` is the read-only path display from the spec. Style it muted so it doesn't compete with the alias.

- [ ] **Step 6.5: Sanity check via grep**

Confirm no other place in `FoldersPage.tsx` renders the same folder name without your new edit affordance. If there's a separate folder-detail view or any other rendering site, leave it alone — this task is the list view only.

```bash
grep -n "display_name" frontend/src/pages/FoldersPage.tsx
```

You should see the new usages plus whatever was there before. Don't add the pencil to other surfaces (e.g., breadcrumbs, page headers) in this task.

- [ ] **Step 6.6: Lint + typecheck**

Run: `cd frontend && npm run lint && npm run type-check`
Expected: clean.

- [ ] **Step 6.7: Manual verification**

Start dev stack:
```bash
cd frontend && npm run dev
# in another terminal:
uv run harbor-clerk-api
```

In the browser:
1. Open `/folders`. Each folder row should show its name + a pencil icon + the path beneath.
2. Click a pencil. The name converts to an input pre-filled with the current name.
3. Type a new name. Press Enter. Confirm the row re-renders with the new name; the picker on `/` shows the new name too (cache invalidation worked).
4. Click the pencil again. Clear the field entirely. Press Enter. Confirm the row shows the path basename (revert).
5. Click the pencil. Type 201 characters. Press Enter. Confirm an error appears next to the field and the row keeps the original name.
6. Click the pencil. Press Escape. Confirm the edit is cancelled with no API call (verify in network tab).

- [ ] **Step 6.8: Commit**

```bash
git add frontend/src/pages/FoldersPage.tsx
git commit -m "feat(folders): inline pencil-edit rename for folder display_name"
```

---

## Task 7: Wrap-up — full verification, fresh-eyes review, PR

- [ ] **Step 7.1: Full Python test suite**

Run: `uv run pytest --ignore=tests/integration`
Expected: all pass — no regression from the watch-route changes.

- [ ] **Step 7.2: Full frontend test suite**

Run: `cd frontend && npm test -- --run`
Expected: all pass.

- [ ] **Step 7.3: Lint + format + type-check (both stacks)**

Run:
```bash
uv run ruff check . && uv run ruff format --check .
cd frontend && npm run lint && npm run type-check && npm run format:check
```
Expected: every check clean.

- [ ] **Step 7.4: Manual happy path on a real corpus**

(If possible — needs the actual macOS dev stack or Docker stack running with > 1 watched folder.)

1. **Confirm popover fix:** Open Ask. Folder picker opens with list visible. Shrink window so chat input sits at viewport bottom; picker flips upward and remains usable.
2. **Confirm disambiguation:** Create at least two watched folders that share a basename (e.g., on macOS Server, watch two parent directories that each have an `ingest` subfolder). Confirm the picker shows them as `parent1/ingest` and `parent2/ingest`, not two identical `ingest` rows.
3. **Confirm rename:** Go to `/folders`. Rename one of the colliding folders to `Receipts`. Return to Ask. Open the picker. The renamed folder appears as `Receipts`; the other remains disambiguated.
4. **Confirm tooltip:** Hover any picker row. Native browser tooltip shows the full filesystem path.
5. **Confirm chip in conversation header:** Start a new conversation scoped to the renamed folder. Header chip reads `Folders: Receipts`. Hover the chip; tooltip shows the path.

- [ ] **Step 7.5: Dispatch a fresh-eyes review**

Per the standing directive on substantive PRs, dispatch a `feature-dev:code-reviewer` agent against the branch tip with a minimal, unconstrained prompt. Address findings ≥80 confidence before opening the PR.

The PR touches: 1 backend schema/route, 1 new TS utility + tests, 3 React components, 1 page. Multi-component scope — review is warranted.

- [ ] **Step 7.6: Push the branch**

```bash
git push -u origin feat/folder-picker-display-fixes
```

- [ ] **Step 7.7: Open the PR**

```bash
gh pr create --title "fix(scope): folder picker positioning + label disambiguation + rename" --body-file ...
```

PR body should:
- Reference the spec at `docs/superpowers/specs/2026-05-27-folder-picker-display-fixes-design.md`
- Summarize the four pieces (positioning, disambig, tooltip, alias)
- Test plan: list the 5 manual checks from Step 7.4 plus the automated CI suite
- Note the fresh-eyes review findings (if any) and how they were addressed

---

## Notes / pitfalls

- **`disambiguateLabels` algorithm:** the inner loop walks segments from rightmost to leftmost. For two folders with shared tail prefixes (`a/x` and `b/a/x`), the depth-2 attempt would yield `a/x` and `a/x` (still colliding), so the loop advances to depth 3: `a/x` (only 2 segments available) and `b/a/x`. The implementation should not crash on `slice(-3)` when there are only 2 segments — `slice` handles short arrays gracefully. The reference implementation in Task 2 has been written to do this.

- **Test fixture shape drift:** `WatchedFolderInfo` has these required fields per `useWatchedFolders.ts`: `folder_id`, `display_name`, `path`, `unavailable_reason`, `enabled`, `auto_discovered`, `skipped_count`, `skipped_extensions`. New test fixtures must include all of them or TypeScript will complain. The plan's test snippets include them; reuse the pattern.

- **TanStack Query cache key:** the existing fetch in `useWatchedFolders` uses `['watch', 'folders']`. Invalidate with that exact key after the PATCH in Task 6. Verify by reading `useWatchedFolders.ts` first.

- **The `summarize` truncation:** the rendered button text only shows the first 3 folder names plus a count suffix. With long disambiguated names (`qwen3-20b/cuad/ingest`) the button can grow wide. We're not adding ellipsis here — the existing CSS truncation (`truncate` class) on the trigger button will handle it. If a future polish pass wants smarter truncation, it can come later.

- **No portal / no floating-ui dep:** Task 3 deliberately uses raw DOM measurement and a class swap. Adding `@floating-ui/react` would be ~10kb gz for behavior we get for free with one `useLayoutEffect`.

- **The "Select all" / "Clear" buttons inside the popover:** Task 3's structural change to the popover container doesn't alter these. They live inside the popover, which now has `max-h-[360px] overflow-hidden`. The inner `<ul>` keeps its own `overflow-y-auto`. The actions bar (Select all / Clear) sits above the scrollable list and remains pinned.

- **Pre-existing ESLint warnings:** the project has ~14 pre-existing react-hooks/react-refresh warnings on unrelated files. Lint runs should report no NEW warnings on any file we touch.

## Self-review checklist for the engineer

- [ ] PATCH `/api/watch/folders/{id}` accepts `display_name` and produces 422 over 200 chars
- [ ] Empty/whitespace `display_name` reverts to `Path(path).name` server-side
- [ ] Omitting `display_name` from a PATCH body leaves the field unchanged
- [ ] `disambiguateLabels` handles: no-collision, single-collision, multi-collision, mixed, null display_name, degenerate identical paths
- [ ] FolderPicker popover flips upward when there's not enough room below
- [ ] FolderPicker rows show disambiguated labels + `title=fullPath`
- [ ] FolderPicker trigger button summarizes with disambiguated labels
- [ ] FolderPicker search filter matches against the rendered (disambiguated) label
- [ ] ScopeChip shows disambiguated labels + `title=fullPaths` (newline-separated when multiple)
- [ ] FoldersPage row has pencil-edit affordance + path display below the name
- [ ] Pencil-edit Enter saves, Escape cancels, blur saves
- [ ] After rename, the picker on other pages updates without manual refresh (cache invalidation)
- [ ] No regressions in the existing FolderPicker (8) / ScopeChip (4) / useWatchedFolders (2) test counts

## Spec coverage mapping

| Spec piece | Task(s) |
|---|---|
| Piece 1 — popover positioning | Task 3 |
| Piece 2 — label disambiguation | Task 2, Task 4, Task 5 |
| Piece 3 — tooltips | Task 4, Task 5 |
| Piece 4 — editable alias backend | Task 1 |
| Piece 4 — editable alias frontend (FoldersPage) | Task 6 |
| Edge cases (length cap, empty alias, etc.) | Task 1 tests |
| Edge cases (collision, mixed, degenerate) | Task 2 tests |
| Manual verification | Task 7 |

Every spec section has at least one task. No orphans.

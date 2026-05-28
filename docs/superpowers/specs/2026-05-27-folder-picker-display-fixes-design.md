# Folder Picker Display Fixes — Design Spec

**Date:** 2026-05-27
**Status:** Draft
**Scope:** Two related UX fixes to the folder picker shipped in PR #415 (feat: folder-scope filter for Ask/Research/Search).

## Overview

PR #415 introduced a folder filter for Ask/Research/Search. Two display issues surfaced in real use:

1. **Popover overflow.** When the picker sits at the bottom of the viewport (the Ask page's input footer), opening the popover downward extends off-screen — the folder list isn't reachable.
2. **Identical labels.** Watched folders are labeled by their path basename (`Path(path).name`). Corpora that follow a `<model>/<dataset>/ingest` layout (e.g. test-corpora runs) produce multiple folders all displayed as "ingest", indistinguishable in the picker.

This spec addresses both with a bundle: a positioning bugfix, automatic label disambiguation when basenames collide, hover tooltips with the full path, and an operator-editable alias on the Folders tab.

## Goals

- The picker popover never overflows the viewport; folders are always reachable.
- Folders with colliding `display_name` values are visually distinguished without operator action.
- Operators can give any folder a custom name on the Folders tab.
- The same disambiguation logic is reused by `FolderPicker` rows and `ScopeChip` (the read-only chip in conversation headers / research detail).
- Backward compatibility: existing watched folders, conversations, and research runs work without migration.

## Non-Goals

- Tree-style rendering (`▼ work/ ⋯ ingest`) — not necessary, the flat-with-disambig pattern is sufficient.
- Drag/drop reordering or grouping of folders.
- Color-coding or icons per folder.
- Search by full path inside the picker (today's search filters on the rendered label only, which now contains enough of the path to make typing fragments useful anyway).
- Renaming folders via the MCP API or CLI — alias is a UI-driven operator feature.
- Cross-device sync of aliases (single-tenant; alias is a server-side DB column).

---

## Piece 1 — Popover positioning (bugfix)

**Problem.** `FolderPicker.tsx` renders its popover with `absolute top-full`. When the trigger sits within `popoverHeight + margin` pixels of the viewport bottom (the Ask chat input footer is exactly this case), the popover extends below the visible area. The component's parent has `overflow-hidden` on the chat-input wrapper, clipping the popover further. The folder list rows are unreachable.

**Fix.** On open, measure the trigger's `getBoundingClientRect()` and the viewport height; choose the popover direction based on available space.

```tsx
const triggerRef = useRef<HTMLButtonElement>(null);
const [direction, setDirection] = useState<'down' | 'up'>('down');

useLayoutEffect(() => {
  if (!open || !triggerRef.current) return;
  const rect = triggerRef.current.getBoundingClientRect();
  const popoverMaxHeight = 360; // matches the max-h cap below
  const margin = 8;
  const spaceBelow = window.innerHeight - rect.bottom - margin;
  const spaceAbove = rect.top - margin;
  setDirection(
    spaceBelow >= popoverMaxHeight || spaceBelow >= spaceAbove ? 'down' : 'up'
  );
}, [open]);
```

**Class binding.**

```tsx
<div
  className={
    direction === 'up'
      ? 'absolute bottom-full mb-1 ...'   // open upward
      : 'absolute top-full mt-1 ...'      // open downward (today's behavior)
  }
  style={{ maxHeight: 360 }}
  ...
>
  ...
</div>
```

The popover list itself remains scrollable internally (`max-h-48 overflow-y-auto` on the `<ul>` stays the same). Capping the outer popover at 360px and choosing direction by available space handles every viewport position without needing a portal/floating-ui dependency.

**Tests.** The existing `FolderPicker.test.tsx` doesn't measure layout, so no positioning test is added at the unit level. Manual verification: open Ask at full window height, open the picker — list visible; resize the window so the input is at the very bottom — picker now flips upward and is fully visible.

---

## Piece 2 — Label disambiguation

**Problem.** `WatchedFolder.display_name` defaults to `Path(path).name`. Multiple folders sharing a basename (`ingest`) render identically in both `FolderPicker` and `ScopeChip`.

**Fix.** A new pure function `disambiguateLabels(folders): Map<folder_id, string>` builds a unique label per folder. Both components consume it.

**Algorithm:**

1. For each folder, candidate label is `display_name` (the user-controlled string; `null` falls back to `Path(path).name`).
2. Group folders by candidate label.
3. For groups of size 1: the candidate is the final label.
4. For groups of size > 1: for each folder in the group, extend the label by prepending its parent directory segment. Repeat until each folder in the group has a unique extended label.
5. Folders outside any colliding group keep their candidate label.

Path traversal uses `path.split('/').filter(Boolean)` from the rightmost segment back. If two paths share a tail prefix (e.g., `a/cuad/ingest` and `b/a/cuad/ingest`), the algorithm walks farther up until the prefixes diverge. In the extreme degenerate case where two paths are identical at every depth (shouldn't happen — paths are filesystem-unique), the algorithm falls back to appending the folder UUID's last 6 chars.

**Implementation location:** `frontend/src/components/folderLabels.ts` (new file). Single export, fully unit-testable, no React imports.

**Consumers:**
- `FolderPicker.tsx`: replaces the inline `summarize()` and the inline `f.display_name ?? f.folder_id` in row rendering.
- `ScopeChip.tsx`: replaces its inline `display_name ?? '?'` mapping.

**Tests** (in `folderLabels.test.ts`):

- No collisions → labels match display_name exactly.
- Two folders both named "ingest" with parents `cuad` and `qwen3-20b/cuad` → `cuad/ingest` and `qwen3-20b/cuad/ingest`.
- Three folders all named "ingest" with parents `cuad`, `qwen3-20b/cuad`, `gpt-oss-20b/cuad` → unique prefixes for each.
- Mix of unique and colliding: unique ones keep simple labels, colliding group disambiguates.
- Empty/null `display_name` falls back to `Path(path).name`.
- Degenerate: two folders with identical paths → falls back to UUID suffix.

---

## Piece 3 — Tooltips with full path

**Implementation.** Native browser `title` attribute on every picker row and on the chip span:

```tsx
// FolderPicker.tsx row:
<label className="..." title={f.path}>
  <input type="checkbox" ... aria-label={labelMap.get(f.folder_id)} />
  {labelMap.get(f.folder_id)}
</label>

// ScopeChip.tsx:
<span className={...} title={folder_ids.map(id => folders.find(...)?.path).filter(Boolean).join('\n')}>
  Folders: {/* disambiguated names */}
</span>
```

The chip's title shows one path per line for the selected folders (when multiple). No JS tooltip library — the native browser one is fine for low-information disclosure like this.

---

## Piece 4 — Editable alias on the Folders tab

### Backend

**Extend `FolderPatch`** in `src/harbor_clerk/api/schemas/watch.py` (or wherever it's defined) to include `display_name: str | None = None`. Add validation:

- Strip whitespace.
- Length 1–200 (matching `conversations.title` convention).
- An empty/whitespace-only string is treated as "revert to default" — the handler writes `Path(folder.path).name`.

**Handler change** in `src/harbor_clerk/api/routes/watch.py:387` (`patch_folder`):

```python
if body.display_name is not None:
    new_name = body.display_name.strip()
    if not new_name:
        # Empty alias → revert to default (path basename)
        new_name = Path(folder.path).name
    if len(new_name) > 200:
        raise HTTPException(status_code=422, detail="display_name exceeds 200 chars")
    folder.display_name = new_name
```

(`None` continues to mean "field absent in patch" — no change.)

### Frontend

**`FoldersPage.tsx`** — find the row rendering for each folder. Add a small pencil icon adjacent to the displayed name; clicking it converts the cell to a controlled text input.

```tsx
const [editingId, setEditingId] = useState<string | null>(null);
const [draft, setDraft] = useState('');

// In each row:
{editingId === folder.folder_id ? (
  <input
    autoFocus
    value={draft}
    onChange={(e) => setDraft(e.target.value)}
    onKeyDown={(e) => {
      if (e.key === 'Enter') saveAlias(folder, draft);
      if (e.key === 'Escape') setEditingId(null);
    }}
    onBlur={() => saveAlias(folder, draft)}
  />
) : (
  <>
    <span title={folder.path}>{folder.display_name}</span>
    <button onClick={() => { setDraft(folder.display_name ?? ''); setEditingId(folder.folder_id); }}>
      <PencilIcon />
    </button>
  </>
)}
```

`saveAlias` calls `PATCH /api/watch/folders/{folder_id}` with `{display_name: draft}` and on success invalidates the `['watch', 'folders']` query (via TanStack's `queryClient.invalidateQueries`).

**Path display on the row.** Add the full path as secondary text below the folder name (matching the spec's preview):

```
[CUAD test set ✎]
  ~/work/cuad/ingest                ← muted, smaller font
```

This way the operator can always see what the alias points at without hover. (The pencil-edit interaction handles renames; the path text is read-only.)

### Refresh

The TanStack Query cache key for the folders list is `['watch', 'folders']`. After a successful PATCH, invalidate that key. The picker, chip, and Folders page all subscribe to the same cache so they refresh consistently. Conversations and research runs already store `folder_ids` (UUIDs), not labels, so a rename has zero impact on their stored scope — it only changes what's rendered.

---

## Edge cases

| Scenario | Behavior |
|---|---|
| User aliases two folders to the same name ("Receipts" both) | Both get the alias as their candidate; `disambiguateLabels` treats them as a collision and prepends path segments, yielding `parent/Receipts` for each. |
| User clears the alias (submits empty) | Handler writes `Path(folder.path).name`. Folder reverts to the default basename. Auto-disambig still kicks in if that basename collides. |
| User aliases to a string longer than 200 chars | 422 from the API. Frontend shows the error inline and keeps the input open. |
| User aliases to a single character | Allowed. Length 1–200 is the only constraint. |
| Folder is unavailable (`unavailable_reason IS NOT NULL`) and user tries to rename | Allowed. Rename is metadata; doesn't depend on filesystem accessibility. The folder is hidden from the picker anyway (filtered by `useWatchedFolders` on `unavailable_reason === null`). |
| Aliased folder is then deleted | Standard delete path; the alias goes with the row. |
| New folder auto-discovered after Docker mount | Receives `display_name = Path(path).name` as today. If it collides, `disambiguateLabels` handles it without operator action. Operator can alias later. |
| Two folders have the same path (shouldn't happen) | Degenerate; the algorithm appends the folder UUID's last 6 chars to make labels unique. |
| `useWatchedFolders` is still loading when the picker opens | `folders` is `[]`; the picker shows "No folders to scope to" until the fetch completes. Existing behavior; unchanged. |

---

## Testing

### Frontend unit

- `folderLabels.test.ts` covers the 6 algorithm cases above.
- `FolderPicker.test.tsx` gains 1 test: when given two folders with identical `display_name`, the rendered row labels are the disambiguated forms (not the bare collision).
- `ScopeChip.test.tsx` gains 1 test: same disambiguation behavior in the chip.
- `FolderPicker.test.tsx` gains 1 test for the `title` attribute carrying the full path.

### Frontend integration (Folders tab)

- Existing FoldersPage tests (if any) gain a test for the inline pencil-edit interaction:
  - Click pencil → input appears with current name.
  - Type new name → Enter → calls PATCH with the new name.
  - Empty string + Enter → PATCH succeeds; folder name reverts to path basename.
  - Escape → closes the edit, no PATCH.
- If FoldersPage has no test file yet, this task does not add one — the alias UI is small enough that manual verification is fine. (A future test would also exercise the loading/error states.)

### Backend

- `tests/test_watch_routes.py` (or wherever PATCH `/folders/{id}` is tested today) gains 3 tests:
  - PATCH with `display_name="My Folder"` updates the row and 200s.
  - PATCH with `display_name=""` writes `Path(path).name` (revert).
  - PATCH with `display_name="x" * 201` → 422.

### Positioning (manual)

- Open Ask at full window height, click the chip → popover opens downward, list visible.
- Shrink the window until the chat input is near the bottom → popover flips upward.
- Same on Research start form (mid-page) and Search filter row (top of page) — popover opens downward in both because there's more room below.

---

## Migration

No schema change. `WatchedFolder.display_name` is already `Mapped[str | None]` with no default — the column accepts both user aliases and the existing `Path(path).name` default. The PATCH endpoint already exists; only the request schema and handler body change.

---

## Out of scope (explicit)

- Slash-aware path display in tooltips (showing `~/work/cuad/ingest` rather than full absolute path) — could be a polish pass later. For now we show the raw `path` field as the backend stores it.
- Bulk rename / find-and-replace across many folders.
- Per-conversation folder name overrides ("for THIS conversation, call this folder 'Contracts'") — out of scope; folders have one canonical alias.
- Reordering folders. The picker list is alphabetical by candidate label.
- Showing the alias on the Documents page document list (where folder is sometimes referenced). Defer until users ask for it.

---

## Forward compat

If we later add a `path_display` column (e.g. for showing the home-relative form `~/foo`) or a `color` column or icons, none of this work blocks it. `disambiguateLabels` is keyed off `display_name + path` only; it doesn't care about new columns.

If we ever ship a per-conversation folder pinning feature (the speculative "label this folder 'Active' for the duration of this conversation"), it would live on the Conversation, not on the WatchedFolder. The shared `disambiguateLabels` algorithm continues to work for the global aliases.

## References

- PR #415 — the folder-scope feature that introduced the picker
- `WatchedFolder.display_name` model field: `src/harbor_clerk/models/watched.py:28`
- Current PATCH handler: `src/harbor_clerk/api/routes/watch.py:386`
- Existing FolderPicker: `frontend/src/components/FolderPicker.tsx`
- Existing ScopeChip: `frontend/src/components/ScopeChip.tsx`

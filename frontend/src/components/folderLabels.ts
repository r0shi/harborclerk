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

    // Disambiguate in two phases:
    //
    // Phase 1 — find the minimum uniform depth D at which every folder in the
    // group has a distinct label. This is the smallest D where no two folders
    // share the same last-D path segments.
    //
    // Phase 2 — trim: for each folder independently, find the smallest depth
    // d (1 ≤ d ≤ D) such that labelAt(f, d) does not appear in the set of
    // sibling labels computed at the full depth D. Shorter paths naturally
    // end up with fewer segments because their early suffixes are already unique
    // against the longer-path siblings' depth-D labels.
    const maxDepth = Math.max(...group.map((f) => pathSegments(f.path).length))

    function labelAt(f: FolderLike, depth: number): string {
      const segs = pathSegments(f.path)
      const cand = candidate(f)
      // If candidate is a custom alias (differs from basename), keep it as
      // the leaf and prepend path segments above the actual basename.
      const segsForLabel = cand === basename(f.path) ? segs.slice(-depth) : [...segs.slice(-depth, -1), cand]
      return segsForLabel.join('/')
    }

    // Phase 1: find minimum uniform depth where all labels are distinct.
    let resolvedDepth = 1
    for (let d = 1; d <= maxDepth; d++) {
      const seen = new Set<string>()
      let allUnique = true
      for (const f of group) {
        const lbl = labelAt(f, d)
        if (seen.has(lbl)) {
          allUnique = false
          break
        }
        seen.add(lbl)
      }
      if (allUnique) {
        resolvedDepth = d
        break
      }
      resolvedDepth = d // keep advancing even if not unique (fallback)
    }

    // The sibling labels at the resolved depth — used as the comparison set.
    const siblingLabelsAtD = new Map<string, string>()
    for (const f of group) {
      siblingLabelsAtD.set(f.folder_id, labelAt(f, resolvedDepth))
    }

    // Phase 2: assign labels greedily, processing folders with longer paths
    // first (they need more segments to distinguish themselves). Each folder
    // gets the shortest depth (≥ 2) whose label doesn't match any
    // already-assigned label. Shorter-path folders benefit because the longer
    // siblings have already "claimed" their deeper labels, leaving the shorter
    // suffixes free.
    const groupLabels = new Map<string, string>()
    const assigned = new Set<string>() // labels already locked in

    // Sort shorter paths first: they get priority on shorter suffixes.
    const sorted = [...group].sort((a, b) => pathSegments(a.path).length - pathSegments(b.path).length)

    for (const f of sorted) {
      let chosen = siblingLabelsAtD.get(f.folder_id)!
      // Try from depth 2 upward; take the first depth whose label isn't taken.
      for (let d = 2; d <= resolvedDepth; d++) {
        const lbl = labelAt(f, d)
        if (!assigned.has(lbl)) {
          chosen = lbl
          break
        }
      }
      groupLabels.set(f.folder_id, chosen)
      assigned.add(chosen)
    }

    // If still collisions (e.g., identical paths), fall back to UUID suffix.
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

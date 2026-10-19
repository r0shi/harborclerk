// Operator guides live in the repository, not in the SPA: a relative `/docs/...`
// link is routed to DocumentDetailPage (`/docs/:id`) and shows an error page.
// Open these in a new tab (the macOS client hands new-window links to the
// default browser).

const REPO_BLOB = 'https://github.com/r0shi/harborclerk/blob'

// `/api/system/health` reports a bare `X.Y.Z` only for a stamped release build
// (docs/releasing.md); anything else (`0.9.3+2.g887c3fb`, `0.9.3+unstamped`,
// which is what Docker images report today) is not a tag.
const RELEASE_VERSION = /^\d+\.\d+\.\d+$/

/** A repository doc at the running release's tag, or on main for any other build. */
export function repoDocUrl(path: string, version?: string | null): string {
  const ref = version && RELEASE_VERSION.test(version) ? `v${version}` : 'main'
  return `${REPO_BLOB}/${ref}/${path}`
}

export function watchedFoldersDockerGuideUrl(version?: string | null): string {
  return repoDocUrl('docs/watched-folders-docker.md', version)
}

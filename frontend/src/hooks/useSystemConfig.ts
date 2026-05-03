import { useEffect, useState } from 'react'

export interface SystemConfig {
  /**
   * True when the backend is configured to serve original document bytes
   * over /api/docs/{doc_id}/download. Defaults to false on every deployment;
   * macOS native does not expose any toggle to flip this on (use Reveal in
   * Finder instead). On Docker, an admin can opt in via ALLOW_SOURCE_DOWNLOAD.
   */
  allowSourceDownload: boolean
  /**
   * True once the system config has been fetched at least once. While false
   * the UI should treat capability flags conservatively (e.g. hide buttons
   * rather than show them only to have them flicker off when the response
   * arrives).
   */
  loaded: boolean
}

const FALLBACK: SystemConfig = { allowSourceDownload: false, loaded: false }

/**
 * Fetches /api/system/health once on mount to read deployment-wide
 * capabilities. Used by DocumentDetailPage to decide whether to render the
 * Download button.
 *
 * Not a global context — the page-level cost is one fetch on mount,
 * negligible. If a future page needs the config too and we end up making
 * the same call from multiple components, lifting this into a context is
 * the obvious refactor.
 */
export function useSystemConfig(): SystemConfig {
  const [config, setConfig] = useState<SystemConfig>(FALLBACK)

  useEffect(() => {
    let cancelled = false
    fetch('/api/system/health')
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (cancelled || !data) return
        setConfig({
          allowSourceDownload: Boolean(data.allow_source_download),
          loaded: true,
        })
      })
      .catch(() => {
        // Health endpoint is unauthenticated and very rarely fails. If it
        // does, leave the conservative default in place — a failure to
        // fetch capability shouldn't be interpreted as "everything is
        // available". The user just sees a slightly less complete UI.
      })
    return () => {
      cancelled = true
    }
  }, [])

  return config
}

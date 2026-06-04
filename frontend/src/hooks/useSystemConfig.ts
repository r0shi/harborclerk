import { useEffect, useState } from 'react'

export interface SystemConfig {
  /**
   * Readiness status reported by /api/system/health. This is a coarse
   * at-a-glance signal; the Status page remains the detailed diagnostic view.
   */
  healthStatus: 'healthy' | 'degraded' | null
  /**
   * True when the backend is configured to serve original document bytes
   * over /api/docs/{doc_id}/download. Defaults to false on every deployment;
   * macOS native does not expose any toggle to flip this on (use Reveal in
   * Finder instead). On Docker, an admin can opt in via ALLOW_SOURCE_DOWNLOAD.
   */
  allowSourceDownload: boolean
  /**
   * True when the backend has CLI/agentic-harness access enabled. Controls
   * whether the Integrations page shows the CLI access section and API key
   * management UI for programmatic clients.
   */
  enableCliAccess: boolean
  /**
   * macOS native only. One of "installed", "installed_outdated", "not_installed",
   * or null when running on Docker/Linux (shim concept doesn't apply there).
   */
  cliShimInstallStatus: 'installed' | 'installed_outdated' | 'not_installed' | null
  /**
   * True once the system config has been fetched at least once. While false
   * the UI should treat capability flags conservatively (e.g. hide buttons
   * rather than show them only to have them flicker off when the response
   * arrives).
   */
  loaded: boolean
}

const FALLBACK: SystemConfig = {
  healthStatus: null,
  allowSourceDownload: false,
  enableCliAccess: false,
  cliShimInstallStatus: null,
  loaded: false,
}

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
          healthStatus: data.status === 'healthy' || data.status === 'degraded' ? data.status : null,
          allowSourceDownload: Boolean(data.allow_source_download),
          enableCliAccess: Boolean(data.enable_cli_access),
          cliShimInstallStatus: data.cli_shim_install_status ?? null,
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

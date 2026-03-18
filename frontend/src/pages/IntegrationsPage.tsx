import { useCallback, useEffect, useState } from 'react'
import { del, get, put } from '../api'
import { Link } from 'react-router-dom'

interface IntegrationSettings {
  public_url: string
  oauth_refresh_token_days: number
}

interface OAuthConnection {
  client_id: string
  client_name: string
  is_active: boolean
  last_used_at: string | null
  created_at: string
}

const TOKEN_LIFETIME_OPTIONS = [30, 60, 90, 120, 365]

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false)

  async function handleCopy() {
    await navigator.clipboard.writeText(text)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }

  return (
    <button
      onClick={handleCopy}
      className="ml-2 shrink-0 rounded-md bg-(--color-bg-secondary) px-2 py-1 text-xs font-medium text-(--color-text-secondary) hover:bg-black/6 dark:hover:bg-white/10 transition-colors"
    >
      {copied ? 'Copied!' : 'Copy'}
    </button>
  )
}

function CodeBlock({ children }: { children: string }) {
  return (
    <div className="relative mt-2 rounded-lg bg-(--color-bg-secondary) dark:bg-(--color-bg-tertiary) p-3 font-mono text-sm text-(--color-text-primary) overflow-x-auto">
      <div className="absolute top-2 right-2">
        <CopyButton text={children} />
      </div>
      <pre className="whitespace-pre-wrap pr-16">{children}</pre>
    </div>
  )
}

const cardClass = 'rounded-xl bg-white dark:bg-[#2c2c2e] shadow-mac ring-1 ring-(--color-border) p-6'

export default function IntegrationsPage() {
  const [settings, setSettings] = useState<IntegrationSettings>({ public_url: '', oauth_refresh_token_days: 90 })
  const [connections, setConnections] = useState<OAuthConnection[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)
  const [urlDraft, setUrlDraft] = useState('')
  const [guideTab, setGuideTab] = useState<'chatgpt' | 'claude' | 'gemini'>('chatgpt')

  const loadData = useCallback(async () => {
    try {
      const [s, c] = await Promise.all([
        get<IntegrationSettings>('/api/integrations/settings'),
        get<OAuthConnection[]>('/api/integrations/connections'),
      ])
      setSettings(s)
      setUrlDraft(s.public_url)
      setConnections(c)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    loadData()
  }, [loadData])

  async function saveUrl() {
    setSaving(true)
    setError('')
    try {
      await put('/api/integrations/settings', { public_url: urlDraft })
      setSettings((prev) => ({ ...prev, public_url: urlDraft }))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Save failed')
    } finally {
      setSaving(false)
    }
  }

  async function saveLifetime(days: number) {
    setError('')
    try {
      await put('/api/integrations/settings', { oauth_refresh_token_days: days })
      setSettings((prev) => ({ ...prev, oauth_refresh_token_days: days }))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Save failed')
    }
  }

  async function revokeConnection(clientId: string) {
    try {
      await del(`/api/integrations/connections/${clientId}`)
      setConnections((prev) => prev.filter((c) => c.client_id !== clientId))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Revoke failed')
    }
  }

  const mcpUrl = settings.public_url
    ? `${settings.public_url.replace(/\/$/, '')}/mcp`
    : 'https://your-server.example.com/mcp'

  if (loading) return <div className="text-gray-500 dark:text-gray-400">Loading...</div>

  return (
    <div className="animate-slide-in space-y-6">
      <h1 className="text-xl font-bold">Integrations</h1>

      {error && (
        <div className="rounded-lg bg-red-50 dark:bg-red-900/20 px-3 py-2 text-sm text-red-700 dark:text-red-400">
          {error}
        </div>
      )}

      {/* Connection Settings */}
      <div className={cardClass}>
        <h2 className="text-lg font-semibold mb-4">Connection Settings</h2>

        <div className="space-y-4">
          <div>
            <label className="mb-1 block text-sm font-medium text-(--color-text-primary)">Public URL</label>
            <p className="mb-2 text-xs text-(--color-text-secondary)">
              The public HTTPS URL where this server is reachable from the internet. Required for OAuth-based
              connections like ChatGPT. Use a reverse proxy or tunnel (e.g. Cloudflare Tunnel) to expose your local
              server.
            </p>
            <div className="flex items-center gap-2">
              <input
                type="url"
                value={urlDraft}
                onChange={(e) => setUrlDraft(e.target.value)}
                placeholder="https://clerk.example.com"
                className="flex-1 rounded-lg border-0 bg-(--color-bg-secondary) dark:bg-(--color-bg-tertiary) shadow-mac focus:ring-2 focus:ring-(--color-accent)/30 px-3 py-1.5 text-sm"
              />
              <button
                onClick={saveUrl}
                disabled={saving || urlDraft === settings.public_url}
                className="rounded-lg bg-blue-600 px-4 py-1.5 text-sm font-medium text-white shadow-xs hover:bg-blue-700 disabled:opacity-50"
              >
                {saving ? 'Saving...' : 'Save'}
              </button>
            </div>
          </div>

          <div>
            <label className="mb-1 block text-sm font-medium text-(--color-text-primary)">Token Lifetime</label>
            <p className="mb-2 text-xs text-(--color-text-secondary)">
              How long OAuth tokens remain valid before the external tool must reconnect.
            </p>
            <select
              value={settings.oauth_refresh_token_days}
              onChange={(e) => saveLifetime(Number(e.target.value))}
              className="rounded-lg border-0 bg-(--color-bg-secondary) dark:bg-(--color-bg-tertiary) shadow-mac focus:ring-2 focus:ring-(--color-accent)/30 px-3 py-1.5 text-sm"
            >
              {TOKEN_LIFETIME_OPTIONS.map((d) => (
                <option key={d} value={d}>
                  {d} days
                </option>
              ))}
            </select>
          </div>
        </div>
      </div>

      {/* Active Connections */}
      {connections.length === 0 ? (
        <div className={cardClass}>
          <h2 className="text-lg font-semibold mb-4">Active Connections</h2>
          <p className="text-sm text-(--color-text-secondary)">No external AI tools connected yet.</p>
        </div>
      ) : (
        <div className={cardClass}>
          <details>
            <summary className="text-lg font-semibold cursor-pointer">
              Active Connections ({connections.length})
            </summary>
            <div className="mt-4 overflow-hidden rounded-lg ring-1 ring-(--color-border)">
              <table className="min-w-full divide-y divide-(--color-border)">
                <thead className="bg-(--color-bg-secondary)">
                  <tr>
                    <th className="px-4 py-2.5 text-left text-xs font-medium uppercase text-gray-500 dark:text-gray-400">
                      Status
                    </th>
                    <th className="px-4 py-2.5 text-left text-xs font-medium uppercase text-gray-500 dark:text-gray-400">
                      Client
                    </th>
                    <th className="px-4 py-2.5 text-left text-xs font-medium uppercase text-gray-500 dark:text-gray-400">
                      Connected
                    </th>
                    <th className="px-4 py-2.5 text-left text-xs font-medium uppercase text-gray-500 dark:text-gray-400">
                      Last Used
                    </th>
                    <th className="px-4 py-2.5 text-right text-xs font-medium uppercase text-gray-500 dark:text-gray-400">
                      Actions
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-(--color-border)">
                  {connections.map((c) => (
                    <tr key={c.client_id} className="hover:bg-black/3 dark:hover:bg-white/3">
                      <td className="px-4 py-3">
                        <span
                          className={`inline-block h-2.5 w-2.5 rounded-full ${c.is_active ? 'bg-green-500' : 'bg-gray-400'}`}
                        />
                      </td>
                      <td className="px-4 py-3 text-sm font-medium">{c.client_name}</td>
                      <td className="px-4 py-3 text-sm text-gray-500 dark:text-gray-400">
                        {new Date(c.created_at).toLocaleDateString()}
                      </td>
                      <td className="px-4 py-3 text-sm text-gray-500 dark:text-gray-400">
                        {c.last_used_at ? new Date(c.last_used_at).toLocaleString() : 'Never'}
                      </td>
                      <td className="px-4 py-3 text-right">
                        <button
                          onClick={() => revokeConnection(c.client_id)}
                          className="text-sm text-red-600 dark:text-red-400 hover:underline"
                        >
                          Revoke
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </details>
        </div>
      )}

      {/* Connection Guides */}
      <div className={cardClass}>
        <div className="grid grid-cols-3 gap-3 mb-6">
          {(['chatgpt', 'claude', 'gemini'] as const).map((tab) => {
            const info = {
              chatgpt: { label: 'ChatGPT', desc: 'OAuth \u00b7 browser-based', icon: '\ud83c\udf10' },
              claude: { label: 'Claude', desc: 'API key \u00b7 desktop & CLI', icon: '\ud83d\udcbb' },
              gemini: { label: 'Gemini CLI', desc: 'API key \u00b7 terminal', icon: '\u2328\ufe0f' },
            }[tab]
            const active = guideTab === tab
            return (
              <button
                key={tab}
                onClick={() => setGuideTab(tab)}
                className={`rounded-xl px-4 py-3 text-left transition-all ${
                  active
                    ? 'bg-blue-50 dark:bg-blue-900/20 ring-2 ring-blue-500/50 dark:ring-blue-400/40 shadow-sm'
                    : 'bg-(--color-bg-secondary) dark:bg-(--color-bg-tertiary) ring-1 ring-(--color-border) hover:ring-blue-300/50 dark:hover:ring-blue-600/30'
                }`}
              >
                <div className="text-lg mb-0.5">{info.icon}</div>
                <div
                  className={`text-sm font-semibold ${active ? 'text-blue-700 dark:text-blue-300' : 'text-(--color-text-primary)'}`}
                >
                  {info.label}
                </div>
                <div className="text-xs text-(--color-text-secondary)">{info.desc}</div>
              </button>
            )
          })}
        </div>

        {guideTab === 'chatgpt' && (
          <>
            <ol className="list-decimal list-inside space-y-3 text-sm text-(--color-text-primary)">
              <li>
                Set your <strong>Public URL</strong> above. Your server must be reachable over HTTPS from the internet.
                If running locally, use a tunnel such as{' '}
                <a
                  href="https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-blue-600 dark:text-blue-400 hover:underline"
                >
                  Cloudflare Tunnel
                </a>
                .
              </li>
              <li>
                In ChatGPT, go to <strong>Settings &rarr; Connected Apps</strong> and add a new MCP server.
              </li>
              <li>Enter the following MCP URL:</li>
            </ol>
            <CodeBlock>{mcpUrl}</CodeBlock>
            <p className="mt-3 text-xs text-(--color-text-secondary)">
              ChatGPT will redirect you to authorize the connection via OAuth. Once approved, it appears in Active
              Connections above.
            </p>
          </>
        )}

        {guideTab === 'claude' && (
          <>
            <p className="mb-3 text-sm text-(--color-text-primary)">
              Claude Desktop and Claude Code use API key authentication instead of OAuth.
            </p>
            <ol className="list-decimal list-inside space-y-3 text-sm text-(--color-text-primary)">
              <li>
                <Link to="/admin/keys" className="text-blue-600 dark:text-blue-400 hover:underline">
                  Create an API key
                </Link>{' '}
                if you don&apos;t have one already.
              </li>
              <li>
                For <strong>Claude Desktop</strong>, add this to your{' '}
                <code className="rounded bg-(--color-bg-secondary) px-1.5 py-0.5 text-xs">
                  claude_desktop_config.json
                </code>
                :
              </li>
            </ol>
            <CodeBlock>
              {JSON.stringify(
                {
                  mcpServers: {
                    'harbor-clerk': {
                      url: `${mcpUrl}?key=YOUR_API_KEY`,
                    },
                  },
                },
                null,
                2,
              )}
            </CodeBlock>
            <p className="mt-4 text-sm text-(--color-text-primary)">
              For <strong>Claude Code</strong>, run:
            </p>
            <CodeBlock>{`claude mcp add harbor-clerk "${mcpUrl}?key=YOUR_API_KEY"`}</CodeBlock>
            <p className="mt-3 text-xs text-(--color-text-secondary)">
              Replace <code className="rounded bg-(--color-bg-secondary) px-1 py-0.5">YOUR_API_KEY</code> with the key
              you created.
            </p>
          </>
        )}

        {guideTab === 'gemini' && (
          <>
            <ol className="list-decimal list-inside space-y-3 text-sm text-(--color-text-primary)">
              <li>
                <Link to="/admin/keys" className="text-blue-600 dark:text-blue-400 hover:underline">
                  Create an API key
                </Link>{' '}
                if you don&apos;t have one already.
              </li>
              <li>
                Add this to your Gemini CLI settings file (
                <code className="rounded bg-(--color-bg-secondary) px-1.5 py-0.5 text-xs">~/.gemini/settings.json</code>
                ):
              </li>
            </ol>
            <CodeBlock>
              {JSON.stringify(
                {
                  mcpServers: {
                    'harbor-clerk': {
                      uri: `${mcpUrl}?key=YOUR_API_KEY`,
                    },
                  },
                },
                null,
                2,
              )}
            </CodeBlock>
            <p className="mt-3 text-xs text-(--color-text-secondary)">
              Replace <code className="rounded bg-(--color-bg-secondary) px-1 py-0.5">YOUR_API_KEY</code> with the key
              you created.
            </p>
          </>
        )}
      </div>
    </div>
  )
}

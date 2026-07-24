import { FormEvent, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router'
import { del, get, patch, post } from '../api'
import { PageHeader } from '../components/PageHeader'
import { Card } from '../components/Card'

type PermissionTier = 'search' | 'read' | 'full'

interface ApiKeyInfo {
  key_id: string
  name: string
  is_active: boolean
  created_at: string
  last_used_at?: string
  expires_at: string | null
  permission_tier: PermissionTier
  tool_overrides: Record<string, boolean>
  scope_topic_ids: number[] | null
  scope_folder_ids: string[] | null
  max_snippet_chars: number | null
  scope_summary: string
  rate_limit_rpm: number | null
  rate_limit_rph: number | null
}

interface ApiKeyCreated {
  key_id: string
  name: string
  raw_key: string
  mcp_path: string
  created_at: string
}

interface TopicCluster {
  cluster_id: number
  name: string
}

interface WatchedFolderInfo {
  folder_id: string
  path: string
}

interface ScopePreview {
  accessible_documents: number
  total_documents: number
}

// Mirrors src/harbor_clerk/api/scope.py — keep in sync
const SEARCH_TIER_TOOLS = [
  'kb_search',
  'kb_batch_search',
  'kb_corpus_overview',
  'kb_list_recent',
  'kb_find_all',
  'kb_documents_by_date',
] as const
const READ_TIER_TOOLS = [
  ...SEARCH_TIER_TOOLS,
  'kb_read_passages',
  'kb_expand_context',
  'kb_document_outline',
  'kb_get_document',
  'kb_verify_identifier',
] as const
const FULL_TIER_TOOLS = [
  ...READ_TIER_TOOLS,
  'kb_read_document',
  'kb_find_related',
  'kb_entity_search',
  'kb_entity_overview',
  'kb_entity_cooccurrence',
  'kb_ingest_status',
] as const

const ALL_TOOLS: readonly string[] = FULL_TIER_TOOLS

function tierBaseTools(tier: PermissionTier): ReadonlySet<string> {
  if (tier === 'search') return new Set(SEARCH_TIER_TOOLS)
  if (tier === 'read') return new Set(READ_TIER_TOOLS)
  return new Set(FULL_TIER_TOOLS)
}

function toolEnabled(tool: string, tier: PermissionTier, overrides: Record<string, boolean>): boolean {
  if (tool in overrides) return overrides[tool]
  return tierBaseTools(tier).has(tool)
}

function toggleToolOverride(
  tool: string,
  nextChecked: boolean,
  tier: PermissionTier,
  overrides: Record<string, boolean>,
): Record<string, boolean> {
  const inTier = tierBaseTools(tier).has(tool)
  const next = { ...overrides }
  if (nextChecked === inTier) {
    delete next[tool]
  } else {
    next[tool] = nextChecked
  }
  return next
}

interface ScopeFormState {
  expiresAt: string // yyyy-mm-dd or ''
  tier: PermissionTier
  toolOverrides: Record<string, boolean>
  topicIds: number[] | null
  folderIds: string[] | null
  maxSnippetChars: string // raw input string
  rateLimitRpmUnlimited: boolean
  rateLimitRpm: string // '' = system default, number string = custom
  rateLimitRphUnlimited: boolean
  rateLimitRph: string
}

function emptyScopeState(): ScopeFormState {
  return {
    expiresAt: '',
    tier: 'full',
    toolOverrides: {},
    topicIds: null,
    folderIds: null,
    maxSnippetChars: '',
    rateLimitRpmUnlimited: false,
    rateLimitRpm: '',
    rateLimitRphUnlimited: false,
    rateLimitRph: '',
  }
}

function scopeStateFromKey(k: ApiKeyInfo): ScopeFormState {
  return {
    expiresAt: k.expires_at ? k.expires_at.slice(0, 10) : '',
    tier: k.permission_tier,
    toolOverrides: { ...k.tool_overrides },
    topicIds: k.scope_topic_ids,
    folderIds: k.scope_folder_ids,
    maxSnippetChars: k.max_snippet_chars != null ? String(k.max_snippet_chars) : '',
    rateLimitRpmUnlimited: k.rate_limit_rpm === 0,
    rateLimitRpm: k.rate_limit_rpm != null && k.rate_limit_rpm > 0 ? String(k.rate_limit_rpm) : '',
    rateLimitRphUnlimited: k.rate_limit_rph === 0,
    rateLimitRph: k.rate_limit_rph != null && k.rate_limit_rph > 0 ? String(k.rate_limit_rph) : '',
  }
}

interface ScopePayload {
  expires_at: string | null
  permission_tier: PermissionTier
  tool_overrides: Record<string, boolean>
  scope_topic_ids: number[] | null
  scope_folder_ids: string[] | null
  max_snippet_chars: number | null
  rate_limit_rpm: number | null
  rate_limit_rph: number | null
}

function scopeStateToPayload(s: ScopeFormState): ScopePayload {
  const maxSnippet = s.maxSnippetChars.trim() ? Number(s.maxSnippetChars) : null
  return {
    expires_at: s.expiresAt ? s.expiresAt + 'T23:59:59Z' : null,
    permission_tier: s.tier,
    tool_overrides: s.toolOverrides,
    scope_topic_ids: s.topicIds,
    scope_folder_ids: s.folderIds,
    max_snippet_chars: maxSnippet != null && Number.isFinite(maxSnippet) && maxSnippet > 0 ? maxSnippet : null,
    rate_limit_rpm: s.rateLimitRpmUnlimited ? 0 : s.rateLimitRpm.trim() ? Number(s.rateLimitRpm) : null,
    rate_limit_rph: s.rateLimitRphUnlimited ? 0 : s.rateLimitRph.trim() ? Number(s.rateLimitRph) : null,
  }
}

export default function ApiKeysPage() {
  const [keys, setKeys] = useState<ApiKeyInfo[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [showCreate, setShowCreate] = useState(false)
  const [newKey, setNewKey] = useState<ApiKeyCreated | null>(null)
  const [editingKey, setEditingKey] = useState<ApiKeyInfo | null>(null)

  const [topics, setTopics] = useState<TopicCluster[]>([])
  const [folders, setFolders] = useState<WatchedFolderInfo[]>([])

  async function loadKeys() {
    try {
      const data = await get<ApiKeyInfo[]>('/api/api-keys')
      setKeys(data)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load keys')
    } finally {
      setLoading(false)
    }
  }

  async function loadScopeOptions() {
    try {
      const [t, f] = await Promise.all([
        get<{ clusters: TopicCluster[] }>('/api/stats/topics').catch(() => ({ clusters: [] })),
        get<WatchedFolderInfo[]>('/api/watch/folders').catch(() => [] as WatchedFolderInfo[]),
      ])
      setTopics(t.clusters || [])
      setFolders(Array.isArray(f) ? f : [])
    } catch {
      // non-fatal
    }
  }

  useEffect(() => {
    async function run() {
      await loadKeys()
      await loadScopeOptions()
    }
    run()
  }, [])

  async function handleRevoke(keyId: string) {
    if (!confirm('Revoke this API key? This cannot be undone.')) return
    try {
      await del(`/api/api-keys/${keyId}`)
      await loadKeys()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Revoke failed')
    }
  }

  if (loading) return <div className="text-gray-500 dark:text-gray-400">Loading...</div>

  return (
    <div className="animate-slide-in">
      <PageHeader
        title="API Keys"
        actions={
          <button
            onClick={() => {
              setShowCreate(!showCreate)
              setNewKey(null)
            }}
            className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white shadow-xs hover:bg-blue-700"
          >
            {showCreate ? 'Cancel' : 'Create Key'}
          </button>
        }
      />

      {error && (
        <div className="mb-4 rounded-sm bg-red-50 dark:bg-red-900/20 px-3 py-2 text-sm text-red-700 dark:text-red-400">
          {error}
        </div>
      )}

      {newKey && (
        <Card className="mb-4 border border-green-200 dark:border-green-800 bg-green-50 dark:bg-green-900/20 p-4">
          <p className="mb-2 text-sm font-medium text-green-800 dark:text-green-400">
            API key created! Copy it now — it won't be shown again.
          </p>
          <div className="space-y-3">
            <div>
              <p className="mb-1 text-xs font-medium text-gray-600 dark:text-gray-400">
                API Key (for Authorization header)
              </p>
              <code className="block rounded-sm bg-white dark:bg-gray-700 px-3 py-2 text-sm font-mono text-gray-800 dark:text-gray-200 select-all">
                {newKey.raw_key}
              </code>
            </div>
            <div>
              <p className="mb-1 text-xs font-medium text-gray-600 dark:text-gray-400">
                MCP URL path (for cloud AI connectors like Claude, ChatGPT, Gemini)
              </p>
              <code className="block rounded-sm bg-white dark:bg-gray-700 px-3 py-2 text-sm font-mono text-gray-800 dark:text-gray-200 select-all">
                {newKey.mcp_path}
              </code>
            </div>
          </div>
        </Card>
      )}

      {showCreate && !newKey && (
        <CreateKeyForm
          topics={topics}
          folders={folders}
          onCreated={(key) => {
            setNewKey(key)
            loadKeys()
          }}
          onError={setError}
        />
      )}

      <Card className="overflow-hidden">
        <table className="min-w-full divide-y divide-(--color-border)">
          <thead className="bg-(--color-bg-secondary)">
            <tr>
              <th className="px-4 py-3 text-left text-xs font-medium uppercase text-gray-500 dark:text-gray-400">
                Name
              </th>
              <th className="px-4 py-3 text-left text-xs font-medium uppercase text-gray-500 dark:text-gray-400">
                Status
              </th>
              <th className="px-4 py-3 text-left text-xs font-medium uppercase text-gray-500 dark:text-gray-400">
                Scope
              </th>
              <th className="px-4 py-3 text-left text-xs font-medium uppercase text-gray-500 dark:text-gray-400">
                Expires
              </th>
              <th className="px-4 py-3 text-left text-xs font-medium uppercase text-gray-500 dark:text-gray-400">
                Created
              </th>
              <th className="px-4 py-3 text-left text-xs font-medium uppercase text-gray-500 dark:text-gray-400">
                Last Used
              </th>
              <th className="px-4 py-3 text-right text-xs font-medium uppercase text-gray-500 dark:text-gray-400">
                Actions
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-(--color-border) bg-white dark:bg-[#2c2c2e]">
            {keys.map((k) => (
              <tr key={k.key_id} className="hover:bg-black/3 dark:hover:bg-white/3">
                <td className="px-4 py-3 text-sm font-medium">
                  <Link
                    to={`/settings/api-keys/${k.key_id}`}
                    className="text-blue-600 dark:text-blue-400 hover:underline"
                  >
                    {k.name}
                  </Link>
                </td>
                <td className="px-4 py-3">
                  <span
                    className={`rounded-md px-2 py-0.5 text-[11px] font-medium ${
                      k.is_active
                        ? 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400'
                        : 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400'
                    }`}
                  >
                    {k.is_active ? 'active' : 'revoked'}
                  </span>
                </td>
                <td className="px-4 py-3 text-sm text-gray-600 dark:text-gray-300">{k.scope_summary}</td>
                <td className="px-4 py-3 text-sm text-gray-500 dark:text-gray-400">
                  {k.expires_at ? new Date(k.expires_at).toLocaleDateString() : 'Never'}
                </td>
                <td className="px-4 py-3 text-sm text-gray-500 dark:text-gray-400">
                  {new Date(k.created_at).toLocaleDateString()}
                </td>
                <td className="px-4 py-3 text-sm text-gray-500 dark:text-gray-400">
                  {k.last_used_at ? new Date(k.last_used_at).toLocaleString() : 'Never'}
                </td>
                <td className="px-4 py-3 text-right">
                  {k.is_active && (
                    <div className="flex justify-end gap-3">
                      <button
                        onClick={() => setEditingKey(k)}
                        className="text-sm text-blue-600 dark:text-blue-400 hover:underline"
                      >
                        Edit
                      </button>
                      <button
                        onClick={() => handleRevoke(k.key_id)}
                        className="text-sm text-red-600 dark:text-red-400 hover:underline"
                      >
                        Revoke
                      </button>
                    </div>
                  )}
                </td>
              </tr>
            ))}
            {keys.length === 0 && (
              <tr>
                <td colSpan={7} className="px-4 py-6 text-center text-sm text-gray-500 dark:text-gray-400">
                  No API keys yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </Card>

      {editingKey && (
        <EditKeyModal
          apiKey={editingKey}
          topics={topics}
          folders={folders}
          onClose={() => setEditingKey(null)}
          onSaved={() => {
            setEditingKey(null)
            loadKeys()
          }}
          onError={setError}
        />
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Shared scope form fields
// ---------------------------------------------------------------------------

function ScopeFormFields({
  state,
  setState,
  topics,
  folders,
}: {
  state: ScopeFormState
  setState: (updater: (prev: ScopeFormState) => ScopeFormState) => void
  topics: TopicCluster[]
  folders: WatchedFolderInfo[]
}) {
  return (
    <div className="space-y-4">
      <div>
        <label className="flex items-center gap-2 text-xs font-medium text-gray-700 dark:text-gray-300">
          <input
            type="checkbox"
            checked={state.expiresAt !== ''}
            onChange={(e) =>
              setState((s) => ({
                ...s,
                expiresAt: e.target.checked ? new Date(Date.now() + 90 * 86400000).toISOString().slice(0, 10) : '',
              }))
            }
          />
          Set expiry date
        </label>
        {state.expiresAt !== '' && (
          <input
            type="date"
            value={state.expiresAt}
            onChange={(e) => setState((s) => ({ ...s, expiresAt: e.target.value }))}
            className="mt-1.5 rounded-lg border-0 bg-(--color-bg-secondary) dark:bg-(--color-bg-tertiary) shadow-mac focus:ring-2 focus:ring-(--color-accent)/30 px-3 py-1.5 text-sm"
          />
        )}
      </div>

      <div>
        <label className="mb-1 block text-xs font-medium text-gray-700 dark:text-gray-300">Permission tier</label>
        <div className="flex gap-4 text-sm">
          {(['search', 'read', 'full'] as const).map((t) => (
            <label key={t} className="flex items-center gap-1.5">
              <input
                type="radio"
                name="tier"
                value={t}
                checked={state.tier === t}
                onChange={() => setState((s) => ({ ...s, tier: t }))}
              />
              <span className="capitalize">{t}</span>
            </label>
          ))}
        </div>
        <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
          Search: search only · Read: search + passage read · Full: all read-only tools
        </p>
      </div>

      <details className="rounded-lg bg-(--color-bg-secondary) dark:bg-(--color-bg-tertiary) p-3">
        <summary className="cursor-pointer text-xs font-medium text-gray-700 dark:text-gray-300">
          Tool overrides ({Object.keys(state.toolOverrides).length} custom)
        </summary>
        <div className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1.5">
          {ALL_TOOLS.map((tool) => {
            const checked = toolEnabled(tool, state.tier, state.toolOverrides)
            const overridden = tool in state.toolOverrides
            return (
              <label key={tool} className="flex items-center gap-1.5 text-xs">
                <input
                  type="checkbox"
                  checked={checked}
                  onChange={(e) =>
                    setState((s) => ({
                      ...s,
                      toolOverrides: toggleToolOverride(tool, e.target.checked, s.tier, s.toolOverrides),
                    }))
                  }
                />
                <code className={overridden ? 'font-semibold text-(--color-accent)' : ''}>{tool}</code>
              </label>
            )
          })}
        </div>
      </details>

      <details className="rounded-lg bg-(--color-bg-secondary) dark:bg-(--color-bg-tertiary) p-3">
        <summary className="cursor-pointer text-xs font-medium text-gray-700 dark:text-gray-300">
          Document scope (empty = no restriction)
        </summary>
        <div className="mt-3 space-y-3">
          <div>
            <p className="mb-1 text-xs font-medium text-gray-600 dark:text-gray-400">Topics</p>
            {topics.length === 0 ? (
              <p className="text-xs text-gray-500 dark:text-gray-400">No topics available.</p>
            ) : (
              <div className="max-h-40 overflow-y-auto rounded-sm bg-white dark:bg-[#1e1e1f] p-2 ring-1 ring-(--color-border)">
                {topics.map((t) => {
                  const selected = state.topicIds?.includes(t.cluster_id) ?? false
                  return (
                    <label key={t.cluster_id} className="flex items-center gap-1.5 text-xs py-0.5">
                      <input
                        type="checkbox"
                        checked={selected}
                        onChange={(e) =>
                          setState((s) => {
                            const current = s.topicIds ?? []
                            const next = e.target.checked
                              ? [...current, t.cluster_id]
                              : current.filter((id) => id !== t.cluster_id)
                            return { ...s, topicIds: next.length > 0 ? next : null }
                          })
                        }
                      />
                      <span>{t.name}</span>
                    </label>
                  )
                })}
              </div>
            )}
          </div>

          <div>
            <p className="mb-1 text-xs font-medium text-gray-600 dark:text-gray-400">Watched folders</p>
            {folders.length === 0 ? (
              <p className="text-xs text-gray-500 dark:text-gray-400">No watched folders configured.</p>
            ) : (
              <div className="max-h-40 overflow-y-auto rounded-sm bg-white dark:bg-[#1e1e1f] p-2 ring-1 ring-(--color-border)">
                {folders.map((f) => {
                  const selected = state.folderIds?.includes(f.folder_id) ?? false
                  return (
                    <label key={f.folder_id} className="flex items-center gap-1.5 text-xs py-0.5">
                      <input
                        type="checkbox"
                        checked={selected}
                        onChange={(e) =>
                          setState((s) => {
                            const current = s.folderIds ?? []
                            const next = e.target.checked
                              ? [...current, f.folder_id]
                              : current.filter((id) => id !== f.folder_id)
                            return { ...s, folderIds: next.length > 0 ? next : null }
                          })
                        }
                      />
                      <span className="font-mono">{f.path}</span>
                    </label>
                  )
                })}
              </div>
            )}
          </div>
        </div>
      </details>

      <div>
        <label className="mb-1 block text-xs font-medium text-gray-700 dark:text-gray-300">
          Max snippet chars (optional)
        </label>
        <input
          type="number"
          min="1"
          value={state.maxSnippetChars}
          onChange={(e) => setState((s) => ({ ...s, maxSnippetChars: e.target.value }))}
          placeholder="No limit"
          className="w-40 rounded-lg border-0 bg-(--color-bg-secondary) dark:bg-(--color-bg-tertiary) shadow-mac focus:ring-2 focus:ring-(--color-accent)/30 px-3 py-1.5 text-sm"
        />
      </div>

      <div>
        <label className="flex items-center gap-2 text-xs font-medium text-gray-700 dark:text-gray-300">
          <input
            type="checkbox"
            checked={state.rateLimitRpmUnlimited}
            onChange={(e) =>
              setState((s) => ({
                ...s,
                rateLimitRpmUnlimited: e.target.checked,
                rateLimitRpm: e.target.checked ? '' : s.rateLimitRpm,
              }))
            }
          />
          Unlimited requests/min
        </label>
        {!state.rateLimitRpmUnlimited && (
          <input
            type="number"
            min="1"
            value={state.rateLimitRpm}
            onChange={(e) => setState((s) => ({ ...s, rateLimitRpm: e.target.value }))}
            placeholder="System default"
            className="mt-1.5 w-40 rounded-lg border-0 bg-(--color-bg-secondary) dark:bg-(--color-bg-tertiary) shadow-mac focus:ring-2 focus:ring-(--color-accent)/30 px-3 py-1.5 text-sm"
          />
        )}
      </div>

      <div>
        <label className="flex items-center gap-2 text-xs font-medium text-gray-700 dark:text-gray-300">
          <input
            type="checkbox"
            checked={state.rateLimitRphUnlimited}
            onChange={(e) =>
              setState((s) => ({
                ...s,
                rateLimitRphUnlimited: e.target.checked,
                rateLimitRph: e.target.checked ? '' : s.rateLimitRph,
              }))
            }
          />
          Unlimited requests/hour
        </label>
        {!state.rateLimitRphUnlimited && (
          <input
            type="number"
            min="1"
            value={state.rateLimitRph}
            onChange={(e) => setState((s) => ({ ...s, rateLimitRph: e.target.value }))}
            placeholder="System default"
            className="mt-1.5 w-40 rounded-lg border-0 bg-(--color-bg-secondary) dark:bg-(--color-bg-tertiary) shadow-mac focus:ring-2 focus:ring-(--color-accent)/30 px-3 py-1.5 text-sm"
          />
        )}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Create form
// ---------------------------------------------------------------------------

function CreateKeyForm({
  onCreated,
  onError,
  topics,
  folders,
}: {
  onCreated: (key: ApiKeyCreated) => void
  onError: (msg: string) => void
  topics: TopicCluster[]
  folders: WatchedFolderInfo[]
}) {
  const [name, setName] = useState('')
  const [scope, setScope] = useState<ScopeFormState>(emptyScopeState())
  const [submitting, setSubmitting] = useState(false)

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    setSubmitting(true)
    try {
      const payload = { name, ...scopeStateToPayload(scope) }
      const key = await post<ApiKeyCreated>('/api/api-keys', payload)
      onCreated(key)
    } catch (e) {
      onError(e instanceof Error ? e.message : 'Create failed')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Card className="mb-4 space-y-4 p-4">
      <form onSubmit={handleSubmit}>
        <div>
          <label className="mb-1 block text-xs font-medium text-gray-700 dark:text-gray-300">Key Name</label>
          <input
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            required
            placeholder="e.g. Claude Desktop"
            className="w-full rounded-lg border-0 bg-(--color-bg-secondary) dark:bg-(--color-bg-tertiary) shadow-mac focus:ring-2 focus:ring-(--color-accent)/30 px-3 py-1.5 text-sm"
          />
        </div>

        <ScopeFormFields state={scope} setState={setScope} topics={topics} folders={folders} />

        <div className="flex justify-end">
          <button
            type="submit"
            disabled={submitting}
            className="rounded-lg bg-blue-600 px-4 py-1.5 text-sm font-medium text-white shadow-xs hover:bg-blue-700 disabled:opacity-50"
          >
            Create
          </button>
        </div>
      </form>
    </Card>
  )
}

// ---------------------------------------------------------------------------
// Edit modal
// ---------------------------------------------------------------------------

function EditKeyModal({
  apiKey,
  topics,
  folders,
  onClose,
  onSaved,
  onError,
}: {
  apiKey: ApiKeyInfo
  topics: TopicCluster[]
  folders: WatchedFolderInfo[]
  onClose: () => void
  onSaved: () => void
  onError: (msg: string) => void
}) {
  const [scope, setScope] = useState<ScopeFormState>(scopeStateFromKey(apiKey))
  const [submitting, setSubmitting] = useState(false)
  const [preview, setPreview] = useState<ScopePreview | null>(null)
  const [previewLoading, setPreviewLoading] = useState(false)
  const debounceRef = useRef<number | null>(null)

  // Memo the payload object; use its JSON string as a stable effect dep key
  const scopePayload = useMemo(() => scopeStateToPayload(scope), [scope])
  const payloadKey = useMemo(() => JSON.stringify(scopePayload), [scopePayload])

  // Debounced live scope preview: POST unsaved scope params to get real-time
  // document count without needing to save first.
  useEffect(() => {
    let cancelled = false
    async function fetchPreview() {
      setPreviewLoading(true)
      try {
        const data = await post<ScopePreview>('/api/api-keys/scope-preview', {
          permission_tier: scopePayload.permission_tier,
          scope_topic_ids: scopePayload.scope_topic_ids,
          scope_folder_ids: scopePayload.scope_folder_ids,
        })
        if (!cancelled) setPreview(data)
      } catch {
        if (!cancelled) setPreview(null)
      } finally {
        if (!cancelled) setPreviewLoading(false)
      }
    }
    if (debounceRef.current) window.clearTimeout(debounceRef.current)
    debounceRef.current = window.setTimeout(fetchPreview, 500)
    return () => {
      cancelled = true
      if (debounceRef.current) window.clearTimeout(debounceRef.current)
    }
  }, [apiKey.key_id, payloadKey, scopePayload])

  async function handleSave() {
    setSubmitting(true)
    try {
      await patch(`/api/api-keys/${apiKey.key_id}`, scopeStateToPayload(scope))
      onSaved()
    } catch (e) {
      onError(e instanceof Error ? e.message : 'Save failed')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose()
      }}
    >
      <Card className="w-full max-w-2xl max-h-[90vh] overflow-y-auto p-5">
        <div className="mb-4 flex items-center justify-between">
          <h2 className="text-lg font-bold">Edit Key: {apiKey.name}</h2>
          <button
            onClick={onClose}
            className="text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200"
            aria-label="Close"
          >
            ×
          </button>
        </div>

        <ScopeFormFields state={scope} setState={setScope} topics={topics} folders={folders} />

        <div className="mt-4 rounded-lg bg-(--color-bg-secondary) dark:bg-(--color-bg-tertiary) p-3 text-xs">
          {previewLoading && <span className="text-gray-500 dark:text-gray-400">Loading scope preview…</span>}
          {!previewLoading && preview && (
            <span className="text-gray-700 dark:text-gray-300">
              <strong>{preview.accessible_documents}</strong> of <strong>{preview.total_documents}</strong> documents
              accessible
            </span>
          )}
          {!previewLoading && !preview && (
            <span className="text-gray-500 dark:text-gray-400">Scope preview unavailable.</span>
          )}
        </div>

        <div className="mt-5 flex justify-end gap-3">
          <button
            onClick={onClose}
            className="rounded-lg bg-gray-200 dark:bg-gray-700 px-4 py-1.5 text-sm font-medium text-gray-800 dark:text-gray-200 hover:bg-gray-300 dark:hover:bg-gray-600"
          >
            Cancel
          </button>
          <button
            onClick={handleSave}
            disabled={submitting}
            className="rounded-lg bg-blue-600 px-4 py-1.5 text-sm font-medium text-white shadow-xs hover:bg-blue-700 disabled:opacity-50"
          >
            {submitting ? 'Saving…' : 'Save'}
          </button>
        </div>
      </Card>
    </div>
  )
}

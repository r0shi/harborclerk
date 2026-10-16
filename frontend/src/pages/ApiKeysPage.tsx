import { FormEvent, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router'
import { del, get, patch, post } from '../api'
import { PageHeader } from '../components/PageHeader'
import { Card } from '../components/Card'
import {
  defaultExpiryDate,
  documentScopeSummary,
  expiryDateInput,
  formatExpiry,
  formatExpiryMoment,
  isLocalEndOfDay,
  isScopeRestricted,
  parseExpiry,
  parseRateLimit,
  parseSnippetCap,
  snippetCapInput,
  unknownScopeIds,
  type ExpiryParse,
  type NumberFieldParse,
} from '../utils/apiKeyForm'

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

interface ScopeOptions {
  topics: TopicCluster[]
  folders: WatchedFolderInfo[]
  topicsFailed: boolean // the list failed to load: its stored ids are unknown, not stale
  foldersFailed: boolean
  loading: boolean // the lists are still on their way: scope fields wait, the key table does not
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
  // Kept apart from the date: a date input reports '' while a segment is cleared, and that must
  // read as "incomplete", not as "never expires".
  expiryEnabled: boolean
  expiresAt: string // yyyy-mm-dd in local time, or '' (incomplete or unset)
  storedExpiresAt: string | null // the key's saved instant, for an honest hint; never sent
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
    expiryEnabled: false,
    expiresAt: '',
    storedExpiresAt: null,
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
    expiryEnabled: k.expires_at != null,
    expiresAt: expiryDateInput(k.expires_at),
    storedExpiresAt: k.expires_at,
    tier: k.permission_tier,
    toolOverrides: { ...k.tool_overrides },
    topicIds: k.scope_topic_ids,
    folderIds: k.scope_folder_ids,
    maxSnippetChars: snippetCapInput(k.max_snippet_chars),
    // The rate limiter treats any value <= 0 as unlimited (a legacy negative included), as does
    // the scope summary, so the form shows it that way.
    rateLimitRpmUnlimited: k.rate_limit_rpm != null && k.rate_limit_rpm <= 0,
    rateLimitRpm: k.rate_limit_rpm != null && k.rate_limit_rpm > 0 ? String(k.rate_limit_rpm) : '',
    rateLimitRphUnlimited: k.rate_limit_rph != null && k.rate_limit_rph <= 0,
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

type FieldResult<T> = { ok: true; value: T } | { ok: false; error: string }

function expiryField(s: ScopeFormState): ExpiryParse {
  return parseExpiry(s.expiryEnabled, s.expiresAt)
}

function snippetCapField(s: ScopeFormState): NumberFieldParse {
  return parseSnippetCap(s.maxSnippetChars)
}

function rpmField(s: ScopeFormState): NumberFieldParse {
  return s.rateLimitRpmUnlimited ? { ok: true, value: 0 } : parseRateLimit(s.rateLimitRpm, 'Requests/min')
}

function rphField(s: ScopeFormState): NumberFieldParse {
  return s.rateLimitRphUnlimited ? { ok: true, value: 0 } : parseRateLimit(s.rateLimitRph, 'Requests/hour')
}

/** The whole payload for a new key, or the first field error to show. */
function scopeStateToPayload(s: ScopeFormState): FieldResult<ScopePayload> {
  const expiry = expiryField(s)
  if (!expiry.ok) return expiry
  const cap = snippetCapField(s)
  if (!cap.ok) return cap
  const rpm = rpmField(s)
  if (!rpm.ok) return rpm
  const rph = rphField(s)
  if (!rph.ok) return rph
  return {
    ok: true,
    value: {
      expires_at: expiry.value,
      permission_tier: s.tier,
      tool_overrides: s.toolOverrides,
      scope_topic_ids: s.topicIds,
      scope_folder_ids: s.folderIds,
      max_snippet_chars: cap.value,
      rate_limit_rpm: rpm.value,
      rate_limit_rph: rph.value,
    },
  }
}

// Order-insensitive comparison for the override map and the id lists, which the
// checkboxes rebuild in click order.
function sameSet(a: Record<string, boolean> | (number | string)[] | null, b: typeof a): boolean {
  const norm = (v: typeof a) =>
    v == null ? 'null' : Array.isArray(v) ? JSON.stringify([...v].sort()) : JSON.stringify(Object.entries(v).sort())
  return norm(a) === norm(b)
}

/**
 * The PATCH body for an edit: only the fields the operator changed, compared with the form as it
 * was loaded. PATCH applies every field it is sent, and the form cannot represent every stored
 * value (an expiry time is shown as its date, a negative rate limit as unlimited, a legacy
 * negative cap as 0, a name with surrounding spaces trimmed), so re-sending an untouched field would silently change the key.
 */
function changedFields(
  name: string,
  initialName: string,
  s: ScopeFormState,
  initial: ScopeFormState,
): FieldResult<Partial<ScopePayload> & { name?: string }> {
  const body: Partial<ScopePayload> & { name?: string } = {}
  if (name.trim() !== initialName.trim()) {
    if (!name.trim()) return { ok: false, error: KEY_NAME_ERROR }
    body.name = name.trim()
  }
  if (s.expiryEnabled !== initial.expiryEnabled || (s.expiryEnabled && s.expiresAt !== initial.expiresAt)) {
    const expiry = expiryField(s)
    if (!expiry.ok) return expiry
    body.expires_at = expiry.value
  }
  if (s.tier !== initial.tier) body.permission_tier = s.tier
  if (!sameSet(s.toolOverrides, initial.toolOverrides)) body.tool_overrides = s.toolOverrides
  if (!sameSet(s.topicIds, initial.topicIds)) body.scope_topic_ids = s.topicIds
  if (!sameSet(s.folderIds, initial.folderIds)) body.scope_folder_ids = s.folderIds
  if (s.maxSnippetChars.trim() !== initial.maxSnippetChars.trim()) {
    const cap = snippetCapField(s)
    if (!cap.ok) return cap
    body.max_snippet_chars = cap.value
  }
  if (
    s.rateLimitRpmUnlimited !== initial.rateLimitRpmUnlimited ||
    s.rateLimitRpm.trim() !== initial.rateLimitRpm.trim()
  ) {
    const rpm = rpmField(s)
    if (!rpm.ok) return rpm
    body.rate_limit_rpm = rpm.value
  }
  if (
    s.rateLimitRphUnlimited !== initial.rateLimitRphUnlimited ||
    s.rateLimitRph.trim() !== initial.rateLimitRph.trim()
  ) {
    const rph = rphField(s)
    if (!rph.ok) return rph
    body.rate_limit_rph = rph.value
  }
  return { ok: true, value: body }
}

const KEY_NAME_ERROR = 'Key name cannot be empty.'

function FieldError({ result }: { result: { ok: true } | { ok: false; error: string } }) {
  if (result.ok) return null
  return (
    <p role="alert" className="mt-1 text-xs text-red-600 dark:text-red-400">
      {result.error}
    </p>
  )
}

export default function ApiKeysPage() {
  const [keys, setKeys] = useState<ApiKeyInfo[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [showCreate, setShowCreate] = useState(false)
  const [newKey, setNewKey] = useState<ApiKeyCreated | null>(null)
  const [editingKey, setEditingKey] = useState<ApiKeyInfo | null>(null)

  const [scopeOptions, setScopeOptions] = useState<ScopeOptions>({
    topics: [],
    folders: [],
    topicsFailed: false,
    foldersFailed: false,
    loading: true,
  })

  async function loadKeys() {
    try {
      const data = await get<ApiKeyInfo[]>('/api/api-keys')
      setKeys(data)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load keys')
    }
  }

  // A list that failed to load is not an empty list: read as empty, every stored id would look
  // stale, and removing those ids could leave the key with no restriction at all.
  async function loadScopeOptions() {
    const [t, f] = await Promise.all([
      get<{ clusters: TopicCluster[] }>('/api/stats/topics').then(
        (r) => r.clusters || [],
        () => null,
      ),
      get<WatchedFolderInfo[]>('/api/watch/folders').then(
        (r) => (Array.isArray(r) ? r : []),
        () => null,
      ),
    ])
    setScopeOptions({
      topics: t ?? [],
      folders: f ?? [],
      topicsFailed: t === null,
      foldersFailed: f === null,
      loading: false,
    })
  }

  useEffect(() => {
    // The key table (and Revoke) wait only for the keys. The topic and folder lists load
    // alongside; the forms' scope fields wait for them (ScopeOptions.loading), since before they
    // arrive every stored id would look as if it were no longer listed.
    async function run() {
      void loadScopeOptions() // not awaited: the table must not wait for it
      await loadKeys()
      setLoading(false)
    }
    run()
  }, [])

  // Revoke is confirmed in the row, not with window.confirm: the macOS app hosts this page in a
  // WKWebView, where window.confirm silently returns false, so Revoke never revoked (#833).
  const [pendingRevokeId, setPendingRevokeId] = useState<string | null>(null)

  async function handleRevoke(keyId: string) {
    setPendingRevokeId(null)
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
          options={scopeOptions}
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
                <td className="px-4 py-3 text-sm text-gray-500 dark:text-gray-400">{formatExpiry(k.expires_at)}</td>
                <td className="px-4 py-3 text-sm text-gray-500 dark:text-gray-400">
                  {new Date(k.created_at).toLocaleDateString()}
                </td>
                <td className="px-4 py-3 text-sm text-gray-500 dark:text-gray-400">
                  {k.last_used_at ? new Date(k.last_used_at).toLocaleString() : 'Never'}
                </td>
                <td className="px-4 py-3 text-right">
                  {k.is_active && pendingRevokeId === k.key_id && (
                    <div className="flex items-center justify-end gap-2">
                      <span className="text-sm text-red-600 dark:text-red-400">
                        Revoke this key? This cannot be undone.
                      </span>
                      <button
                        onClick={() => handleRevoke(k.key_id)}
                        className="rounded-lg bg-red-600 px-3 py-1 text-sm font-medium text-white shadow-xs hover:bg-red-700"
                      >
                        Confirm revoke
                      </button>
                      <button
                        onClick={() => setPendingRevokeId(null)}
                        className="rounded-lg bg-gray-200 dark:bg-gray-700 px-3 py-1 text-sm font-medium text-gray-800 dark:text-gray-200 hover:bg-gray-300 dark:hover:bg-gray-600"
                      >
                        Cancel
                      </button>
                    </div>
                  )}
                  {k.is_active && pendingRevokeId !== k.key_id && (
                    <div className="flex justify-end gap-3">
                      <button
                        onClick={() => setEditingKey(k)}
                        className="text-sm text-blue-600 dark:text-blue-400 hover:underline"
                      >
                        Edit
                      </button>
                      <button
                        onClick={() => setPendingRevokeId(k.key_id)}
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
          options={scopeOptions}
          onClose={() => setEditingKey(null)}
          onSaved={() => {
            setEditingKey(null)
            loadKeys()
          }}
        />
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Shared scope form fields
// ---------------------------------------------------------------------------

function UnknownScopeChips<T extends number | string>({
  kind,
  ids,
  onRemove,
}: {
  kind: 'topic' | 'folder'
  ids: T[]
  onRemove: (id: T) => void
}) {
  if (ids.length === 0) return null
  return (
    <div className="mb-2 rounded-sm bg-amber-50 dark:bg-amber-900/20 p-2 text-xs text-amber-800 dark:text-amber-300">
      <p className="mb-1.5">
        The key is still limited to {ids.length === 1 ? `this ${kind}` : `these ${kind}s`}, which{' '}
        {ids.length === 1 ? 'is' : 'are'} no longer in the list. Remove {ids.length === 1 ? 'it' : 'them'} to drop the
        restriction.
      </p>
      <ul className="flex flex-wrap gap-1.5" aria-label={`Unlisted ${kind}s`}>
        {ids.map((id) => (
          <li
            key={String(id)}
            className="flex items-center gap-1 rounded-md bg-white dark:bg-[#1e1e1f] px-2 py-0.5 ring-1 ring-amber-300 dark:ring-amber-700"
          >
            <span className="font-mono">
              {kind === 'topic' ? 'Topic' : 'Folder'} {String(id)}
            </span>
            <button
              type="button"
              onClick={() => onRemove(id)}
              aria-label={`Remove ${kind} ${String(id)}`}
              className="text-amber-700 hover:text-amber-900 dark:text-amber-400 dark:hover:text-amber-200"
            >
              ×
            </button>
          </li>
        ))}
      </ul>
    </div>
  )
}

function ScopeListFailed({ kind, storedCount }: { kind: 'topic' | 'folder'; storedCount: number }) {
  return (
    <p role="alert" className="text-xs text-red-600 dark:text-red-400">
      The {kind} list could not be loaded, so {kind}s cannot be changed here.{' '}
      {storedCount > 0
        ? `The key's ${storedCount} stored ${kind} restriction${storedCount === 1 ? ' is' : 's are'} kept as is.`
        : ''}{' '}
      Reload the page to try again.
    </p>
  )
}

function ScopeFormFields({
  state,
  setState,
  options,
}: {
  state: ScopeFormState
  setState: (updater: (prev: ScopeFormState) => ScopeFormState) => void
  options: ScopeOptions
}) {
  const parsedCap = snippetCapField(state)
  const parsedExpiry = expiryField(state)
  // The saved instant is kept on save while its date is left as it is (changedFields sends no
  // expiry then), so the hint must name that instant when it is not the end of a local day: a
  // key saved by the old UI, or a time set through the API.
  const keepsStoredTime =
    state.expiryEnabled &&
    state.storedExpiresAt != null &&
    state.expiresAt === expiryDateInput(state.storedExpiresAt) &&
    !isLocalEndOfDay(state.storedExpiresAt)
  const { topics, folders, topicsFailed, foldersFailed, loading: optionsLoading } = options
  // Stored ids a loaded list no longer shows (a topic re-clustered away, a folder removed). The
  // key is still limited to them, so they stay visible and removable. A list that failed to load
  // says nothing about its ids: they are shown as kept, with nothing offered for removal.
  const unknownTopicIds =
    topicsFailed || optionsLoading
      ? []
      : unknownScopeIds(
          state.topicIds,
          topics.map((t) => t.cluster_id),
        )
  const unknownFolderIds =
    foldersFailed || optionsLoading
      ? []
      : unknownScopeIds(
          state.folderIds,
          folders.map((f) => f.folder_id),
        )
  return (
    <div className="space-y-4">
      <div>
        <label className="flex items-center gap-2 text-xs font-medium text-gray-700 dark:text-gray-300">
          <input
            type="checkbox"
            checked={state.expiryEnabled}
            onChange={(e) =>
              setState((s) => ({
                ...s,
                expiryEnabled: e.target.checked,
                // Unticking keeps the date, so ticking again restores it rather than a new default.
                expiresAt: e.target.checked ? s.expiresAt || defaultExpiryDate() : s.expiresAt,
              }))
            }
          />
          Set expiry date
        </label>
        {state.expiryEnabled && (
          <>
            <input
              type="date"
              aria-label="Expiry date"
              value={state.expiresAt}
              onChange={(e) => setState((s) => ({ ...s, expiresAt: e.target.value }))}
              className="mt-1.5 rounded-lg border-0 bg-(--color-bg-secondary) dark:bg-(--color-bg-tertiary) shadow-mac focus:ring-2 focus:ring-(--color-accent)/30 px-3 py-1.5 text-sm"
            />
            {!parsedExpiry.ok ? (
              <FieldError result={parsedExpiry} />
            ) : keepsStoredTime ? (
              <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                The key stops working at {formatExpiryMoment(state.storedExpiresAt!)}, the time it was saved with.
                Choosing another date sets the end of that day, in this computer&apos;s time zone.
              </p>
            ) : (
              <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                The key stops working at the end of this day, in this computer&apos;s time zone.
              </p>
            )}
          </>
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
          Document scope: {documentScopeSummary(state.topicIds, state.folderIds)}
          {unknownTopicIds.length + unknownFolderIds.length > 0 && (
            <span className="ml-1 text-amber-700 dark:text-amber-400">
              ({unknownTopicIds.length + unknownFolderIds.length} no longer listed)
            </span>
          )}
          {(topicsFailed || foldersFailed) && (
            <span className="ml-1 text-red-600 dark:text-red-400">
              ({[topicsFailed && 'topics', foldersFailed && 'folders'].filter(Boolean).join(' and ')} could not be
              loaded)
            </span>
          )}
        </summary>
        <div className="mt-3 space-y-3">
          {optionsLoading ? (
            <p role="status" className="text-xs text-gray-500 dark:text-gray-400">
              Loading topics and folders…
            </p>
          ) : (
            <>
              <p className="text-xs text-gray-500 dark:text-gray-400">Tick nothing for no restriction.</p>
              <div>
                <p className="mb-1 text-xs font-medium text-gray-600 dark:text-gray-400">Topics</p>
                <UnknownScopeChips
                  kind="topic"
                  ids={unknownTopicIds}
                  onRemove={(id) =>
                    setState((s) => {
                      const next = (s.topicIds ?? []).filter((t) => t !== id)
                      return { ...s, topicIds: next.length > 0 ? next : null }
                    })
                  }
                />
                {topicsFailed ? (
                  <ScopeListFailed kind="topic" storedCount={state.topicIds?.length ?? 0} />
                ) : topics.length === 0 ? (
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
                <UnknownScopeChips
                  kind="folder"
                  ids={unknownFolderIds}
                  onRemove={(id) =>
                    setState((s) => {
                      const next = (s.folderIds ?? []).filter((f) => f !== id)
                      return { ...s, folderIds: next.length > 0 ? next : null }
                    })
                  }
                />
                {foldersFailed ? (
                  <ScopeListFailed kind="folder" storedCount={state.folderIds?.length ?? 0} />
                ) : folders.length === 0 ? (
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
            </>
          )}
        </div>
      </details>

      <div>
        <label className="mb-1 block text-xs font-medium text-gray-700 dark:text-gray-300">
          Max snippet chars (optional)
        </label>
        <input
          type="text"
          inputMode="numeric"
          aria-label="Max snippet chars"
          value={state.maxSnippetChars}
          onChange={(e) => setState((s) => ({ ...s, maxSnippetChars: e.target.value }))}
          placeholder="No limit"
          className="w-40 rounded-lg border-0 bg-(--color-bg-secondary) dark:bg-(--color-bg-tertiary) shadow-mac focus:ring-2 focus:ring-(--color-accent)/30 px-3 py-1.5 text-sm"
        />
        {!parsedCap.ok ? (
          <FieldError result={parsedCap} />
        ) : (
          <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
            Passage and page text, summaries and metadata values the key receives are cut to their first N characters, a
            literal-text filter matches only within them, and a metadata filter value longer than N is refused; 0
            returns citations without the text. Ranked search still matches the whole document, so results can show that
            words occur past the cap. Titles, dates and mail headers (sender, recipients, subject, ids) are outside the
            cap. Empty = no limit.
          </p>
        )}
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
            type="text"
            inputMode="numeric"
            aria-label="Requests per minute"
            value={state.rateLimitRpm}
            onChange={(e) => setState((s) => ({ ...s, rateLimitRpm: e.target.value }))}
            placeholder="System default"
            className="mt-1.5 w-40 rounded-lg border-0 bg-(--color-bg-secondary) dark:bg-(--color-bg-tertiary) shadow-mac focus:ring-2 focus:ring-(--color-accent)/30 px-3 py-1.5 text-sm"
          />
        )}
        <FieldError result={rpmField(state)} />
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
            type="text"
            inputMode="numeric"
            aria-label="Requests per hour"
            value={state.rateLimitRph}
            onChange={(e) => setState((s) => ({ ...s, rateLimitRph: e.target.value }))}
            placeholder="System default"
            className="mt-1.5 w-40 rounded-lg border-0 bg-(--color-bg-secondary) dark:bg-(--color-bg-tertiary) shadow-mac focus:ring-2 focus:ring-(--color-accent)/30 px-3 py-1.5 text-sm"
          />
        )}
        <FieldError result={rphField(state)} />
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
  options,
}: {
  onCreated: (key: ApiKeyCreated) => void
  onError: (msg: string) => void
  options: ScopeOptions
}) {
  const [name, setName] = useState('')
  const [scope, setScope] = useState<ScopeFormState>(emptyScopeState())
  const [submitting, setSubmitting] = useState(false)

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    if (!name.trim()) {
      onError(KEY_NAME_ERROR)
      return
    }
    const scopePayload = scopeStateToPayload(scope)
    if (!scopePayload.ok) {
      onError(scopePayload.error)
      return
    }
    setSubmitting(true)
    try {
      const payload = { name: name.trim(), ...scopePayload.value }
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

        <ScopeFormFields state={scope} setState={setScope} options={options} />

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
  options,
  onClose,
  onSaved,
}: {
  apiKey: ApiKeyInfo
  options: ScopeOptions
  onClose: () => void
  onSaved: () => void
}) {
  const [initialScope] = useState<ScopeFormState>(() => scopeStateFromKey(apiKey))
  const [scope, setScope] = useState<ScopeFormState>(initialScope)
  const [name, setName] = useState(apiKey.name)
  // Shown inside the modal: the page's error banner sits behind the overlay.
  const [saveError, setSaveError] = useState('')
  const [submitting, setSubmitting] = useState(false)
  // Asked in the modal (no native dialog) before a save that leaves a restricted key with no
  // document restriction, so it would read the whole corpus. Any scope edit withdraws the question.
  const [confirmUnrestrict, setConfirmUnrestrict] = useState(false)
  const [preview, setPreview] = useState<ScopePreview | null>(null)
  const [previewLoading, setPreviewLoading] = useState(false)
  const debounceRef = useRef<number | null>(null)

  // Memo the preview inputs; use their JSON string as a stable effect dep key. The
  // preview needs only tier and document scope, never the snippet cap (which may be invalid mid-edit).
  const scopePayload = useMemo(
    () => ({ permission_tier: scope.tier, scope_topic_ids: scope.topicIds, scope_folder_ids: scope.folderIds }),
    [scope.tier, scope.topicIds, scope.folderIds],
  )
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

  function updateScope(updater: (prev: ScopeFormState) => ScopeFormState) {
    setConfirmUnrestrict(false)
    setScope(updater)
  }

  async function handleSave(unrestrictConfirmed = false) {
    setSaveError('')
    const changed = changedFields(name, apiKey.name, scope, initialScope)
    if (!changed.ok) {
      setSaveError(changed.error)
      return
    }
    if (Object.keys(changed.value).length === 0) {
      onClose() // nothing changed: nothing to send
      return
    }
    const dropsLastRestriction =
      isScopeRestricted(initialScope.topicIds, initialScope.folderIds) &&
      !isScopeRestricted(scope.topicIds, scope.folderIds)
    if (dropsLastRestriction && !unrestrictConfirmed) {
      setConfirmUnrestrict(true)
      return
    }
    setConfirmUnrestrict(false)
    setSubmitting(true)
    try {
      await patch(`/api/api-keys/${apiKey.key_id}`, changed.value)
      onSaved()
    } catch (e) {
      setSaveError(e instanceof Error ? e.message : 'Save failed')
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

        <div className="mb-4">
          <label className="mb-1 block text-xs font-medium text-gray-700 dark:text-gray-300">Key Name</label>
          <input
            type="text"
            aria-label="Key name"
            value={name}
            onChange={(e) => setName(e.target.value)}
            className="w-full rounded-lg border-0 bg-(--color-bg-secondary) dark:bg-(--color-bg-tertiary) shadow-mac focus:ring-2 focus:ring-(--color-accent)/30 px-3 py-1.5 text-sm"
          />
        </div>

        <ScopeFormFields state={scope} setState={updateScope} options={options} />

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

        {saveError && (
          <div
            role="alert"
            className="mt-4 rounded-sm bg-red-50 dark:bg-red-900/20 px-3 py-2 text-sm text-red-700 dark:text-red-400"
          >
            {saveError}
          </div>
        )}

        {confirmUnrestrict && (
          <div
            role="alert"
            className="mt-4 rounded-sm bg-amber-50 dark:bg-amber-900/20 p-3 text-sm text-amber-800 dark:text-amber-300"
          >
            <p>
              This removes the key&apos;s last document restriction. Once saved, it can read every document in the
              corpus.
            </p>
            <div className="mt-2 flex justify-end gap-2">
              <button
                onClick={() => setConfirmUnrestrict(false)}
                className="rounded-lg bg-gray-200 dark:bg-gray-700 px-3 py-1 text-sm font-medium text-gray-800 dark:text-gray-200 hover:bg-gray-300 dark:hover:bg-gray-600"
              >
                Keep editing
              </button>
              <button
                onClick={() => handleSave(true)}
                disabled={submitting}
                className="rounded-lg bg-red-600 px-3 py-1 text-sm font-medium text-white shadow-xs hover:bg-red-700 disabled:opacity-50"
              >
                Remove restriction and save
              </button>
            </div>
          </div>
        )}

        <div className="mt-5 flex justify-end gap-3">
          <button
            onClick={onClose}
            className="rounded-lg bg-gray-200 dark:bg-gray-700 px-4 py-1.5 text-sm font-medium text-gray-800 dark:text-gray-200 hover:bg-gray-300 dark:hover:bg-gray-600"
          >
            Cancel
          </button>
          <button
            onClick={() => handleSave()}
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

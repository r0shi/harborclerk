import { useEffect, useState } from 'react'
import { get, put } from '../api'

interface RetrievalSettings {
  rag_auto_k: number
  rag_auto_threshold: number
  max_tool_rounds: number
  max_history_messages: number
  mcp_max_k: number
  mcp_brief_chars: number
}

interface FieldDef {
  key: keyof RetrievalSettings
  label: string
  description: string
  min: number
  max: number
  step: number
}

const CHAT_FIELDS: FieldDef[] = [
  { key: 'rag_auto_k', label: 'RAG context passages', description: 'Number of passages auto-injected into chat context (0 to disable)', min: 0, max: 10, step: 1 },
  { key: 'rag_auto_threshold', label: 'RAG relevance threshold', description: 'Minimum score for a passage to be included', min: 0, max: 1, step: 0.05 },
  { key: 'max_tool_rounds', label: 'Max tool rounds', description: 'Maximum tool-calling iterations per chat turn', min: 1, max: 10, step: 1 },
  { key: 'max_history_messages', label: 'Conversation history depth', description: 'Number of previous messages sent to the LLM', min: 10, max: 100, step: 5 },
]

const MCP_FIELDS: FieldDef[] = [
  { key: 'mcp_max_k', label: 'Max search results', description: 'Maximum passages returned by MCP search tools', min: 10, max: 1000, step: 10 },
  { key: 'mcp_brief_chars', label: 'Brief mode length', description: 'Character limit for brief passage text in MCP responses', min: 50, max: 1000, step: 50 },
]

function NumberField({ field, value, onChange }: { field: FieldDef; value: number; onChange: (v: number) => void }) {
  return (
    <div className="flex items-center justify-between gap-4 py-3">
      <div className="min-w-0">
        <div className="text-sm font-medium text-[var(--color-text-primary)]">{field.label}</div>
        <div className="text-xs text-[var(--color-text-secondary)] mt-0.5">{field.description}</div>
      </div>
      <input
        type="number"
        min={field.min}
        max={field.max}
        step={field.step}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        className="w-24 shrink-0 rounded-lg border-0 bg-[var(--color-bg-secondary)] dark:bg-[var(--color-bg-tertiary)] shadow-mac focus:ring-2 focus:ring-[var(--color-accent)]/30 px-3 py-1.5 text-sm text-right text-[var(--color-text-primary)] tabular-nums"
      />
    </div>
  )
}

export default function RetrievalSettingsPage() {
  const [saved, setSaved] = useState<RetrievalSettings | null>(null)
  const [form, setForm] = useState<RetrievalSettings | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [success, setSuccess] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    get<RetrievalSettings>('/api/system/retrieval-settings')
      .then((data) => { setSaved(data); setForm(data) })
      .catch((e) => setError(e instanceof Error ? e.message : 'Failed to load'))
      .finally(() => setLoading(false))
  }, [])

  if (loading) return <div className="text-sm text-[var(--color-text-secondary)]">Loading...</div>
  if (!form || !saved) return <div className="text-sm text-red-500">{error || 'Failed to load settings'}</div>

  const dirty = JSON.stringify(form) !== JSON.stringify(saved)

  function update(key: keyof RetrievalSettings, value: number) {
    setForm((prev) => prev ? { ...prev, [key]: value } : prev)
    setSuccess('')
  }

  async function handleSave() {
    setSaving(true)
    setError('')
    setSuccess('')
    try {
      const data = await put<RetrievalSettings>('/api/system/retrieval-settings', form)
      setSaved(data)
      setForm(data)
      setSuccess('Settings saved')
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Save failed')
    } finally {
      setSaving(false)
    }
  }

  function handleReset() {
    setForm(saved)
    setSuccess('')
    setError('')
  }

  return (
    <div className="animate-slide-in">
      <h1 className="mb-4 text-xl font-bold">Retrieval Settings</h1>
      <p className="mb-4 text-sm text-[var(--color-text-secondary)]">
        Tune how chat and MCP search tools retrieve passages from the knowledge base.
      </p>

      {error && (
        <div className="mb-4 rounded-lg bg-red-50 dark:bg-red-900/20 px-3 py-2 text-sm text-red-700 dark:text-red-400">
          {error}
        </div>
      )}
      {success && (
        <div className="mb-4 rounded-lg bg-green-50 dark:bg-green-900/20 px-3 py-2 text-sm text-green-700 dark:text-green-400">
          {success}
        </div>
      )}

      <div className="space-y-6">
        <div className="rounded-xl bg-white dark:bg-[#2c2c2e] shadow-mac border border-[var(--color-border)] overflow-hidden">
          <div className="px-4 py-3 bg-[var(--color-bg-secondary)]">
            <h2 className="text-sm font-medium text-[var(--color-text-primary)]">Chat Retrieval</h2>
          </div>
          <div className="px-4 divide-y divide-[var(--color-border)]">
            {CHAT_FIELDS.map((f) => (
              <NumberField key={f.key} field={f} value={form[f.key]} onChange={(v) => update(f.key, v)} />
            ))}
          </div>
        </div>

        <div className="rounded-xl bg-white dark:bg-[#2c2c2e] shadow-mac border border-[var(--color-border)] overflow-hidden">
          <div className="px-4 py-3 bg-[var(--color-bg-secondary)]">
            <h2 className="text-sm font-medium text-[var(--color-text-primary)]">MCP API</h2>
          </div>
          <div className="px-4 divide-y divide-[var(--color-border)]">
            {MCP_FIELDS.map((f) => (
              <NumberField key={f.key} field={f} value={form[f.key]} onChange={(v) => update(f.key, v)} />
            ))}
          </div>
        </div>
      </div>

      <div className="mt-6 flex gap-3">
        <button
          onClick={handleSave}
          disabled={!dirty || saving}
          className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white shadow-sm hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {saving ? 'Saving...' : 'Save'}
        </button>
        <button
          onClick={handleReset}
          disabled={!dirty}
          className="rounded-lg border border-gray-300 dark:border-gray-600 px-4 py-2 text-sm font-medium text-[var(--color-text-primary)] hover:bg-[var(--color-bg-secondary)] disabled:opacity-50 disabled:cursor-not-allowed"
        >
          Reset
        </button>
      </div>
    </div>
  )
}

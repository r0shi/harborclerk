import { useEffect, useState } from 'react'
import { get, put } from '../api'

interface RateLimitSettings {
  default_rpm: number
  default_rph: number
}

export default function RateLimitSettingsPage() {
  const [saved, setSaved] = useState<RateLimitSettings | null>(null)
  const [form, setForm] = useState<RateLimitSettings | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [success, setSuccess] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    get<RateLimitSettings>('/api/system/rate-limit-settings')
      .then((data) => {
        setSaved(data)
        setForm(data)
      })
      .catch((e) => setError(e instanceof Error ? e.message : 'Failed to load'))
      .finally(() => setLoading(false))
  }, [])

  if (loading) return <div className="text-sm text-(--color-text-secondary)">Loading...</div>
  if (!form || !saved) return <div className="text-sm text-red-500">{error || 'Failed to load settings'}</div>

  const dirty = JSON.stringify(form) !== JSON.stringify(saved)

  function updateField(key: keyof RateLimitSettings, value: number) {
    setForm((prev) => (prev ? { ...prev, [key]: value } : prev))
    setSuccess('')
  }

  async function handleSave() {
    setSaving(true)
    setError('')
    setSuccess('')
    try {
      const data = await put<RateLimitSettings>('/api/system/rate-limit-settings', form)
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
      <h1 className="mb-4 text-xl font-bold">Rate Limits</h1>
      <p className="mb-4 text-sm text-(--color-text-secondary)">
        Default rate limits applied to API keys that don&apos;t specify their own limits.
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

      <div className="rounded-xl bg-white dark:bg-[#2c2c2e] shadow-mac ring-1 ring-(--color-border) overflow-hidden">
        <div className="px-4 py-3 bg-(--color-bg-secondary)">
          <h2 className="text-sm font-medium text-(--color-text-primary)">Default Limits</h2>
        </div>
        <div className="px-4 divide-y divide-(--color-border)">
          <div className="flex items-center justify-between gap-4 py-3">
            <div className="min-w-0">
              <div className="text-sm font-medium text-(--color-text-primary)">Default requests/minute</div>
              <div className="text-xs text-(--color-text-secondary) mt-0.5">
                Applied when an API key has no per-key RPM override
              </div>
            </div>
            <input
              type="number"
              min={1}
              value={form.default_rpm}
              onChange={(e) => updateField('default_rpm', Number(e.target.value))}
              className="w-24 shrink-0 rounded-lg border-0 bg-(--color-bg-secondary) dark:bg-(--color-bg-tertiary) shadow-mac focus:ring-2 focus:ring-(--color-accent)/30 px-3 py-1.5 text-sm text-right text-(--color-text-primary) tabular-nums"
            />
          </div>
          <div className="flex items-center justify-between gap-4 py-3">
            <div className="min-w-0">
              <div className="text-sm font-medium text-(--color-text-primary)">Default requests/hour</div>
              <div className="text-xs text-(--color-text-secondary) mt-0.5">
                Applied when an API key has no per-key RPH override
              </div>
            </div>
            <input
              type="number"
              min={1}
              value={form.default_rph}
              onChange={(e) => updateField('default_rph', Number(e.target.value))}
              className="w-24 shrink-0 rounded-lg border-0 bg-(--color-bg-secondary) dark:bg-(--color-bg-tertiary) shadow-mac focus:ring-2 focus:ring-(--color-accent)/30 px-3 py-1.5 text-sm text-right text-(--color-text-primary) tabular-nums"
            />
          </div>
        </div>
      </div>

      <div className="mt-6 flex gap-3">
        <button
          onClick={handleSave}
          disabled={!dirty || saving}
          className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white shadow-xs hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {saving ? 'Saving...' : 'Save'}
        </button>
        <button
          onClick={handleReset}
          disabled={!dirty}
          className="rounded-lg border border-gray-300 dark:border-gray-600 px-4 py-2 text-sm font-medium text-(--color-text-primary) hover:bg-(--color-bg-secondary) disabled:opacity-50 disabled:cursor-not-allowed"
        >
          Reset
        </button>
      </div>
    </div>
  )
}

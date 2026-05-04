import { FormEvent, useState } from 'react'
import { useAuth } from '../auth'
import { post } from '../api'
import { PageHeader } from '../components/PageHeader'
import { Card } from '../components/Card'

const PAGE_SIZE_OPTIONS = [10, 25, 50, 100]

export default function PreferencesPage() {
  const { user, updatePreferences } = useAuth()
  const theme = user?.preferences?.theme || 'light'
  const pageSize = user?.preferences?.page_size || 10

  return (
    <div>
      <PageHeader title="Preferences" subtitle="Personal settings" />

      <div className="space-y-6">
        <Card className="p-6">
          <h2 className="mb-1 text-sm font-medium text-gray-900 dark:text-gray-100">Theme</h2>
          <p className="mb-3 text-sm text-gray-500 dark:text-gray-400">Choose between light and dark appearance.</p>
          <div className="flex space-x-2">
            <button
              onClick={() => updatePreferences({ theme: 'light' })}
              className={`rounded-lg px-4 py-2 text-sm font-medium ${
                theme === 'light'
                  ? 'bg-blue-600 text-white shadow-xs'
                  : 'bg-(--color-bg-tertiary) text-gray-700 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-600'
              }`}
            >
              Light
            </button>
            <button
              onClick={() => updatePreferences({ theme: 'dark' })}
              className={`rounded-lg px-4 py-2 text-sm font-medium ${
                theme === 'dark'
                  ? 'bg-blue-600 text-white shadow-xs'
                  : 'bg-(--color-bg-tertiary) text-gray-700 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-600'
              }`}
            >
              Dark
            </button>
          </div>
        </Card>

        <Card className="p-6">
          <h2 className="mb-1 text-sm font-medium text-gray-900 dark:text-gray-100">Default page size</h2>
          <p className="mb-3 text-sm text-gray-500 dark:text-gray-400">Number of items shown per page by default.</p>
          <select
            value={pageSize}
            onChange={(e) => updatePreferences({ page_size: Number(e.target.value) })}
            className="rounded-lg border-0 bg-(--color-bg-secondary) dark:bg-(--color-bg-tertiary) shadow-mac focus:ring-2 focus:ring-(--color-accent)/30 px-3 py-2 text-sm text-gray-900 dark:text-gray-100"
          >
            {PAGE_SIZE_OPTIONS.map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </Card>

        <ChangePasswordCard />
      </div>
    </div>
  )
}

function ChangePasswordCard() {
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [confirm, setConfirm] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState(false)

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    setError(null)
    setSuccess(false)
    if (next !== confirm) {
      setError('New password and confirmation do not match.')
      return
    }
    setSubmitting(true)
    try {
      await post('/api/me/password', { current_password: current, new_password: next })
      setSuccess(true)
      setCurrent('')
      setNext('')
      setConfirm('')
    } catch (e) {
      // The api.post helper rejects on non-2xx with the response text.
      // The endpoint returns either a string detail or {detail: {errors: [...]}}
      // for password-validation failures.
      let msg = 'Failed to change password.'
      if (e instanceof Error) {
        msg = e.message
        try {
          const parsed = JSON.parse(msg)
          if (parsed?.detail?.errors && Array.isArray(parsed.detail.errors)) {
            msg = parsed.detail.errors.join(' • ')
          } else if (typeof parsed?.detail === 'string') {
            msg = parsed.detail
          }
        } catch {
          // not JSON — leave msg as-is
        }
      }
      setError(msg)
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Card className="p-6">
      <h2 className="mb-1 text-sm font-medium text-gray-900 dark:text-gray-100">Change password</h2>
      <p className="mb-3 text-sm text-gray-500 dark:text-gray-400">
        Requires your current password. New password must be at least 12 characters and contain upper, lower, and digit.
      </p>
      <form onSubmit={handleSubmit} className="space-y-3 max-w-md">
        <input
          type="password"
          autoComplete="current-password"
          placeholder="Current password"
          value={current}
          onChange={(e) => setCurrent(e.target.value)}
          required
          className="w-full rounded-lg border-0 bg-(--color-bg-secondary) dark:bg-(--color-bg-tertiary) shadow-mac focus:ring-2 focus:ring-(--color-accent)/30 px-3 py-2 text-sm text-gray-900 dark:text-gray-100"
        />
        <input
          type="password"
          autoComplete="new-password"
          placeholder="New password"
          value={next}
          onChange={(e) => setNext(e.target.value)}
          required
          minLength={12}
          className="w-full rounded-lg border-0 bg-(--color-bg-secondary) dark:bg-(--color-bg-tertiary) shadow-mac focus:ring-2 focus:ring-(--color-accent)/30 px-3 py-2 text-sm text-gray-900 dark:text-gray-100"
        />
        <input
          type="password"
          autoComplete="new-password"
          placeholder="Confirm new password"
          value={confirm}
          onChange={(e) => setConfirm(e.target.value)}
          required
          minLength={12}
          className="w-full rounded-lg border-0 bg-(--color-bg-secondary) dark:bg-(--color-bg-tertiary) shadow-mac focus:ring-2 focus:ring-(--color-accent)/30 px-3 py-2 text-sm text-gray-900 dark:text-gray-100"
        />
        {error && (
          <div className="rounded-md bg-red-50 dark:bg-red-900/20 px-3 py-2 text-sm text-red-700 dark:text-red-400">
            {error}
          </div>
        )}
        {success && (
          <div className="rounded-md bg-green-50 dark:bg-green-900/20 px-3 py-2 text-sm text-green-700 dark:text-green-400">
            Password updated.
          </div>
        )}
        <button
          type="submit"
          disabled={submitting || !current || !next || !confirm}
          className="rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white shadow-xs hover:bg-blue-700 disabled:opacity-50"
        >
          {submitting ? 'Updating…' : 'Update password'}
        </button>
      </form>
    </Card>
  )
}

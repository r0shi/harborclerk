import { FormEvent, useState } from 'react'
import { Navigate } from 'react-router-dom'
import { useAuth } from '../auth'

export default function LoginPage() {
  const { user, loading, login } = useAuth()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)

  if (loading) {
    return (
      <div className="flex h-screen items-center justify-center">
        <div className="text-gray-500 dark:text-gray-400">Loading...</div>
      </div>
    )
  }

  if (user) {
    return <Navigate to="/" replace />
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    setError('')
    setSubmitting(true)
    try {
      await login(email, password)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Login failed')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-[var(--color-bg-secondary)]">
      <div className="w-full max-w-sm">
        <img src="/logo.png" alt="Harbor Clerk" className="mx-auto mb-4 h-16 object-contain" />
        <h1 className="mb-6 text-center text-2xl font-bold text-gray-900 dark:text-gray-100">Harbor Clerk</h1>
        <form onSubmit={handleSubmit} className="rounded-xl bg-white dark:bg-[#2c2c2e] p-6 shadow-mac-lg">
          {error && (
            <div className="mb-4 rounded bg-red-50 dark:bg-red-900/20 px-3 py-2 text-sm text-red-700 dark:text-red-400">
              {error}
            </div>
          )}
          <label className="mb-1 block text-sm font-medium text-gray-700 dark:text-gray-300">Email</label>
          <input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
            autoFocus
            autoComplete="username"
            autoCapitalize="off"
            className="mb-4 w-full rounded-lg border-0 bg-[var(--color-bg-secondary)] dark:bg-[var(--color-bg-tertiary)] shadow-mac focus:ring-2 focus:ring-[var(--color-accent)]/30 px-3 py-2 text-sm focus:outline-none"
          />
          <label className="mb-1 block text-sm font-medium text-gray-700 dark:text-gray-300">Password</label>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
            autoComplete="current-password"
            autoCapitalize="off"
            className="mb-6 w-full rounded-lg border-0 bg-[var(--color-bg-secondary)] dark:bg-[var(--color-bg-tertiary)] shadow-mac focus:ring-2 focus:ring-[var(--color-accent)]/30 px-3 py-2 text-sm focus:outline-none"
          />
          <button
            type="submit"
            disabled={submitting}
            className="w-full rounded-lg bg-blue-600 px-4 py-2 text-sm font-medium text-white shadow-sm hover:bg-blue-700 disabled:opacity-50"
          >
            {submitting ? 'Signing in...' : 'Sign in'}
          </button>
        </form>
      </div>
    </div>
  )
}

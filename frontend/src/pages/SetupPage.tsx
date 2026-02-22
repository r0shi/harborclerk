import { FormEvent, useMemo, useState } from 'react'
import { Navigate } from 'react-router-dom'
import { useAuth } from '../auth'

interface PasswordCheck {
  label: string
  test: (pw: string, email: string) => boolean
}

const PASSWORD_CHECKS: PasswordCheck[] = [
  { label: 'At least 12 characters', test: (pw) => pw.length >= 12 },
  {
    label: 'Does not contain email',
    test: (pw, email) =>
      !email || !pw.toLowerCase().includes(email.toLowerCase()),
  },
  { label: 'One uppercase letter', test: (pw) => /[A-Z]/.test(pw) },
  { label: 'One lowercase letter', test: (pw) => /[a-z]/.test(pw) },
  { label: 'One digit', test: (pw) => /\d/.test(pw) },
  {
    label: 'Not a single repeated character',
    test: (pw) => pw.length === 0 || new Set(pw).size > 1,
  },
]

export default function SetupPage() {
  const { user, loading, needsSetup } = useAuth()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)

  const checks = useMemo(
    () => PASSWORD_CHECKS.map((c) => ({ ...c, passed: c.test(password, email) })),
    [password, email],
  )
  const allPassed = password.length > 0 && checks.every((c) => c.passed)
  const passwordsMatch = password === confirmPassword

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

  if (!needsSetup) {
    return <Navigate to="/login" replace />
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    if (!allPassed || !passwordsMatch) return
    setError('')
    setSubmitting(true)
    try {
      const res = await fetch('/api/setup', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({ email, password }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({ detail: res.statusText }))
        const detail = data.detail
        throw new Error(Array.isArray(detail) ? detail.join('. ') : detail)
      }
      // Reload to let AuthProvider pick up the refresh cookie
      window.location.href = '/'
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Setup failed')
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-gray-50 dark:bg-gray-900">
      <div className="w-full max-w-sm">
        <img src="/logo.png" alt="Harbor Clerk" className="mx-auto mb-4 h-16 object-contain" />
        <h1 className="mb-2 text-center text-2xl font-bold text-gray-900 dark:text-gray-100">
          Harbor Clerk
        </h1>
        <p className="mb-6 text-center text-sm text-gray-500 dark:text-gray-400">
          Create your admin account to get started.
        </p>
        <form
          onSubmit={handleSubmit}
          className="rounded-lg bg-white dark:bg-gray-800 p-6 shadow-md"
        >
          {error && (
            <div className="mb-4 rounded bg-red-50 dark:bg-red-900/20 px-3 py-2 text-sm text-red-700 dark:text-red-400">
              {error}
            </div>
          )}
          <label className="mb-1 block text-sm font-medium text-gray-700 dark:text-gray-300">
            Email
          </label>
          <input
            type="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
            autoFocus
            autoComplete="username"
            autoCapitalize="off"
            className="mb-4 w-full rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
          />
          <label className="mb-1 block text-sm font-medium text-gray-700 dark:text-gray-300">
            Password
          </label>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
            autoComplete="new-password"
            autoCapitalize="off"
            passwordrules="minlength: 12; required: upper; required: lower; required: digit;"
            className="mb-2 w-full rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
          />
          {password.length > 0 && (
            <ul className="mb-4 space-y-1 text-xs">
              {checks.map((c) => (
                <li
                  key={c.label}
                  className={c.passed ? 'text-green-600 dark:text-green-400' : 'text-red-500 dark:text-red-400'}
                >
                  {c.passed ? '\u2713' : '\u2717'} {c.label}
                </li>
              ))}
            </ul>
          )}
          <label className="mb-1 block text-sm font-medium text-gray-700 dark:text-gray-300">
            Confirm password
          </label>
          <input
            type="password"
            value={confirmPassword}
            onChange={(e) => setConfirmPassword(e.target.value)}
            required
            autoComplete="new-password"
            autoCapitalize="off"
            passwordrules="minlength: 12; required: upper; required: lower; required: digit;"
            className="mb-1 w-full rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
          />
          {confirmPassword.length > 0 && !passwordsMatch && (
            <p className="mb-4 text-xs text-red-500 dark:text-red-400">Passwords do not match</p>
          )}
          <button
            type="submit"
            disabled={submitting || !allPassed || !passwordsMatch}
            className="mt-4 w-full rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
          >
            {submitting ? 'Creating account...' : 'Create admin account'}
          </button>
        </form>
      </div>
    </div>
  )
}

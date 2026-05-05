/**
 * /admin/system/security
 *
 * Surfaces the deployment's secret-storage health:
 *   - List of mail accounts whose stored ciphertext can't be decrypted
 *     with the current master key (key_mismatch state).
 *   - Documentation pointer for what to do (re-enter app passwords, or
 *     import the prior master key from another deployment).
 *
 * Stage 4 ships this as a read-only health view. The "Import master key"
 * admin form is a planned follow-up — it requires a backend endpoint that
 * lives outside Stage 4's scope.
 */

import { useEffect, useState } from 'react'

import { listMailAccounts } from '../api/mail'
import type { MailAccountResponse } from '../types/mail'
import BackButton from '../components/BackButton'
import { PageHeader } from '../components/PageHeader'

export default function SecuritySettingsPage() {
  const [accounts, setAccounts] = useState<MailAccountResponse[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    async function load() {
      try {
        const result = await listMailAccounts()
        if (!cancelled) setAccounts(result)
      } catch {
        if (!cancelled) setAccounts([])
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    void load()
    return () => {
      cancelled = true
    }
  }, [])

  const keyMismatchAccounts = accounts.filter((a) => a.status === 'key_mismatch')

  return (
    <div className="max-w-3xl mx-auto px-6 py-6">
      <BackButton />
      <PageHeader title="Security" />

      <section className="mb-8">
        <h2 className="text-base font-medium mb-2">Encryption status</h2>
        {loading ? (
          <p className="text-sm text-(--color-text-secondary)">Loading…</p>
        ) : keyMismatchAccounts.length === 0 ? (
          <div className="rounded-md border border-(--color-border) bg-(--color-bg-secondary) p-3 text-sm">
            <p>All mail account credentials decrypt cleanly with the current master key.</p>
          </div>
        ) : (
          <div className="rounded-md border border-red-300 bg-red-50 dark:border-red-700 dark:bg-red-900/20 p-3 text-sm">
            <p className="font-medium text-red-800 dark:text-red-300 mb-2">
              {keyMismatchAccounts.length} mail account{keyMismatchAccounts.length === 1 ? '' : 's'} need reconnecting
            </p>
            <p className="text-(--color-text-primary) mb-3">
              The credentials for these accounts were encrypted with a different master key than the one currently
              loaded — typically because this database was moved between deployments without carrying the master key
              over.
            </p>
            <ul className="list-disc list-inside text-(--color-text-primary) mb-3">
              {keyMismatchAccounts.map((a) => (
                <li key={a.account_id}>
                  <strong>{a.display_name}</strong> ({a.imap_username})
                </li>
              ))}
            </ul>
            <p className="text-(--color-text-primary)">
              Go to{' '}
              <a href="/folders" className="text-(--color-accent) hover:underline">
                Folders
              </a>{' '}
              and click each affected account to re-enter its app password.
            </p>
          </div>
        )}
      </section>

      <section className="mb-8">
        <h2 className="text-base font-medium mb-2">Master key</h2>
        <p className="text-sm text-(--color-text-primary) mb-2">
          Sensitive values (mail-account app passwords, future OAuth tokens) are encrypted at rest in PostgreSQL with a
          32-byte master key.
        </p>
        <p className="text-sm text-(--color-text-secondary)">
          On macOS the key is generated on first launch and stored in your login Keychain. On Docker the operator sets
          it via the <code className="rounded bg-(--color-bg-secondary) px-1">HARBOR_CLERK_MASTER_KEY</code> environment
          variable. See the operator guide for details.
        </p>
        <p className="mt-3 text-xs text-(--color-text-secondary)">
          A future release will add an &ldquo;Import master key&rdquo; form here for migrating credentials between
          deployments without re-entering app passwords. For now, re-entry is the only restoration path.
        </p>
      </section>
    </div>
  )
}

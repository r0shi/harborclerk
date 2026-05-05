// frontend/src/components/EmailSection.tsx
/**
 * "Email" section for the /folders page. Lists every mail account with its
 * watched labels nested under it. Each row supports test-connection,
 * rescan (label only), and remove.
 *
 * The "Add email source" button opens AddEmailSourceWizard. On wizard
 * completion, the section reloads to pick up the new account + labels.
 */

import { useEffect, useState } from 'react'

import {
  deleteMailAccount,
  deleteWatchedLabel,
  listMailAccounts,
  listWatchedLabels,
  rescanWatchedLabel,
  testMailAccount,
} from '../api/mail'
import type { MailAccountResponse, WatchedLabelResponse } from '../types/mail'
import AddEmailSourceWizard from './AddEmailSourceWizard'
import { StatusPill, type PillState } from './StatusPill'

export default function EmailSection() {
  const [accounts, setAccounts] = useState<MailAccountResponse[]>([])
  const [labels, setLabels] = useState<WatchedLabelResponse[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [showWizard, setShowWizard] = useState(false)
  const [busy, setBusy] = useState<string | null>(null)
  // Two-click confirmation state. window.confirm() is unusable here because the
  // macOS native app wraps this UI in a WKWebView, where window.confirm silently
  // returns false (see CLAUDE.md "Gotchas & Fixes"). Same pattern as FoldersPage.
  const [pendingDeleteAccountId, setPendingDeleteAccountId] = useState<string | null>(null)
  const [pendingDeleteLabelId, setPendingDeleteLabelId] = useState<string | null>(null)

  async function reload() {
    setLoading(true)
    setError(null)
    try {
      const [a, l] = await Promise.all([listMailAccounts(), listWatchedLabels()])
      setAccounts(a)
      setLabels(l)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    let cancelled = false
    async function load() {
      setLoading(true)
      setError(null)
      try {
        const [a, l] = await Promise.all([listMailAccounts(), listWatchedLabels()])
        if (cancelled) return
        setAccounts(a)
        setLabels(l)
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e))
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    void load()
    return () => {
      cancelled = true
    }
  }, [])

  async function handleTest(accountId: string) {
    setBusy(accountId)
    try {
      await testMailAccount(accountId)
    } finally {
      setBusy(null)
      await reload()
    }
  }

  async function handleRescan(labelId: string) {
    setBusy(labelId)
    try {
      await rescanWatchedLabel(labelId)
    } finally {
      setBusy(null)
    }
  }

  async function handleRemoveAccount(accountId: string) {
    if (pendingDeleteAccountId !== accountId) {
      setPendingDeleteAccountId(accountId)
      setTimeout(() => {
        setPendingDeleteAccountId((cur) => (cur === accountId ? null : cur))
      }, 5000)
      return
    }
    setPendingDeleteAccountId(null)
    setBusy(accountId)
    try {
      await deleteMailAccount(accountId)
    } finally {
      setBusy(null)
      await reload()
    }
  }

  async function handleRemoveLabel(labelId: string) {
    if (pendingDeleteLabelId !== labelId) {
      setPendingDeleteLabelId(labelId)
      setTimeout(() => {
        setPendingDeleteLabelId((cur) => (cur === labelId ? null : cur))
      }, 5000)
      return
    }
    setPendingDeleteLabelId(null)
    setBusy(labelId)
    try {
      await deleteWatchedLabel(labelId)
    } finally {
      setBusy(null)
      await reload()
    }
  }

  return (
    <section className="mt-8">
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-base font-medium text-(--color-text-primary)">Email</h2>
        <button
          type="button"
          onClick={() => setShowWizard(true)}
          className="rounded-lg bg-(--color-accent) px-3 py-1.5 text-sm font-medium text-white hover:opacity-90"
        >
          Add email source
        </button>
      </div>

      {loading && <p className="text-sm text-(--color-text-secondary)">Loading…</p>}
      {error && (
        <p className="text-sm text-red-600 dark:text-red-400" role="alert">
          {error}
        </p>
      )}

      {!loading && accounts.length === 0 && (
        <p className="text-sm text-(--color-text-secondary)">
          No email accounts connected. Add one to start syncing labeled emails into your corpus.
        </p>
      )}

      <div className="space-y-3">
        {accounts.map((account) => {
          const accountLabels = labels.filter((l) => l.account_id === account.account_id)
          return (
            <div
              key={account.account_id}
              className="rounded-[10px] border border-(--color-border) bg-(--color-bg-primary)"
            >
              <div className="px-4 py-3 flex items-center justify-between">
                <div>
                  <div className="font-medium text-sm">{account.display_name}</div>
                  <div className="text-xs text-(--color-text-secondary)">{account.imap_username}</div>
                  {account.last_error && (
                    <div className="text-xs text-red-600 dark:text-red-400 mt-1">{account.last_error}</div>
                  )}
                </div>
                <div className="flex items-center gap-2">
                  <StatusPill state={mapAccountStatus(account.status)} label={labelForAccountStatus(account.status)} />
                  <button
                    type="button"
                    onClick={() => handleTest(account.account_id)}
                    disabled={busy !== null}
                    className="text-xs rounded-md border border-(--color-border) px-2 py-1 hover:bg-(--color-bg-secondary) disabled:opacity-50"
                  >
                    {busy === account.account_id ? 'Testing…' : 'Test'}
                  </button>
                  <button
                    type="button"
                    onClick={() => handleRemoveAccount(account.account_id)}
                    disabled={busy !== null}
                    className="text-xs rounded-md border border-(--color-border) px-2 py-1 text-red-600 hover:bg-red-50 dark:hover:bg-red-900/20 disabled:opacity-50"
                    title="Existing Documents stay in the corpus; only future syncs stop"
                  >
                    {pendingDeleteAccountId === account.account_id ? 'Click again to confirm' : 'Remove'}
                  </button>
                </div>
              </div>
              {accountLabels.length > 0 && (
                <ul className="border-t border-(--color-border)">
                  {accountLabels.map((label) => (
                    <li
                      key={label.label_id}
                      className="px-4 py-2 flex items-center justify-between hover:bg-(--color-bg-secondary)"
                    >
                      <div className="flex items-center gap-2">
                        <span className="text-sm">{label.label_path}</span>
                        {label.last_synced_at && (
                          <span className="text-xs text-(--color-text-secondary)">
                            last synced {new Date(label.last_synced_at).toLocaleString()}
                          </span>
                        )}
                      </div>
                      <div className="flex items-center gap-2">
                        <StatusPill state={mapLabelStatus(label.status)} label={label.status} />
                        <button
                          type="button"
                          onClick={() => handleRescan(label.label_id)}
                          disabled={busy !== null}
                          className="text-xs rounded-md border border-(--color-border) px-2 py-1 hover:bg-(--color-bg-secondary) disabled:opacity-50"
                          title="Reset cursor and re-sync the label from scratch"
                        >
                          Rescan
                        </button>
                        <button
                          type="button"
                          onClick={() => handleRemoveLabel(label.label_id)}
                          disabled={busy !== null}
                          className="text-xs rounded-md border border-(--color-border) px-2 py-1 text-red-600 hover:bg-red-50 dark:hover:bg-red-900/20 disabled:opacity-50"
                          title="Existing Documents stay in the corpus"
                        >
                          {pendingDeleteLabelId === label.label_id ? 'Click again to confirm' : 'Remove'}
                        </button>
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )
        })}
      </div>

      {showWizard && (
        <AddEmailSourceWizard
          onCancel={() => setShowWizard(false)}
          onCompleted={() => {
            setShowWizard(false)
            void reload()
          }}
        />
      )}
    </section>
  )
}

function mapAccountStatus(status: MailAccountResponse['status']): PillState {
  switch (status) {
    case 'active':
      return 'active'
    case 'paused':
      return 'pending'
    case 'auth_error':
    case 'key_mismatch':
      return 'error'
  }
}

function labelForAccountStatus(status: MailAccountResponse['status']): string {
  switch (status) {
    case 'active':
      return 'Active'
    case 'paused':
      return 'Paused'
    case 'auth_error':
      return 'Auth error'
    case 'key_mismatch':
      return 'Key mismatch'
  }
}

function mapLabelStatus(status: WatchedLabelResponse['status']): PillState {
  switch (status) {
    case 'active':
      return 'active'
    case 'paused':
      return 'pending'
    case 'error':
      return 'error'
  }
}

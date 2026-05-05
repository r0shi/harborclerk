// frontend/src/components/AddEmailSourceWizard.tsx
/**
 * Three-step modal wizard for adding an email source.
 *
 *   Step 1: Pick provider (Gmail / iCloud / Fastmail / Yahoo / Other)
 *   Step 2: Enter credentials + Test Connection
 *   Step 3: Pick labels to watch (multi-select tree)
 *
 * On completion, calls onCompleted(account_id) so the caller can refresh
 * its account/label lists.
 */

import { useState } from 'react'

import { createMailAccount, deleteMailAccount, testMailAccount } from '../api/mail'
import type { FolderInfo } from '../types/mail'
import { PROVIDER_PRESETS, type ProviderPreset } from '../data/mailProviders'

type Step = 'provider' | 'credentials' | 'labels'

export interface AddEmailSourceWizardProps {
  onCancel: () => void
  onCompleted: (accountId: string) => void
}

export default function AddEmailSourceWizard({ onCancel, onCompleted }: AddEmailSourceWizardProps) {
  const [step, setStep] = useState<Step>('provider')
  const [selectedProvider, setSelectedProvider] = useState<ProviderPreset | null>(null)
  const [createdAccountId, setCreatedAccountId] = useState<string | null>(null)
  const [discoveredFolders, setDiscoveredFolders] = useState<FolderInfo[]>([])

  function handleProviderPicked(preset: ProviderPreset) {
    setSelectedProvider(preset)
    setStep('credentials')
  }

  return (
    <div
      role="dialog"
      aria-modal="true"
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40"
      onClick={(e) => {
        if (e.target === e.currentTarget) onCancel()
      }}
    >
      <div className="w-full max-w-2xl rounded-[10px] bg-(--color-bg-primary) shadow-xl">
        <div className="border-b border-(--color-border) px-6 py-4 flex items-center justify-between">
          <h2 className="text-lg font-medium text-(--color-text-primary)">Add email source</h2>
          <button
            type="button"
            onClick={onCancel}
            className="rounded-md p-1 text-(--color-text-secondary) hover:bg-(--color-bg-secondary)"
            aria-label="Close"
          >
            ×
          </button>
        </div>

        {step === 'provider' && <ProviderStep onPick={handleProviderPicked} />}
        {step === 'credentials' && selectedProvider && (
          <CredentialsStep
            preset={selectedProvider}
            onBack={() => setStep('provider')}
            onConnected={(accountId, folders) => {
              setCreatedAccountId(accountId)
              setDiscoveredFolders(folders)
              setStep('labels')
            }}
          />
        )}
        {step === 'labels' && createdAccountId && (
          <LabelsStep
            accountId={createdAccountId}
            folders={discoveredFolders}
            onBack={() => setStep('credentials')}
            onCompleted={() => onCompleted(createdAccountId)}
          />
        )}
      </div>
    </div>
  )
}

function ProviderStep({ onPick }: { onPick: (preset: ProviderPreset) => void }) {
  return (
    <div className="px-6 py-5">
      <p className="text-sm text-(--color-text-secondary) mb-4">
        Pick the provider that hosts the email account you want to connect.
      </p>
      <div className="grid grid-cols-2 gap-3">
        {PROVIDER_PRESETS.map((preset) => (
          <button
            key={preset.id}
            type="button"
            onClick={() => onPick(preset)}
            className="text-left rounded-lg border border-(--color-border) p-4 hover:border-(--color-accent) hover:bg-(--color-bg-secondary) transition-colors"
          >
            <div className="font-medium text-(--color-text-primary)">{preset.name}</div>
            <div className="text-xs text-(--color-text-secondary) mt-1">{preset.description}</div>
          </button>
        ))}
      </div>
    </div>
  )
}

function CredentialsStep({
  preset,
  onBack,
  onConnected,
}: {
  preset: ProviderPreset
  onBack: () => void
  onConnected: (accountId: string, folders: FolderInfo[]) => void
}) {
  const [displayName, setDisplayName] = useState('')
  const [imapHost, setImapHost] = useState(preset.imap_host)
  const [imapPort, setImapPort] = useState(preset.imap_port)
  const [imapUsername, setImapUsername] = useState('')
  const [appPassword, setAppPassword] = useState('')
  const [testing, setTesting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const isGeneric = preset.id === 'generic'
  const canSubmit = !!(displayName && imapHost && imapPort && imapUsername && appPassword) && !testing

  async function handleTest() {
    setTesting(true)
    setError(null)
    let createdAccountId: string | null = null
    try {
      const account = await createMailAccount({
        display_name: displayName,
        provider: preset.id,
        imap_host: imapHost,
        imap_port: imapPort,
        imap_username: imapUsername,
        app_password: appPassword,
      })
      createdAccountId = account.account_id
      const result = await testMailAccount(account.account_id)
      if (!result.success) {
        setError(result.error || 'Connection failed')
        return
      }
      onConnected(account.account_id, result.folders)
    } catch (e) {
      if (createdAccountId !== null) {
        try {
          await deleteMailAccount(createdAccountId)
        } catch {
          /* ignore cleanup failures */
        }
      }
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setTesting(false)
    }
  }

  return (
    <div className="px-6 py-5">
      <h3 className="text-sm font-medium mb-2">{preset.name}</h3>
      {preset.app_password_help_url && (
        <p className="text-xs text-(--color-text-secondary) mb-4">
          Need an app password?{' '}
          <a
            href={preset.app_password_help_url}
            target="_blank"
            rel="noreferrer"
            className="text-(--color-accent) underline"
          >
            {preset.app_password_help_label}
          </a>
        </p>
      )}

      <div className="space-y-3">
        <Field label="Display name">
          <input
            type="text"
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            placeholder={preset.id === 'gmail' ? 'Personal Gmail' : 'My account'}
            className="input-base"
          />
        </Field>
        <Field label="Email address">
          <input
            type="email"
            value={imapUsername}
            onChange={(e) => setImapUsername(e.target.value)}
            placeholder="you@example.com"
            className="input-base"
            autoComplete="username"
          />
        </Field>
        <Field label="App password">
          <input
            type="password"
            value={appPassword}
            onChange={(e) => setAppPassword(e.target.value)}
            placeholder="xxxx-xxxx-xxxx-xxxx"
            className="input-base"
            autoComplete="new-password"
          />
        </Field>
        {isGeneric && (
          <div className="grid grid-cols-2 gap-3">
            <Field label="IMAP host">
              <input
                type="text"
                value={imapHost}
                onChange={(e) => setImapHost(e.target.value)}
                placeholder="imap.example.com"
                className="input-base"
              />
            </Field>
            <Field label="Port">
              <input
                type="number"
                value={imapPort}
                onChange={(e) => setImapPort(Number(e.target.value))}
                min={1}
                max={65535}
                className="input-base"
              />
            </Field>
          </div>
        )}
      </div>

      {error && (
        <p className="mt-3 text-sm text-red-600 dark:text-red-400" role="alert">
          {error}
        </p>
      )}

      <div className="mt-5 flex justify-between">
        <button
          type="button"
          onClick={onBack}
          disabled={testing}
          className="rounded-lg border border-(--color-border) px-3 py-1.5 text-sm disabled:opacity-50"
        >
          Back
        </button>
        <button
          type="button"
          onClick={handleTest}
          disabled={!canSubmit}
          className="rounded-lg bg-(--color-accent) px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
        >
          {testing ? 'Testing…' : 'Test connection'}
        </button>
      </div>
    </div>
  )
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="text-xs font-medium text-(--color-text-secondary) block mb-1">{label}</span>
      {children}
    </label>
  )
}

function LabelsStep({
  accountId: _accountId,
  folders,
  onBack,
  onCompleted,
}: {
  accountId: string
  folders: FolderInfo[]
  onBack: () => void
  onCompleted: () => void
}) {
  return (
    <div className="px-6 py-5">
      <p>Stub: {folders.length} folders</p>
      <button onClick={onBack}>Back</button>
      <button onClick={onCompleted}>Done</button>
    </div>
  )
}

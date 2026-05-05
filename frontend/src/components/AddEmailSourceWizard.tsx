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
  return (
    <div className="px-6 py-5">
      <p className="text-sm">Stub: {preset.name}</p>
      <button onClick={onBack}>Back</button>
      <button onClick={() => onConnected('stub', [])}>Continue</button>
    </div>
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

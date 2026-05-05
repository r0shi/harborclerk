# Email Ingestion — Stage 4: UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make email ingestion accessible to non-technical office staff: an "Add email source" wizard for connecting Gmail/iCloud/Fastmail/Yahoo accounts and picking labels to watch, an Email section on the existing `/folders` page surfacing connection state, an email-metadata block on the document detail page (with a "View in Gmail" deep link), and System Settings affordances for encryption status and key import.

**Architecture:** All Stage 4 work is in `frontend/`. New types in `frontend/src/types/mail.ts` mirror the Pydantic schemas from Stage 2's `harbor_clerk.api.schemas.mail`. The wizard is a single new component `frontend/src/components/AddEmailSourceWizard.tsx` (multi-step modal with provider tile picker → credentials + test → label multi-select). The Folders page gets a new "Email" sibling section listing accounts and watched labels alongside filesystem folders. DocumentDetailPage gets an `EmailMetadataSection` component that conditionally renders for Documents with `email_message_id`. System Settings gets a new `/admin/system/security` page for encryption status and key import.

**Tech Stack:** React 19, React Router 7, Tailwind CSS 4.2 (CSS-first config), Vite 7.3, TypeScript 5. Uses existing `api.ts` helpers (`get`, `post`, `del`), `useAuth` for admin gating, and the existing UI primitives (`StatusPill`, `Card`, `IconTile`, `PageHeader`, `BackButton`).

**Spec:** [`docs/superpowers/specs/2026-05-04-email-ingestion-design.md`](../specs/2026-05-04-email-ingestion-design.md)

**Builds on:** Stage 1 ([PR #281](https://github.com/r0shi/harborclerk/pull/281)), Stage 2 ([PR #282](https://github.com/r0shi/harborclerk/pull/282)), Stage 3 ([PR #283](https://github.com/r0shi/harborclerk/pull/283)) — schema, API endpoints, sync engine, ingest pipeline. After Stage 4 lands, the entire email-ingestion feature is end-to-end usable by non-admin office staff (admins for setup; non-admins read the resulting Documents).

**Implementation note:** The frontend has no automated test suite (per the existing pattern — `frontend/src/__tests__/` doesn't exist, only ad-hoc tests). Verification per task is `npm run lint && npm run type-check && npm run build`, plus manual browser testing against a running dev server when behavior is interactive. This plan reflects that reality — tasks include lint/type/build steps as the green/red signal, with manual verification steps for interactive flows.

---

## File Structure

**New files:**
- `frontend/src/types/mail.ts` — TypeScript interfaces mirroring the Pydantic schemas from Stage 2
- `frontend/src/api/mail.ts` — typed API client functions (`createMailAccount`, `listMailAccounts`, `testMailAccount`, `listWatchedLabels`, `createWatchedLabel`, `deleteWatchedLabel`, `rescanWatchedLabel`, `deleteMailAccount`)
- `frontend/src/components/AddEmailSourceWizard.tsx` — the multi-step modal
- `frontend/src/components/EmailSection.tsx` — Email section for the Folders page (lists accounts + watched labels)
- `frontend/src/components/EmailMetadataSection.tsx` — From/To/Cc/Date/Subject + "View in Gmail" link for DocumentDetailPage
- `frontend/src/pages/SecuritySettingsPage.tsx` — `/admin/system/security` page
- `frontend/src/components/MailAccountStatusRow.tsx` — one row in the System Status mail-accounts table

**Modified files:**
- `frontend/src/pages/FoldersPage.tsx` — add `<EmailSection />` below the existing filesystem-folders section
- `frontend/src/pages/DocumentDetailPage.tsx` — render `<EmailMetadataSection />` when the doc is an email or attachment
- `frontend/src/pages/SystemStatusPage.tsx` — add a "Mail Accounts" subsection
- `frontend/src/pages/SystemSettingsPage.tsx` — add a "Security" tile linking to `/admin/system/security`
- `frontend/src/App.tsx` — register `/admin/system/security` route

---

## Task 1: TypeScript types for the mail API surface

**Files:**
- Create: `frontend/src/types/mail.ts`

- [ ] **Step 1: Create the types**

```typescript
// frontend/src/types/mail.ts
/**
 * TypeScript interfaces for /api/mail/* — mirrors the Pydantic schemas in
 * src/harbor_clerk/api/schemas/mail.py. Keep them in sync when the Python
 * side changes.
 */

export type ProviderName = 'gmail' | 'icloud' | 'fastmail' | 'yahoo' | 'generic'
export type AccountStatus = 'active' | 'auth_error' | 'key_mismatch' | 'paused'
export type LabelStatus = 'active' | 'paused' | 'error'

export interface MailAccountCreate {
  display_name: string
  provider: ProviderName
  imap_host: string
  imap_port: number
  imap_username: string
  app_password: string
}

export interface MailAccountResponse {
  account_id: string
  display_name: string
  provider: ProviderName
  imap_host: string
  imap_port: number
  imap_username: string
  status: AccountStatus
  last_error: string | null
  last_connected_at: string | null
  created_at: string
}

export interface FolderInfo {
  path: string
  display_name: string
  is_system: boolean
  has_children: boolean
}

export interface TestConnectionResponse {
  success: boolean
  error: string | null
  folders: FolderInfo[]
}

export interface WatchedLabelCreate {
  account_id: string
  label_path: string
  display_name: string
}

export interface WatchedLabelResponse {
  label_id: string
  account_id: string
  label_path: string
  display_name: string
  status: LabelStatus
  last_error: string | null
  last_synced_at: string | null
  last_uid_seen: number
  uidvalidity: number | null
  created_at: string
}
```

- [ ] **Step 2: Verify**

```bash
cd /Users/alex/mcp-gateway/.worktrees/email-stage4/frontend && npm run type-check 2>&1 | tail -3
```

Expected: clean (the file just declares types; no other code uses them yet).

- [ ] **Step 3: Commit**

```bash
git add frontend/src/types/mail.ts
git commit -m "feat(frontend): TypeScript types mirroring /api/mail/* schemas"
```

---

## Task 2: API client functions

**Files:**
- Create: `frontend/src/api/mail.ts`

- [ ] **Step 1: Create the client**

```typescript
// frontend/src/api/mail.ts
/**
 * Typed wrappers around the /api/mail/* endpoints.
 *
 * The underlying `get`/`post`/`del` helpers from `../api.ts` already handle
 * auth headers, build-hash mismatch detection, and 401 refresh — these
 * functions add only typing + URL construction.
 */

import { del, get, post } from '../api'
import type {
  MailAccountCreate,
  MailAccountResponse,
  TestConnectionResponse,
  WatchedLabelCreate,
  WatchedLabelResponse,
} from '../types/mail'

export async function listMailAccounts(): Promise<MailAccountResponse[]> {
  return get('/api/mail/accounts')
}

export async function createMailAccount(body: MailAccountCreate): Promise<MailAccountResponse> {
  return post('/api/mail/accounts', body)
}

export async function deleteMailAccount(accountId: string): Promise<void> {
  return del(`/api/mail/accounts/${accountId}`)
}

export async function testMailAccount(accountId: string): Promise<TestConnectionResponse> {
  return post(`/api/mail/accounts/${accountId}/test`)
}

export async function listWatchedLabels(): Promise<WatchedLabelResponse[]> {
  return get('/api/mail/labels')
}

export async function createWatchedLabel(body: WatchedLabelCreate): Promise<WatchedLabelResponse> {
  return post('/api/mail/labels', body)
}

export async function deleteWatchedLabel(labelId: string): Promise<void> {
  return del(`/api/mail/labels/${labelId}`)
}

export async function rescanWatchedLabel(labelId: string): Promise<{ status: string }> {
  return post(`/api/mail/labels/${labelId}/rescan`)
}
```

- [ ] **Step 2: Verify**

```bash
cd /Users/alex/mcp-gateway/.worktrees/email-stage4/frontend && npm run type-check && npm run lint 2>&1 | tail -3
```

Expected: clean.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/api/mail.ts
git commit -m "feat(frontend): typed API client for /api/mail/* endpoints"
```

---

## Task 3: Provider preset table (used by wizard)

**Files:**
- Create: `frontend/src/data/mailProviders.ts`

This is a small lookup that maps provider tile selections to default IMAP host/port and the app-password documentation URL. Used by the wizard's step 2 to prefill the form.

- [ ] **Step 1: Create the providers data**

```typescript
// frontend/src/data/mailProviders.ts
/**
 * Provider presets shown in the AddEmailSourceWizard's first step.
 *
 * Outlook / Microsoft 365 deliberately omitted — Microsoft requires
 * XOAUTH2; app passwords don't work there. Adding Outlook is part of
 * the OAuth follow-up after this stage ships.
 */

import type { ProviderName } from '../types/mail'

export interface ProviderPreset {
  id: ProviderName
  name: string
  imap_host: string
  imap_port: number
  app_password_help_url: string
  app_password_help_label: string
  /** Short blurb shown under the tile in the picker. */
  description: string
}

export const PROVIDER_PRESETS: ProviderPreset[] = [
  {
    id: 'gmail',
    name: 'Gmail',
    imap_host: 'imap.gmail.com',
    imap_port: 993,
    app_password_help_url: 'https://myaccount.google.com/apppasswords',
    app_password_help_label: 'Generate an app password',
    description: 'Google Workspace and consumer Gmail with 2FA enabled.',
  },
  {
    id: 'icloud',
    name: 'iCloud Mail',
    imap_host: 'imap.mail.me.com',
    imap_port: 993,
    app_password_help_url: 'https://support.apple.com/en-us/HT204397',
    app_password_help_label: 'How to make an app-specific password',
    description: 'Apple iCloud Mail with two-factor authentication.',
  },
  {
    id: 'fastmail',
    name: 'Fastmail',
    imap_host: 'imap.fastmail.com',
    imap_port: 993,
    app_password_help_url: 'https://www.fastmail.help/hc/en-us/articles/360058752394',
    app_password_help_label: 'Create an app password',
    description: 'Fastmail with the IMAP/SMTP app password set.',
  },
  {
    id: 'yahoo',
    name: 'Yahoo Mail',
    imap_host: 'imap.mail.yahoo.com',
    imap_port: 993,
    app_password_help_url: 'https://help.yahoo.com/kb/SLN15241.html',
    app_password_help_label: 'Manage app passwords',
    description: 'Yahoo Mail with an app password.',
  },
  {
    id: 'generic',
    name: 'Other IMAP',
    imap_host: '',
    imap_port: 993,
    app_password_help_url: '',
    app_password_help_label: '',
    description: 'Any IMAP server. Enter host and port manually.',
  },
]

export function getProviderPreset(id: ProviderName): ProviderPreset {
  const preset = PROVIDER_PRESETS.find((p) => p.id === id)
  if (preset === undefined) {
    throw new Error(`unknown provider: ${id}`)
  }
  return preset
}
```

- [ ] **Step 2: Verify**

```bash
cd /Users/alex/mcp-gateway/.worktrees/email-stage4/frontend && npm run type-check && npm run lint 2>&1 | tail -3
```

Expected: clean.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/data/mailProviders.ts
git commit -m "feat(frontend): provider presets for AddEmailSourceWizard"
```

---

## Task 4: AddEmailSourceWizard — provider picker (step 1 of 3)

**Files:**
- Create: `frontend/src/components/AddEmailSourceWizard.tsx`

This task creates the wizard scaffold and step 1 (provider tile picker). Steps 2 and 3 come in Tasks 5-6.

- [ ] **Step 1: Create the wizard with step 1 only**

```typescript
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

import { PROVIDER_PRESETS, type ProviderPreset } from '../data/mailProviders'

type Step = 'provider' | 'credentials' | 'labels'

export interface AddEmailSourceWizardProps {
  onCancel: () => void
  onCompleted: (accountId: string) => void
}

export default function AddEmailSourceWizard({ onCancel, onCompleted }: AddEmailSourceWizardProps) {
  const [step, setStep] = useState<Step>('provider')
  const [selectedProvider, setSelectedProvider] = useState<ProviderPreset | null>(null)

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
            onConnected={() => {
              // Will hand off to label picker in Task 6
              setStep('labels')
            }}
          />
        )}
        {step === 'labels' && (
          <div className="px-6 py-4 text-sm text-(--color-text-secondary)">
            Label picker coming in Task 6. <button onClick={() => onCompleted('placeholder')}>Done</button>
          </div>
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
  onConnected: (accountId: string) => void
}) {
  return (
    <div className="px-6 py-5">
      <p className="text-sm text-(--color-text-secondary) mb-4">
        Credentials step for <strong>{preset.name}</strong> — implementation lands in Task 5.
      </p>
      <div className="flex justify-between">
        <button
          type="button"
          onClick={onBack}
          className="rounded-lg border border-(--color-border) px-3 py-1.5 text-sm"
        >
          Back
        </button>
        <button
          type="button"
          onClick={() => onConnected('placeholder-account-id')}
          className="rounded-lg bg-(--color-accent) px-3 py-1.5 text-sm font-medium text-white"
        >
          Continue (stub)
        </button>
      </div>
    </div>
  )
}
```

- [ ] **Step 2: Verify**

```bash
cd /Users/alex/mcp-gateway/.worktrees/email-stage4/frontend && npm run type-check && npm run lint && npm run build 2>&1 | tail -5
```

Expected: clean. Build succeeds.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/AddEmailSourceWizard.tsx
git commit -m "feat(frontend): AddEmailSourceWizard scaffold + provider picker (step 1)"
```

---

## Task 5: AddEmailSourceWizard — credentials + test connection (step 2)

**Files:**
- Modify: `frontend/src/components/AddEmailSourceWizard.tsx`

Replace the stub `CredentialsStep` from Task 4 with a real form. On Test Connection success, advance to label picker carrying the discovered folder list.

- [ ] **Step 1: Implement CredentialsStep**

In `frontend/src/components/AddEmailSourceWizard.tsx`, replace the `CredentialsStep` function with:

```typescript
import { createMailAccount, deleteMailAccount, testMailAccount } from '../api/mail'
import type { FolderInfo } from '../types/mail'

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
  const canSubmit = displayName && imapHost && imapPort && imapUsername && appPassword && !testing

  async function handleTest() {
    setTesting(true)
    setError(null)
    let createdAccountId: string | null = null
    try {
      // Create the account first (encrypts password server-side via the cipher),
      // then probe the connection. The probe writes back account.status based
      // on success/failure, so a failed test still leaves a row visible in the
      // accounts list with status='auth_error' for the operator to fix.
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
        // Leave the account row for the operator to retry — DON'T delete on test failure
        return
      }
      onConnected(account.account_id, result.folders)
    } catch (e) {
      // Network/server error before account was created OR during test —
      // if we did create an account, roll it back so the wizard restart is clean.
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
```

Also: define `input-base` class for shared input styling. Add to `frontend/src/index.css` near the existing `@theme` block:

```css
.input-base {
  @apply w-full rounded-md border border-(--color-border) bg-(--color-bg-secondary) px-2.5 py-1.5 text-sm text-(--color-text-primary) focus:outline-none focus:ring-2 focus:ring-(--color-accent)/30;
}
```

(If a similar helper already exists in `index.css`, use the existing name instead.)

Update the parent `AddEmailSourceWizard` to pass folders to step 3:

```typescript
const [discoveredFolders, setDiscoveredFolders] = useState<FolderInfo[]>([])
const [createdAccountId, setCreatedAccountId] = useState<string | null>(null)

// ...
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
```

(`LabelsStep` is implemented in Task 6 — for now stub it inside the same file:)

```typescript
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
      <p className="text-sm">Discovered {folders.length} folders. Picker comes in Task 6.</p>
      <div className="mt-4 flex justify-between">
        <button onClick={onBack} className="rounded-lg border border-(--color-border) px-3 py-1.5 text-sm">
          Back
        </button>
        <button onClick={onCompleted} className="rounded-lg bg-(--color-accent) px-3 py-1.5 text-sm text-white">
          Done (stub)
        </button>
      </div>
    </div>
  )
}
```

- [ ] **Step 2: Verify**

```bash
cd /Users/alex/mcp-gateway/.worktrees/email-stage4/frontend && npm run type-check && npm run lint && npm run build 2>&1 | tail -5
```

Expected: clean.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/AddEmailSourceWizard.tsx frontend/src/index.css
git commit -m "feat(frontend): AddEmailSourceWizard step 2 — credentials form + test connection"
```

---

## Task 6: AddEmailSourceWizard — label multi-select (step 3)

**Files:**
- Modify: `frontend/src/components/AddEmailSourceWizard.tsx`

Replace the stub `LabelsStep` with a real multi-select that POSTs each selected label as a `WatchedLabel`. System folders (INBOX, [Gmail]/All Mail, etc.) get a warning indicator.

- [ ] **Step 1: Implement LabelsStep**

In `frontend/src/components/AddEmailSourceWizard.tsx`, replace the stub with:

```typescript
import { createWatchedLabel } from '../api/mail'

function LabelsStep({
  accountId,
  folders,
  onBack,
  onCompleted,
}: {
  accountId: string
  folders: FolderInfo[]
  onBack: () => void
  onCompleted: () => void
}) {
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Sort: user labels first, then system folders, both alphabetical.
  // Operators are most likely to want a user label like "Clerk".
  const sortedFolders = [...folders].sort((a, b) => {
    if (a.is_system !== b.is_system) {
      return a.is_system ? 1 : -1
    }
    return a.path.localeCompare(b.path)
  })

  function toggle(path: string) {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(path)) {
        next.delete(path)
      } else {
        next.add(path)
      }
      return next
    })
  }

  async function handleSubmit() {
    if (selected.size === 0) {
      setError('Pick at least one folder to watch.')
      return
    }
    setSubmitting(true)
    setError(null)
    const errors: string[] = []
    for (const path of selected) {
      try {
        await createWatchedLabel({
          account_id: accountId,
          label_path: path,
          display_name: path.split('/').pop() || path,
        })
      } catch (e) {
        errors.push(`${path}: ${e instanceof Error ? e.message : String(e)}`)
      }
    }
    setSubmitting(false)
    if (errors.length > 0) {
      setError(errors.join('; '))
      return
    }
    onCompleted()
  }

  return (
    <div className="px-6 py-5">
      <p className="text-sm text-(--color-text-secondary) mb-3">
        Pick which folders to watch. Each folder you tick will be synced into Documents as new mail arrives.
      </p>

      <div className="max-h-80 overflow-y-auto rounded-md border border-(--color-border)">
        {sortedFolders.map((folder) => {
          const isChecked = selected.has(folder.path)
          return (
            <label
              key={folder.path}
              className="flex items-center gap-2 px-3 py-2 hover:bg-(--color-bg-secondary) cursor-pointer border-b border-(--color-border) last:border-b-0"
            >
              <input
                type="checkbox"
                checked={isChecked}
                onChange={() => toggle(folder.path)}
                className="h-3.5 w-3.5 rounded-sm border-gray-300 text-(--color-accent) focus:ring-(--color-accent)/30"
              />
              <span className="flex-1 text-sm">{folder.path}</span>
              {folder.is_system && (
                <span
                  className="text-xs text-amber-600 dark:text-amber-400"
                  title="System folder. Picking this will sync the entire account; usually you want a user label like 'Clerk' instead."
                >
                  System
                </span>
              )}
            </label>
          )
        })}
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
          disabled={submitting}
          className="rounded-lg border border-(--color-border) px-3 py-1.5 text-sm disabled:opacity-50"
        >
          Back
        </button>
        <button
          type="button"
          onClick={handleSubmit}
          disabled={submitting || selected.size === 0}
          className="rounded-lg bg-(--color-accent) px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
        >
          {submitting ? 'Adding…' : `Watch ${selected.size} folder${selected.size === 1 ? '' : 's'}`}
        </button>
      </div>
    </div>
  )
}
```

- [ ] **Step 2: Verify**

```bash
cd /Users/alex/mcp-gateway/.worktrees/email-stage4/frontend && npm run type-check && npm run lint && npm run build 2>&1 | tail -5
```

Expected: clean.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/AddEmailSourceWizard.tsx
git commit -m "feat(frontend): AddEmailSourceWizard step 3 — label multi-select with system-folder warning"
```

---

## Task 7: EmailSection on /folders page

**Files:**
- Create: `frontend/src/components/EmailSection.tsx`
- Modify: `frontend/src/pages/FoldersPage.tsx`

Adds an Email section listing all mail accounts with their watched labels nested underneath. Each row has an Action menu (test connection / rescan / remove). The "Add email source" button opens the wizard.

- [ ] **Step 1: Create the EmailSection component**

```typescript
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
    void reload()
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
    if (!window.confirm('Remove this email source? Existing Documents stay in the corpus; only future syncs stop.')) {
      return
    }
    setBusy(accountId)
    try {
      await deleteMailAccount(accountId)
    } finally {
      setBusy(null)
      await reload()
    }
  }

  async function handleRemoveLabel(labelId: string) {
    if (!window.confirm('Stop watching this label? Existing Documents stay in the corpus.')) {
      return
    }
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
            <div key={account.account_id} className="rounded-[10px] border border-(--color-border) bg-(--color-bg-primary)">
              <div className="px-4 py-3 flex items-center justify-between">
                <div>
                  <div className="font-medium text-sm">{account.display_name}</div>
                  <div className="text-xs text-(--color-text-secondary)">{account.imap_username}</div>
                  {account.last_error && (
                    <div className="text-xs text-red-600 dark:text-red-400 mt-1">{account.last_error}</div>
                  )}
                </div>
                <div className="flex items-center gap-2">
                  <StatusPill state={mapAccountStatus(account.status)}>{labelForAccountStatus(account.status)}</StatusPill>
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
                  >
                    Remove
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
                        <StatusPill state={mapLabelStatus(label.status)}>{label.status}</StatusPill>
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
                        >
                          Remove
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
      return 'success'
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
      return 'success'
    case 'paused':
      return 'pending'
    case 'error':
      return 'error'
  }
}
```

NOTE: `StatusPill` and `PillState` are imported from `./StatusPill`. Check the exact path and exports — they may be at `frontend/src/components/StatusPill.tsx` (used by FoldersPage already). If `PillState` isn't exported with the names "success", "pending", "error", adapt `mapAccountStatus` / `mapLabelStatus` to whatever the existing `PillState` enum values are.

- [ ] **Step 2: Wire EmailSection into FoldersPage**

In `frontend/src/pages/FoldersPage.tsx`, find the bottom of the page's JSX (after the existing folder list) and add:

```typescript
import EmailSection from '../components/EmailSection'

// ... inside the main JSX, after the existing folders section:
<EmailSection />
```

- [ ] **Step 3: Verify**

```bash
cd /Users/alex/mcp-gateway/.worktrees/email-stage4/frontend && npm run type-check && npm run lint && npm run build 2>&1 | tail -5
```

Expected: clean.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/EmailSection.tsx frontend/src/pages/FoldersPage.tsx
git commit -m "feat(frontend): /folders Email section — accounts, labels, actions"
```

---

## Task 8: EmailMetadataSection for DocumentDetailPage

**Files:**
- Create: `frontend/src/components/EmailMetadataSection.tsx`
- Modify: `frontend/src/pages/DocumentDetailPage.tsx`

When a Document has `email_message_id` set, render a metadata block above the content with From / To / Cc / Date / Subject. For Documents whose source account is Gmail and where Message-ID is present, render a "View in Gmail" deep link.

- [ ] **Step 1: Add the email metadata fields to the DocumentDetail type**

In whatever type file declares the existing DocumentDetail shape (likely in `frontend/src/pages/DocumentDetailPage.tsx` itself or a sibling types file), add the email fields. They're all nullable. Search for the existing `DocumentDetail` interface:

```bash
grep -rn "interface DocumentDetail\|type DocumentDetail" frontend/src/
```

Then extend it with:

```typescript
  email_message_id?: string | null
  email_thread_id?: string | null
  email_parent_doc_id?: string | null
  email_from_address?: string | null
  email_from_name?: string | null
  email_to_addresses?: string[] | null
  email_cc_addresses?: string[] | null
  email_date_sent?: string | null
  email_label_path?: string | null
```

- [ ] **Step 2: Create the EmailMetadataSection component**

```typescript
// frontend/src/components/EmailMetadataSection.tsx
/**
 * Email metadata block — From / To / Cc / Date / Subject — with optional
 * "View in Gmail" deep link for Documents sourced from a Gmail account.
 *
 * Renders nothing (returns null) when the Document is not an email/attachment.
 */

import { useEffect, useState } from 'react'

import { listMailAccounts } from '../api/mail'
import type { MailAccountResponse } from '../types/mail'

export interface EmailMetadataSectionProps {
  emailMessageId: string | null | undefined
  emailFromAddress: string | null | undefined
  emailFromName: string | null | undefined
  emailToAddresses: string[] | null | undefined
  emailCcAddresses: string[] | null | undefined
  emailDateSent: string | null | undefined
  emailLabelPath: string | null | undefined
  emailParentDocId: string | null | undefined
}

export default function EmailMetadataSection(props: EmailMetadataSectionProps) {
  const {
    emailMessageId,
    emailFromAddress,
    emailFromName,
    emailToAddresses,
    emailCcAddresses,
    emailDateSent,
    emailLabelPath,
    emailParentDocId,
  } = props

  const [accounts, setAccounts] = useState<MailAccountResponse[]>([])

  // Fetch accounts to find which provider this email came from. We rely on
  // email_label_path matching the watched label, then jump to the account
  // via the watched-labels list — but that's an extra API call we'd rather
  // avoid. Simpler: assume any account whose watched labels include
  // email_label_path is the source. Since most operators watch one Gmail
  // account, this is usually unambiguous.
  useEffect(() => {
    if (!emailMessageId) return
    listMailAccounts().then(setAccounts).catch(() => setAccounts([]))
  }, [emailMessageId])

  // Render nothing for non-email Documents.
  if (!emailMessageId) {
    return null
  }

  const senderDisplay = emailFromName ? `${emailFromName} <${emailFromAddress}>` : emailFromAddress

  // View-in-Gmail link: only if exactly one Gmail account exists. With more
  // than one account, we'd need the watched-labels join to disambiguate;
  // skip the link until we have that.
  const gmailAccount = accounts.find((a) => a.provider === 'gmail')
  const viewInGmailUrl =
    gmailAccount && accounts.filter((a) => a.provider === 'gmail').length === 1
      ? buildViewInGmailUrl(gmailAccount.imap_username, emailMessageId)
      : null

  return (
    <div className="mb-4 rounded-md border border-(--color-border) bg-(--color-bg-secondary) p-3 text-sm">
      <dl className="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1">
        {emailFromAddress && (
          <>
            <dt className="text-(--color-text-secondary)">From:</dt>
            <dd className="text-(--color-text-primary)">{senderDisplay}</dd>
          </>
        )}
        {emailToAddresses && emailToAddresses.length > 0 && (
          <>
            <dt className="text-(--color-text-secondary)">To:</dt>
            <dd className="text-(--color-text-primary) break-all">{emailToAddresses.join(', ')}</dd>
          </>
        )}
        {emailCcAddresses && emailCcAddresses.length > 0 && (
          <>
            <dt className="text-(--color-text-secondary)">Cc:</dt>
            <dd className="text-(--color-text-primary) break-all">{emailCcAddresses.join(', ')}</dd>
          </>
        )}
        {emailDateSent && (
          <>
            <dt className="text-(--color-text-secondary)">Date:</dt>
            <dd className="text-(--color-text-primary)">{new Date(emailDateSent).toLocaleString()}</dd>
          </>
        )}
        {emailLabelPath && (
          <>
            <dt className="text-(--color-text-secondary)">Label:</dt>
            <dd className="text-(--color-text-primary)">{emailLabelPath}</dd>
          </>
        )}
      </dl>

      <div className="mt-2 flex items-center gap-3 text-xs">
        {viewInGmailUrl && (
          <a
            href={viewInGmailUrl}
            target="_blank"
            rel="noreferrer"
            className="text-(--color-accent) hover:underline"
          >
            View in Gmail ↗
          </a>
        )}
        {emailParentDocId && (
          <a href={`/docs/${emailParentDocId}`} className="text-(--color-accent) hover:underline">
            ← Part of this email
          </a>
        )}
      </div>
    </div>
  )
}

function buildViewInGmailUrl(authuserAddress: string, messageId: string): string {
  // mail.google.com/mail/u/?authuser=<our address>#search/rfc822msgid:<message-id>
  // authuser= picks the right logged-in Gmail account in the user's browser.
  // The Message-ID needs URL encoding; we use encodeURIComponent and strip
  // the surrounding angle brackets per Gmail's search-operator docs.
  const id = messageId.replace(/^<|>$/g, '')
  return (
    `https://mail.google.com/mail/u/?authuser=${encodeURIComponent(authuserAddress)}` +
    `#search/rfc822msgid:${encodeURIComponent(id)}`
  )
}
```

- [ ] **Step 3: Render the section in DocumentDetailPage**

Find the JSX in `DocumentDetailPage.tsx` where the document title and metadata are rendered, and add the EmailMetadataSection ABOVE the document content area:

```typescript
import EmailMetadataSection from '../components/EmailMetadataSection'

// ... inside the JSX, near the top of the document detail layout:
<EmailMetadataSection
  emailMessageId={doc.email_message_id}
  emailFromAddress={doc.email_from_address}
  emailFromName={doc.email_from_name}
  emailToAddresses={doc.email_to_addresses}
  emailCcAddresses={doc.email_cc_addresses}
  emailDateSent={doc.email_date_sent}
  emailLabelPath={doc.email_label_path}
  emailParentDocId={doc.email_parent_doc_id}
/>
```

The component returns null for non-email Documents, so no conditional check is needed at the call site.

- [ ] **Step 4: Verify**

```bash
cd /Users/alex/mcp-gateway/.worktrees/email-stage4/frontend && npm run type-check && npm run lint && npm run build 2>&1 | tail -5
```

Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/components/EmailMetadataSection.tsx frontend/src/pages/DocumentDetailPage.tsx
git commit -m "feat(frontend): EmailMetadataSection on document detail page + View-in-Gmail link"
```

---

## Task 9: Mail accounts subsection on System Status page

**Files:**
- Modify: `frontend/src/pages/SystemStatusPage.tsx`

Add a Mail Accounts subsection that lists every account with its connection state (status pill + last_connected_at + last_error). Read-only — for management actions, the operator goes to /folders.

- [ ] **Step 1: Find the SystemStatusPage layout**

```bash
grep -n "function SystemStatusPage\|export default" frontend/src/pages/SystemStatusPage.tsx
```

Locate where existing subsections (Postgres, Tika, Embedder, etc.) live.

- [ ] **Step 2: Add the Mail Accounts subsection**

Add a new subsection after the existing service-status sections. The exact placement depends on the page's existing structure — emulate what the other sections look like.

Add the import:

```typescript
import { useEffect, useState } from 'react'
import { listMailAccounts } from '../api/mail'
import type { MailAccountResponse } from '../types/mail'
```

Add a hook + component at the bottom of the page or as a sibling section:

```typescript
function MailAccountsStatus() {
  const [accounts, setAccounts] = useState<MailAccountResponse[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    listMailAccounts()
      .then(setAccounts)
      .catch(() => setAccounts([]))
      .finally(() => setLoading(false))
  }, [])

  if (loading) {
    return (
      <section className="mb-6">
        <h2 className="text-base font-medium mb-2">Mail Accounts</h2>
        <p className="text-sm text-(--color-text-secondary)">Loading…</p>
      </section>
    )
  }

  if (accounts.length === 0) {
    return (
      <section className="mb-6">
        <h2 className="text-base font-medium mb-2">Mail Accounts</h2>
        <p className="text-sm text-(--color-text-secondary)">
          No mail accounts connected. Add one from <a href="/folders" className="text-(--color-accent) hover:underline">Folders</a>.
        </p>
      </section>
    )
  }

  return (
    <section className="mb-6">
      <h2 className="text-base font-medium mb-2">Mail Accounts</h2>
      <ul className="rounded-md border border-(--color-border) divide-y divide-(--color-border)">
        {accounts.map((a) => (
          <li key={a.account_id} className="px-3 py-2 flex items-center justify-between text-sm">
            <div>
              <div className="font-medium">{a.display_name}</div>
              <div className="text-xs text-(--color-text-secondary)">{a.imap_username}</div>
              {a.last_error && (
                <div className="text-xs text-red-600 dark:text-red-400 mt-0.5">{a.last_error}</div>
              )}
            </div>
            <div className="flex items-center gap-3">
              {a.last_connected_at && (
                <span className="text-xs text-(--color-text-secondary)">
                  last connected {new Date(a.last_connected_at).toLocaleString()}
                </span>
              )}
              <span
                className={`text-xs px-2 py-0.5 rounded-full ${statusBadgeClass(a.status)}`}
              >
                {a.status}
              </span>
            </div>
          </li>
        ))}
      </ul>
    </section>
  )
}

function statusBadgeClass(status: MailAccountResponse['status']): string {
  switch (status) {
    case 'active':
      return 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300'
    case 'paused':
      return 'bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-300'
    case 'auth_error':
    case 'key_mismatch':
      return 'bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300'
  }
}
```

Then render `<MailAccountsStatus />` inside the page's JSX next to the other status sections.

- [ ] **Step 3: Verify**

```bash
cd /Users/alex/mcp-gateway/.worktrees/email-stage4/frontend && npm run type-check && npm run lint && npm run build 2>&1 | tail -5
```

Expected: clean.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/pages/SystemStatusPage.tsx
git commit -m "feat(frontend): SystemStatusPage — Mail Accounts subsection"
```

---

## Task 10: Security settings page — encryption status

**Files:**
- Create: `frontend/src/pages/SecuritySettingsPage.tsx`
- Modify: `frontend/src/App.tsx` — register the route
- Modify: `frontend/src/pages/SystemSettingsPage.tsx` — add a Security tile

This page surfaces encryption status: are any mail accounts in `key_mismatch` state? It's a read-only health view for now — the actual "Import master key" affordance is a follow-up because it requires a backend endpoint that doesn't exist yet (Task 11 is just the UI shell pointing at the docs).

- [ ] **Step 1: Create the SecuritySettingsPage**

```typescript
// frontend/src/pages/SecuritySettingsPage.tsx
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
import { BackButton } from '../components/BackButton'
import { PageHeader } from '../components/PageHeader'

export default function SecuritySettingsPage() {
  const [accounts, setAccounts] = useState<MailAccountResponse[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    listMailAccounts()
      .then(setAccounts)
      .catch(() => setAccounts([]))
      .finally(() => setLoading(false))
  }, [])

  const keyMismatchAccounts = accounts.filter((a) => a.status === 'key_mismatch')

  return (
    <div className="max-w-3xl mx-auto px-6 py-6">
      <BackButton to="/admin/system" />
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
              Go to <a href="/folders" className="text-(--color-accent) hover:underline">Folders</a> and click each
              affected account to re-enter its app password.
            </p>
          </div>
        )}
      </section>

      <section className="mb-8">
        <h2 className="text-base font-medium mb-2">Master key</h2>
        <p className="text-sm text-(--color-text-primary) mb-2">
          Sensitive values (mail-account app passwords, future OAuth tokens) are encrypted at rest in PostgreSQL with
          a 32-byte master key.
        </p>
        <p className="text-sm text-(--color-text-secondary)">
          On macOS the key is generated on first launch and stored in your login Keychain. On Docker the operator sets
          it via the <code className="rounded bg-(--color-bg-secondary) px-1">HARBOR_CLERK_MASTER_KEY</code> environment
          variable. See the operator guide for details.
        </p>
        <p className="mt-3 text-xs text-(--color-text-secondary)">
          A future release will add an "Import master key" form here for migrating credentials between deployments
          without re-entering app passwords. For now, re-entry is the only restoration path.
        </p>
      </section>
    </div>
  )
}
```

NOTE: `BackButton` import path may differ in this codebase — check what `frontend/src/components/BackButton.tsx` exports (named or default).

- [ ] **Step 2: Register the route**

In `frontend/src/App.tsx`, add the import and route:

```typescript
import SecuritySettingsPage from './pages/SecuritySettingsPage'

// ... in the JSX, after the existing /admin/system/* routes:
<Route path="/admin/system/security" element={<SecuritySettingsPage />} />
```

- [ ] **Step 3: Add a Security tile to SystemSettingsPage**

Find the existing tile/card grid in `frontend/src/pages/SystemSettingsPage.tsx` and add a Security entry alongside the existing tiles (Users, API Keys, System Status, etc.). Match the existing tile pattern; the link target is `/admin/system/security`.

- [ ] **Step 4: Verify**

```bash
cd /Users/alex/mcp-gateway/.worktrees/email-stage4/frontend && npm run type-check && npm run lint && npm run build 2>&1 | tail -5
```

Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/SecuritySettingsPage.tsx frontend/src/App.tsx frontend/src/pages/SystemSettingsPage.tsx
git commit -m "feat(frontend): SecuritySettingsPage — encryption status + Security tile"
```

---

## Task 11: Polish — manual end-to-end verification

This is a manual task. After all the code lands, exercise the full flow against a real Postgres + a real Gmail account (with an app password):

- [ ] **Step 1: Start the dev stack**

```bash
cd /Users/alex/mcp-gateway/.worktrees/email-stage4
export HARBOR_CLERK_MASTER_KEY=$(python3 -c 'import base64, os; print(base64.b64encode(os.urandom(32)).decode())')
# Spin up Postgres, run migrations, start API + watcher + frontend dev server
# (commands depend on the local setup; docker compose up -d postgres minio + the macOS launcher works)
```

- [ ] **Step 2: Walk through the wizard**

1. Open the SPA, log in as admin
2. Navigate to /folders
3. Click "Add email source"
4. Pick Gmail, enter address + app password, click Test connection
5. Verify the folder list appears
6. Tick a label (e.g. "Clerk")
7. Click "Watch 1 folder"
8. Wizard closes; the new account appears in the Email section

- [ ] **Step 3: Watch labels populate**

1. In Gmail, label a few emails with the watched label
2. Wait ~30 seconds (mail observer's poll cycle, or near-instant via IDLE)
3. Refresh /docs — the labeled emails should appear as Documents
4. Click an email Document → verify the From/To/Date metadata block renders, "View in Gmail" link points at the right Gmail search

- [ ] **Step 4: Verify lifecycle**

1. In Gmail, un-label one of the synced emails
2. Wait one poll cycle
3. Refresh /docs — that email's Document should be marked as deleted (or hidden depending on the Documents page filter)

- [ ] **Step 5: Verify error states**

1. Edit the mail account in the DB to have an `auth_error` status (or wait for a real auth error)
2. Confirm the EmailSection on /folders shows the auth-error badge
3. Confirm /admin/system/status shows the account as auth_error
4. Click Test connection → either reconnects (if creds are good) or stays in auth_error with the error message visible

- [ ] **Step 6: Mark task complete**

This task has no automated test gate — successful manual run is the signal. Commit any small fixes that came up during verification individually.

---

## Wrap-up

After Task 11 verification, Stage 4 is complete. Run the full frontend verification suite:

- [ ] **Run lint, type-check, build**

```bash
cd /Users/alex/mcp-gateway/.worktrees/email-stage4/frontend && \
  npm run lint && npm run type-check && npm run build 2>&1 | tail -5
```

Expected: clean.

- [ ] **Run the Python test suite to confirm no backend regressions from Stage 4 work**

```bash
cd /Users/alex/mcp-gateway/.worktrees/email-stage4 && uv run pytest -m "not integration" 2>&1 | tail -3
```

Expected: all green (Stage 4 doesn't touch Python; this is a sanity check).

- [ ] **Open Stage 4 PR**

```bash
git push -u origin spec/email-stage4
gh pr create --title "feat(email): stage 4 UI — Add email source wizard, /folders email section, document metadata, security page" \
    --body-file <(cat <<'EOF'
## Summary

Final stage of email ingestion: makes the entire feature accessible to non-technical office staff. After this lands, an admin walks through "Add email source" → enters Gmail credentials → picks a label → watches new mail flow into Documents in real time.

## What's in this PR

- TypeScript types + API client for /api/mail/* (Stages 1-3 backend surface)
- Three-step Add Email Source wizard (provider tile picker → credentials + test → label multi-select with system-folder warning)
- Email section on /folders listing accounts + watched labels with test/rescan/remove actions
- Email metadata block on document detail with View-in-Gmail deep link for Gmail-sourced emails
- Mail Accounts subsection on System Status page (read-only health view)
- New /admin/system/security page surfacing encryption status (key-mismatch detection)

## What is NOT in this PR

- "Import master key" admin form (planned follow-up — needs a new backend endpoint)
- Outlook/M365 support (separate XOAUTH2 follow-up after this stage)
- Frontend automated test suite for the new components — frontend project has no Vitest setup; verification is via lint + type-check + build + manual browser testing

## Stage roadmap (final)

| Stage | Status | What it adds |
|---|---|---|
| 1. Foundation | [#281](https://github.com/r0shi/harborclerk/pull/281) | Schema, secrets primitive, Keychain bootstrap |
| 2. IMAP sync engine | [#282](https://github.com/r0shi/harborclerk/pull/282) | aioimaplib client, REST API, sync state machine, IDLE+poll, MailObserver |
| 3. .eml → Document pipeline | [#283](https://github.com/r0shi/harborclerk/pull/283) | Parser, ingest orchestrator, Document lifecycle |
| 4. UI | **this PR** | Add email source wizard, /folders email section, doc metadata, security page |

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)
```

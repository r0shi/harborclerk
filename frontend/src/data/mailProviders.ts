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

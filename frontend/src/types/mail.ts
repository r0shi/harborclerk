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

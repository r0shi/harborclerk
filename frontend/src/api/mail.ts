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

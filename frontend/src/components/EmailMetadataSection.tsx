// frontend/src/components/EmailMetadataSection.tsx
/**
 * Email metadata block — From / To / Cc / Date / Subject — with optional
 * "View in Gmail" deep link for Documents sourced from a Gmail account.
 *
 * Renders nothing (returns null) when the Document is not an email/attachment.
 */

import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

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

  useEffect(() => {
    if (!emailMessageId) return
    let cancelled = false
    async function load() {
      try {
        const result = await listMailAccounts()
        if (!cancelled) {
          setAccounts(result)
        }
      } catch {
        if (!cancelled) {
          setAccounts([])
        }
      }
    }
    void load()
    return () => {
      cancelled = true
    }
  }, [emailMessageId])

  // Render nothing for non-email Documents.
  if (!emailMessageId) {
    return null
  }

  const senderDisplay = emailFromName ? `${emailFromName} <${emailFromAddress}>` : emailFromAddress

  // View-in-Gmail link: only if exactly one Gmail account exists. With more
  // than one account, we'd need the watched-labels join to disambiguate;
  // skip the link until we have that.
  const gmailAccounts = accounts.filter((a) => a.provider === 'gmail')
  const viewInGmailUrl =
    gmailAccounts.length === 1 ? buildViewInGmailUrl(gmailAccounts[0].imap_username, emailMessageId) : null

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
          <a href={viewInGmailUrl} target="_blank" rel="noreferrer" className="text-(--color-accent) hover:underline">
            View in Gmail ↗
          </a>
        )}
        {emailParentDocId && (
          <Link to={`/docs/${emailParentDocId}`} className="text-(--color-accent) hover:underline">
            ← Part of this email
          </Link>
        )}
      </div>
    </div>
  )
}

function buildViewInGmailUrl(authuserAddress: string, messageId: string): string {
  // mail.google.com/mail/u/<email>/#search/rfc822msgid:<message-id>
  // The /u/<value>/ path segment is what Gmail uses to pick which signed-in
  // identity to route to — `?authuser=` is an OAuth hint that mail.google.com
  // ignores for routing, and a numeric index would only be right by accident.
  // Putting the email address in the path lets Google match it to the correct
  // signed-in session.
  // The Message-ID needs URL encoding; we strip the surrounding angle brackets
  // per Gmail's search-operator docs.
  const id = messageId.replace(/^<|>$/g, '')
  return (
    `https://mail.google.com/mail/u/${encodeURIComponent(authuserAddress)}/` +
    `#search/rfc822msgid:${encodeURIComponent(id)}`
  )
}

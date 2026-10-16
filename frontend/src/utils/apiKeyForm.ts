// Parsing for the API key form's numeric fields. Each takes the input's raw string: the
// inputs are type="text" (inputMode="numeric"), because a type="number" input reports any
// text it cannot parse ("5e", "50-", or a typo such as "5OO" in Safari/WKWebView) as "",
// which would read as "no limit" and silently remove a cap.
//
// max_snippet_chars: null = no cap, N >= 0 = the first N characters of the passage and page
// text the key receives, and of summaries and metadata values (#831), none at 0 (#823). Rate limits: null = the system default, 0 =
// unlimited, N >= 1 requests. The server rejects anything outside 0..2^31-1, the range of
// its integer columns (src/harbor_clerk/api/schemas/api_keys.py).

export const INT32_MAX = 2 ** 31 - 1

export type NumberFieldParse = { ok: true; value: number | null } | { ok: false; error: string }

export const SNIPPET_CAP_ERROR = `Max snippet chars must be a whole number from 0 to ${INT32_MAX} (empty = no limit).`

function parseWholeNumber(raw: string, error: string): NumberFieldParse {
  const trimmed = raw.trim()
  if (trimmed === '') return { ok: true, value: null }
  if (!/^\d+$/.test(trimmed)) return { ok: false, error }
  const n = Number(trimmed)
  if (n > INT32_MAX) return { ok: false, error }
  return { ok: true, value: n }
}

/** '' is no cap (null), "0" is 0; a negative, a fraction, junk or a value past 2^31-1 is an error. */
export function parseSnippetCap(raw: string): NumberFieldParse {
  return parseWholeNumber(raw, SNIPPET_CAP_ERROR)
}

/** '' is the system default (null), "0" is unlimited; otherwise a whole number of requests. */
export function parseRateLimit(raw: string, label: string): NumberFieldParse {
  return parseWholeNumber(raw, `${label} must be a whole number from 0 to ${INT32_MAX} (empty = system default).`)
}

/** The form's initial string for a stored cap. A legacy negative is shown as 0, the cap the server applies. */
export function snippetCapInput(stored: number | null): string {
  return stored == null ? '' : String(Math.max(0, stored))
}

// Expiry (#833). One convention everywhere: the operator picks a calendar date, the key stops
// working at the end of that day in the browser's local time, and the server stores that
// instant in UTC. The date input and the table both read a stored expiry back in local time, so
// they show the same day whichever side of UTC the operator is on.

export type ExpiryParse = { ok: true; value: string | null } | { ok: false; error: string }

export const EXPIRY_REQUIRED_ERROR =
  'Choose a complete expiry date, or untick "Set expiry date" for a key that never expires.'
export const EXPIRY_INVALID_ERROR = 'Expiry date must be a valid date (yyyy-mm-dd).'

function pad2(n: number): string {
  return String(n).padStart(2, '0')
}

/** A Date's local calendar date as yyyy-mm-dd, the value format of an <input type="date">. */
function localDateValue(d: Date): string {
  return `${String(d.getFullYear()).padStart(4, '0')}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`
}

/** The date input's initial value for a stored expiry: its calendar date in local time. */
export function expiryDateInput(stored: string | null): string {
  if (!stored) return ''
  const d = new Date(stored)
  return Number.isNaN(d.getTime()) ? '' : localDateValue(d)
}

/**
 * Whether a stored expiry is the last second of a local day, the instant this form writes. Keys
 * saved by the old UI (`T23:59:59Z`) and times set through the API (`T10:30:00Z`) usually are not.
 */
export function isLocalEndOfDay(stored: string): boolean {
  const d = new Date(stored)
  return d.getHours() === 23 && d.getMinutes() === 59 && d.getSeconds() === 59
}

/** The local moment a stored expiry takes effect, e.g. "Jan 15, 2027, 3:59 PM". */
export function formatExpiryMoment(stored: string): string {
  return new Date(stored).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}

/**
 * The table's text for a stored expiry, in local time: the date alone when the key stops at the
 * end of that day (the date the input shows), otherwise the date and the time it actually stops.
 */
export function formatExpiry(stored: string | null): string {
  if (!stored) return 'Never'
  const d = new Date(stored)
  if (Number.isNaN(d.getTime())) return 'Never'
  return isLocalEndOfDay(stored) ? d.toLocaleDateString() : formatExpiryMoment(stored)
}

/** The date a freshly ticked "Set expiry date" starts at: 90 local days from now. */
export function defaultExpiryDate(now: Date = new Date()): string {
  return localDateValue(new Date(now.getFullYear(), now.getMonth(), now.getDate() + 90))
}

/** yyyy-mm-dd to the last second of that local day, as a UTC ISO string; null if not a real date. */
export function endOfLocalDayIso(date: string): string | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(date.trim())
  if (!m) return null
  const [year, month, day] = [Number(m[1]), Number(m[2]), Number(m[3])]
  const d = new Date(2000, 0, 1, 23, 59, 59, 0)
  d.setFullYear(year, month - 1, day) // not the constructor, which maps years 0-99 to 19xx
  if (d.getFullYear() !== year || d.getMonth() !== month - 1 || d.getDate() !== day) return null // e.g. 2027-02-30
  return d.toISOString()
}

/**
 * The expiry to send. Unticked is "never expires" (null). Ticked with an empty value is an error,
 * never null: a date input reports "" as soon as one segment of it is cleared, and reading that
 * as "no expiry" would silently turn an expiring key into one that never expires.
 */
export function parseExpiry(enabled: boolean, date: string): ExpiryParse {
  if (!enabled) return { ok: true, value: null }
  if (date.trim() === '') return { ok: false, error: EXPIRY_REQUIRED_ERROR }
  const iso = endOfLocalDayIso(date)
  return iso ? { ok: true, value: iso } : { ok: false, error: EXPIRY_INVALID_ERROR }
}

// Document scope (#833). null and [] both mean "no restriction" on the server
// (KeyScope.unrestricted), so the label is computed from the stored ids, unknown ones included,
// never from what the checkbox lists happen to show.

/** Ids in the stored scope that the current option list does not contain. */
export function unknownScopeIds<T>(stored: T[] | null, known: readonly T[]): T[] {
  if (!stored) return []
  const knownSet = new Set(known)
  return stored.filter((id) => !knownSet.has(id))
}

/** Whether the ids limit the key at all; false means the key reads the whole corpus. */
export function isScopeRestricted(topicIds: unknown[] | null, folderIds: unknown[] | null): boolean {
  return (topicIds?.length ?? 0) > 0 || (folderIds?.length ?? 0) > 0
}

/** The document-scope label, computed from the stored ids. */
export function documentScopeSummary(topicIds: unknown[] | null, folderIds: unknown[] | null): string {
  const t = topicIds?.length ?? 0
  const f = folderIds?.length ?? 0
  const parts = [
    ...(t > 0 ? [`${t} topic${t === 1 ? '' : 's'}`] : []),
    ...(f > 0 ? [`${f} folder${f === 1 ? '' : 's'}`] : []),
  ]
  return parts.length ? `limited to ${parts.join(' and ')}` : 'no restriction'
}

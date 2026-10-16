import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  EXPIRY_INVALID_ERROR,
  EXPIRY_REQUIRED_ERROR,
  INT32_MAX,
  defaultExpiryDate,
  documentScopeSummary,
  endOfLocalDayIso,
  expiryDateInput,
  formatExpiry,
  formatExpiryMoment,
  isLocalEndOfDay,
  isScopeRestricted,
  parseExpiry,
  parseRateLimit,
  parseSnippetCap,
  snippetCapInput,
  unknownScopeIds,
} from './apiKeyForm'

// Node applies a runtime change to the TZ variable, so each zone below really is the local zone.
const ZONES = ['Pacific/Auckland', 'Asia/Kolkata', 'UTC', 'America/Los_Angeles', 'Pacific/Honolulu']

describe.each(ZONES)('expiry in %s', (zone) => {
  beforeEach(() => {
    vi.stubEnv('TZ', zone)
  })
  afterEach(() => {
    vi.unstubAllEnvs()
  })

  it('confirms the zone took effect', () => {
    const offset = new Date(2027, 0, 15).getTimezoneOffset()
    expect(offset).toBe(
      { 'Pacific/Auckland': -780, 'Asia/Kolkata': -330, UTC: 0, 'America/Los_Angeles': 480, 'Pacific/Honolulu': 600 }[
        zone
      ],
    )
  })

  it('stores a chosen date as the last second of that local day, in UTC', () => {
    const iso = endOfLocalDayIso('2027-01-15')!
    expect(iso).toMatch(/Z$/)
    const d = new Date(iso)
    expect([d.getFullYear(), d.getMonth(), d.getDate(), d.getHours(), d.getMinutes(), d.getSeconds()]).toEqual([
      2027, 0, 15, 23, 59, 59,
    ])
  })

  it('reads a stored expiry back as the same date in the modal and in the table', () => {
    const iso = parseExpiry(true, '2027-01-15')
    expect(iso.ok).toBe(true)
    const stored = (iso as { value: string }).value
    expect(expiryDateInput(stored)).toBe('2027-01-15')
    expect(formatExpiry(stored)).toBe(new Date(2027, 0, 15).toLocaleDateString())
  })

  it('shows an API-set time as the local moment it stops, and its local date in the input', () => {
    const stored = '2026-12-01T10:30:00Z' // never 23:59:59 local in these zones
    const local = new Date(stored)
    expect(isLocalEndOfDay(stored)).toBe(false)
    expect(formatExpiry(stored)).toBe(local.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' }))
    expect(formatExpiry(stored)).toBe(formatExpiryMoment(stored))
    expect(expiryDateInput(stored)).toBe(
      `${local.getFullYear()}-${String(local.getMonth() + 1).padStart(2, '0')}-${String(local.getDate()).padStart(2, '0')}`,
    )
  })

  it('shows a legacy T23:59:59Z by its date only where that is the end of the local day', () => {
    const legacy = '2027-01-15T23:59:59Z'
    if (zone === 'UTC') {
      expect(isLocalEndOfDay(legacy)).toBe(true)
      expect(formatExpiry(legacy)).toBe(new Date(2027, 0, 15).toLocaleDateString())
    } else {
      expect(isLocalEndOfDay(legacy)).toBe(false)
      expect(formatExpiry(legacy)).toBe(formatExpiryMoment(legacy))
    }
  })

  it('defaults a freshly ticked expiry to 90 local days ahead', () => {
    expect(defaultExpiryDate(new Date(2027, 0, 15, 23, 30))).toBe('2027-04-15')
    expect(defaultExpiryDate(new Date(2027, 0, 15, 0, 30))).toBe('2027-04-15')
  })
})

describe('parseExpiry', () => {
  it('sends null only when the box is unticked', () => {
    expect(parseExpiry(false, '')).toEqual({ ok: true, value: null })
    expect(parseExpiry(false, '2027-01-15')).toEqual({ ok: true, value: null })
  })

  it('rejects an empty date while the box is ticked instead of reading it as "never expires"', () => {
    expect(parseExpiry(true, '')).toEqual({ ok: false, error: EXPIRY_REQUIRED_ERROR })
    expect(parseExpiry(true, '  ')).toEqual({ ok: false, error: EXPIRY_REQUIRED_ERROR })
  })

  it('rejects a date that is not a real calendar day', () => {
    for (const raw of ['2027-02-30', '2027-13-01', '27-01-15', '2027-1-5', 'soon', '202701-15']) {
      expect(parseExpiry(true, raw), raw).toEqual({ ok: false, error: EXPIRY_INVALID_ERROR })
    }
  })

  it('keeps a year below 100 as written, not as 19xx', () => {
    expect(new Date(endOfLocalDayIso('0099-06-01')!).getFullYear()).toBe(99)
  })
})

describe('document scope helpers', () => {
  it('lists stored ids the option list does not contain', () => {
    expect(unknownScopeIds([1, 7, 2], [1, 2])).toEqual([7])
    expect(unknownScopeIds(['f1', 'gone'], ['f1'])).toEqual(['gone'])
    expect(unknownScopeIds(null, [1])).toEqual([])
    // Given an empty list, every stored id is flagged. So the page must pass [] only for a list
    // that loaded empty: for one that failed to load it skips this helper (topicsFailed /
    // foldersFailed), or a failed fetch would offer every stored id for removal.
    expect(unknownScopeIds([3], [])).toEqual([3])
  })

  it('treats null and [] alike as unrestricted, as the server does', () => {
    expect(isScopeRestricted(null, null)).toBe(false)
    expect(isScopeRestricted([], [])).toBe(false)
    expect(isScopeRestricted([7], null)).toBe(true)
    expect(isScopeRestricted(null, ['f'])).toBe(true)
  })

  it('computes "no restriction" from the stored ids, unknown ones included', () => {
    expect(documentScopeSummary(null, null)).toBe('no restriction')
    expect(documentScopeSummary([], [])).toBe('no restriction')
    expect(documentScopeSummary([7], null)).toBe('limited to 1 topic')
    expect(documentScopeSummary([1, 2], ['f'])).toBe('limited to 2 topics and 1 folder')
  })
})

describe('parseSnippetCap', () => {
  it('sends 0 for "0": a cap of 0 is a cap, not "no limit"', () => {
    expect(parseSnippetCap('0')).toEqual({ ok: true, value: 0 })
  })

  it('sends null only for an empty field', () => {
    expect(parseSnippetCap('')).toEqual({ ok: true, value: null })
    expect(parseSnippetCap('   ')).toEqual({ ok: true, value: null })
  })

  it('accepts a whole number up to the 32-bit column limit', () => {
    expect(parseSnippetCap('200')).toEqual({ ok: true, value: 200 })
    expect(parseSnippetCap(String(INT32_MAX))).toEqual({ ok: true, value: INT32_MAX })
  })

  it('rejects a negative, a fraction, junk or a value past the column limit instead of dropping the cap', () => {
    for (const raw of ['-5', '2.5', 'abc', '5e', '50-', '1.2.3', '5OO', '1e3', String(INT32_MAX + 1)]) {
      expect(parseSnippetCap(raw).ok, raw).toBe(false)
    }
  })
})

describe('parseRateLimit', () => {
  it('reads "" as the system default, "0" as unlimited and rejects junk', () => {
    expect(parseRateLimit('', 'Requests/min')).toEqual({ ok: true, value: null })
    expect(parseRateLimit('0', 'Requests/min')).toEqual({ ok: true, value: 0 })
    expect(parseRateLimit('30', 'Requests/min')).toEqual({ ok: true, value: 30 })
    expect(parseRateLimit('3O', 'Requests/min').ok).toBe(false)
    expect(parseRateLimit('-1', 'Requests/min').ok).toBe(false)
  })
})

describe('snippetCapInput', () => {
  it('shows a stored cap, a legacy negative as 0, and no cap as empty', () => {
    expect(snippetCapInput(50)).toBe('50')
    expect(snippetCapInput(0)).toBe('0')
    expect(snippetCapInput(-5)).toBe('0')
    expect(snippetCapInput(null)).toBe('')
  })
})

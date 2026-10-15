import { describe, expect, it } from 'vitest'
import { INT32_MAX, parseRateLimit, parseSnippetCap, snippetCapInput } from './apiKeyForm'

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

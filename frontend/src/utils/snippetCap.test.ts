import { describe, expect, it } from 'vitest'
import { parseSnippetCap, snippetCapInput } from './snippetCap'

describe('parseSnippetCap', () => {
  it('sends 0 for "0": a cap of 0 is a cap, not "no limit"', () => {
    expect(parseSnippetCap('0')).toEqual({ ok: true, value: 0 })
  })

  it('sends null only for an empty field', () => {
    expect(parseSnippetCap('')).toEqual({ ok: true, value: null })
    expect(parseSnippetCap('   ')).toEqual({ ok: true, value: null })
  })

  it('accepts a positive whole number', () => {
    expect(parseSnippetCap('200')).toEqual({ ok: true, value: 200 })
  })

  it('rejects a negative or fractional value instead of dropping the cap', () => {
    expect(parseSnippetCap('-5').ok).toBe(false)
    expect(parseSnippetCap('2.5').ok).toBe(false)
    expect(parseSnippetCap('abc').ok).toBe(false)
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

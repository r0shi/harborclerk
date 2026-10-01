import { describe, expect, it } from 'vitest'
import { modelGuidance } from '../utils/modelGuidance'

describe('modelGuidance', () => {
  it('labels curated models by intended use', () => {
    expect(modelGuidance({ id: 'qwen36-35b-a3b', size_bytes: 22_000_000_000, supports_research: true }).label).toBe(
      'Best local research',
    )
    expect(modelGuidance({ id: 'gpt-oss-20b', size_bytes: 11_600_000_000, supports_research: true }).label).toBe(
      'Strong tables and comparisons',
    )
    expect(modelGuidance({ id: 'qwen35-4b', size_bytes: 2_740_937_888, supports_research: true }).label).toBe(
      'Pick on a 16 GB Mac',
    )
  })

  it('marks the Qwen3 tiers superseded and names the Qwen3.5 model to pick instead (#551)', () => {
    const q8 = modelGuidance({ id: 'qwen3-8b', size_bytes: 5_027_783_488, supports_research: true })
    expect(q8.label).toBe('Superseded')
    expect(q8.note).toContain('Qwen3.5 9B')
    expect(q8.warning).toBe('Superseded by Qwen3.5 9B: switch when convenient. Until then this model works as it did.')
    const q4 = modelGuidance({ id: 'qwen3-4b', size_bytes: 2_497_280_256, supports_research: true })
    expect(q4.label).toBe('Superseded')
    expect(q4.warning).toBe('Superseded by Qwen3.5 4B: switch when convenient. Until then this model works as it did.')
    // Every model the three benchmark runs measured carries its result, not a pending evaluation.
    for (const id of ['qwen35-9b', 'qwen35-4b', 'gemma4-12b']) {
      const g = modelGuidance({ id, size_bytes: 3_000_000_000, supports_research: true })
      expect(g.note).not.toContain('pending')
      expect(g.warning).toBeUndefined()
    }
  })

  it('warns for small or non-research fallback models', () => {
    const guidance = modelGuidance({ id: 'future-small', size_bytes: 3_000_000_000, supports_research: false })

    expect(guidance.label).toBe('Quick lookup')
    expect(guidance.warning).toBe('Not recommended for deep research.')
  })
})

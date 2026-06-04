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
    expect(modelGuidance({ id: 'qwen3-4b', size_bytes: 2_500_000_000, supports_research: true }).warning).toContain(
      'May miss details',
    )
  })

  it('warns for small or non-research fallback models', () => {
    const guidance = modelGuidance({ id: 'future-small', size_bytes: 3_000_000_000, supports_research: false })

    expect(guidance.label).toBe('Quick lookup')
    expect(guidance.warning).toBe('Not recommended for deep research.')
  })
})

import { describe, expect, it } from 'vitest'
import { groupModelsByRam, ramTierLabel } from './memoryTiers'

describe('groupModelsByRam', () => {
  it('groups by the Mac each model needs, smallest tier first, lightest model first within a tier', () => {
    const groups = groupModelsByRam([
      { id: 'heavy', min_ram_gb: 36, memory_bytes: 28e9 },
      { id: 'mid', min_ram_gb: 24, memory_bytes: 15e9 },
      { id: 'small', min_ram_gb: 16, memory_bytes: 8e9 },
      { id: 'smaller', min_ram_gb: 16, memory_bytes: 7e9 },
    ])
    expect(groups.map((g) => g.gb)).toEqual([16, 24, 36])
    expect(groups[0].items.map((m) => m.id)).toEqual(['smaller', 'small'])
    expect(groups[0].label).toBe('For Macs with 16 GB or more')
  })

  it('has no empty groups and copes with no models', () => {
    expect(groupModelsByRam([])).toEqual([])
    expect(groupModelsByRam([{ min_ram_gb: 32, memory_bytes: 1 }]).every((g) => g.items.length > 0)).toBe(true)
  })

  it('reads as a sentence for any size', () => {
    expect(ramTierLabel(18)).toBe('For Macs with 18 GB or more')
  })
})

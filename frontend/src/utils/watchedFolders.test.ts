import { describe, it, expect } from 'vitest'
import { folderDeleteProtected, folderIsUnavailable, unavailableReasonLabel } from './watchedFolders'

describe('folderIsUnavailable', () => {
  it('is true only for an unmounted Docker mount', () => {
    expect(folderIsUnavailable('unmounted')).toBe(true)
    expect(folderIsUnavailable(null)).toBe(false)
    expect(folderIsUnavailable(undefined)).toBe(false)
    expect(folderIsUnavailable('root_missing')).toBe(false)
    expect(folderIsUnavailable('root_unreadable')).toBe(false)
    expect(folderIsUnavailable('root_empty')).toBe(false)
  })
})

describe('folderDeleteProtected', () => {
  it('protects a live auto-discovered mount, whatever the watcher reports, until it is unmounted', () => {
    const mount = (unavailable_reason: string | null) => ({ auto_discovered: true, unavailable_reason })
    expect(folderDeleteProtected(mount(null))).toBe(true)
    expect(folderDeleteProtected(mount('root_missing'))).toBe(true)
    expect(folderDeleteProtected(mount('root_empty'))).toBe(true)
    expect(folderDeleteProtected(mount('unmounted'))).toBe(false)
    expect(folderDeleteProtected({ auto_discovered: false, unavailable_reason: null })).toBe(false)
  })
})

describe('unavailableReasonLabel', () => {
  it('labels each reason, and nothing when there is none', () => {
    expect(unavailableReasonLabel(null)).toBeNull()
    expect(unavailableReasonLabel('unmounted')).toBe('unmounted')
    expect(unavailableReasonLabel('root_missing')).toBe('folder missing')
    expect(unavailableReasonLabel('root_unreadable')).toBe('not readable')
    expect(unavailableReasonLabel('root_empty')).toBe('empty')
    expect(unavailableReasonLabel('something_new')).toBe('unavailable')
  })
})

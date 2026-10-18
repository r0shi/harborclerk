import { describe, it, expect } from 'vitest'
import {
  folderDeleteProtected,
  folderIsUnavailable,
  folderNotesSummary,
  unavailableReasonLabel,
} from './watchedFolders'

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

describe('folderNotesSummary', () => {
  it('reads the watcher notes as one line, and nothing when there are none', () => {
    expect(folderNotesSummary(undefined)).toBeNull()
    expect(folderNotesSummary({})).toBeNull()
    expect(folderNotesSummary({ root_missing: 0 })).toBeNull()
    expect(folderNotesSummary({ root_missing: 1 })).toBe('1 not found')
    expect(folderNotesSummary({ root_unreadable: 1, root_empty: 2, root_missing: 3 })).toBe(
      '2 empty · 3 not found · 1 not readable',
    )
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

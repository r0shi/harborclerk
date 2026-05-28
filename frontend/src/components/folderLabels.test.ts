import { describe, it, expect } from 'vitest'
import { disambiguateLabels, type FolderLike } from './folderLabels'

function f(folder_id: string, path: string, display_name: string | null = null): FolderLike {
  return { folder_id, path, display_name }
}

describe('disambiguateLabels', () => {
  it('uses display_name as-is when no collisions', () => {
    const folders = [f('1', '/work/contracts', 'Contracts'), f('2', '/work/legal', 'Legal')]
    const labels = disambiguateLabels(folders)
    expect(labels.get('1')).toBe('Contracts')
    expect(labels.get('2')).toBe('Legal')
  })

  it('walks up one path segment when two labels collide', () => {
    const folders = [f('1', '/work/cuad/ingest', 'ingest'), f('2', '/work/qwen3-20b/cuad/ingest', 'ingest')]
    const labels = disambiguateLabels(folders)
    expect(labels.get('1')).toBe('cuad/ingest')
    expect(labels.get('2')).toBe('qwen3-20b/cuad/ingest')
  })

  it('extends as many segments as needed for three+ folders sharing a basename', () => {
    const folders = [
      f('1', '/work/cuad/ingest', 'ingest'),
      f('2', '/work/qwen3-20b/cuad/ingest', 'ingest'),
      f('3', '/work/gpt-oss-20b/cuad/ingest', 'ingest'),
    ]
    const labels = disambiguateLabels(folders)
    expect(labels.get('1')).toBe('cuad/ingest')
    expect(labels.get('2')).toBe('qwen3-20b/cuad/ingest')
    expect(labels.get('3')).toBe('gpt-oss-20b/cuad/ingest')
  })

  it('mixes unique and colliding correctly', () => {
    const folders = [
      f('1', '/work/contracts', 'Contracts'),
      f('2', '/work/cuad/ingest', 'ingest'),
      f('3', '/work/qwen3-20b/cuad/ingest', 'ingest'),
    ]
    const labels = disambiguateLabels(folders)
    expect(labels.get('1')).toBe('Contracts')
    expect(labels.get('2')).toBe('cuad/ingest')
    expect(labels.get('3')).toBe('qwen3-20b/cuad/ingest')
  })

  it('falls back to path basename when display_name is null', () => {
    const folders = [f('1', '/work/contracts', null), f('2', '/work/legal', null)]
    const labels = disambiguateLabels(folders)
    expect(labels.get('1')).toBe('contracts')
    expect(labels.get('2')).toBe('legal')
  })

  it('falls back to UUID suffix when two folders have identical paths (degenerate)', () => {
    const folders = [
      f('aaaaaaaa-1111-2222-3333-444444444444', '/work/dup', 'dup'),
      f('bbbbbbbb-5555-6666-7777-888888888888', '/work/dup', 'dup'),
    ]
    const labels = disambiguateLabels(folders)
    const a = labels.get('aaaaaaaa-1111-2222-3333-444444444444')
    const b = labels.get('bbbbbbbb-5555-6666-7777-888888888888')
    expect(a).not.toBe(b)
    expect(a).toContain('dup')
    expect(b).toContain('dup')
  })
})

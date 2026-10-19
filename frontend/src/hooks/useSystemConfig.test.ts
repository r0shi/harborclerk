import { renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { repoDocUrl } from '../data/docsLinks'
import { useSystemConfig } from './useSystemConfig'

function stubHealth(body: Record<string, unknown>) {
  const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => body })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

describe('useSystemConfig', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('reads version from /api/system/health, and a release version links its tag', async () => {
    const fetchMock = stubHealth({ status: 'healthy', version: '0.9.3', allow_source_download: true })

    const { result } = renderHook(() => useSystemConfig())

    await waitFor(() => expect(result.current.loaded).toBe(true))
    expect(fetchMock).toHaveBeenCalledWith('/api/system/health')
    expect(result.current.version).toBe('0.9.3')
    expect(result.current.allowSourceDownload).toBe(true)
    expect(repoDocUrl('docs/watched-folders-docker.md', result.current.version)).toBe(
      'https://github.com/r0shi/harborclerk/blob/v0.9.3/docs/watched-folders-docker.md',
    )
  })

  it('passes a development version through, which links main', async () => {
    stubHealth({ status: 'healthy', version: '0.9.3+unstamped' })

    const { result } = renderHook(() => useSystemConfig())

    await waitFor(() => expect(result.current.loaded).toBe(true))
    expect(result.current.version).toBe('0.9.3+unstamped')
    expect(repoDocUrl('docs/x.md', result.current.version)).toBe(
      'https://github.com/r0shi/harborclerk/blob/main/docs/x.md',
    )
  })

  it('reports no version when health omits it, and null before it answers', async () => {
    stubHealth({ status: 'healthy' })

    const { result } = renderHook(() => useSystemConfig())
    expect(result.current.version).toBeNull()

    await waitFor(() => expect(result.current.loaded).toBe(true))
    expect(result.current.version).toBeNull()
  })
})

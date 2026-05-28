import React from 'react'
import { describe, it, expect, vi } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useWatchedFolders } from './useWatchedFolders'
import * as api from '../api'

describe('useWatchedFolders', () => {
  function wrapper({ children }: { children: React.ReactNode }) {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>
  }

  it('returns folders, filtering out unavailable ones', async () => {
    const mockGet = vi.spyOn(api, 'get').mockResolvedValue([
      { folder_id: 'a', display_name: 'A', path: '/a', unavailable_reason: null },
      { folder_id: 'b', display_name: 'B', path: '/b', unavailable_reason: 'unmounted' },
      { folder_id: 'c', display_name: 'C', path: '/c', unavailable_reason: null },
    ])

    const { result } = renderHook(() => useWatchedFolders(), { wrapper })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.folders.map((f) => f.folder_id)).toEqual(['a', 'c'])
    mockGet.mockRestore()
  })

  it('returns empty array when no folders', async () => {
    const mockGet = vi.spyOn(api, 'get').mockResolvedValue([])
    const { result } = renderHook(() => useWatchedFolders(), { wrapper })
    await waitFor(() => expect(result.current.isSuccess).toBe(true))
    expect(result.current.folders).toEqual([])
    mockGet.mockRestore()
  })
})

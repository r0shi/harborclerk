import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { post } from '../api'
import SearchPage from './SearchPage'

vi.mock('../api', () => ({
  post: vi.fn(),
}))

vi.mock('../hooks/useWatchedFolders', () => ({
  useWatchedFolders: () => ({
    folders: [],
    isLoading: false,
    isSuccess: true,
    isError: false,
    refetch: () => undefined,
  }),
}))

const postMock = vi.mocked(post)

function storageMock(): Storage {
  const store = new Map<string, string>()
  return {
    get length() {
      return store.size
    },
    clear: () => store.clear(),
    getItem: (key: string) => store.get(key) ?? null,
    key: (index: number) => Array.from(store.keys())[index] ?? null,
    removeItem: (key: string) => store.delete(key),
    setItem: (key: string, value: string) => store.set(key, value),
  }
}

function resetStorage() {
  Object.defineProperty(globalThis, 'localStorage', {
    value: storageMock(),
    configurable: true,
  })
  Object.defineProperty(globalThis, 'sessionStorage', {
    value: storageMock(),
    configurable: true,
  })
}

function renderSearchPage() {
  render(
    <MemoryRouter>
      <SearchPage />
    </MemoryRouter>,
  )
}

describe('SearchPage', () => {
  beforeEach(() => {
    resetStorage()
    postMock.mockReset()
  })

  it('uses the search endpoint by default', async () => {
    postMock.mockResolvedValueOnce({
      hits: [],
      total_candidates: 0,
      has_more: false,
      possible_conflict: false,
      conflict_sources: [],
    })

    renderSearchPage()

    const input = screen.getByPlaceholderText('Search documents...')
    fireEvent.change(input, { target: { value: 'arbitration' } })
    fireEvent.submit(input.closest('form') as HTMLFormElement)

    await waitFor(() => {
      expect(postMock).toHaveBeenCalledWith('/api/search', {
        query: 'arbitration',
        k: 25,
        offset: 0,
      })
    })
    expect(await screen.findByText('No results found.')).toBeInTheDocument()
  })

  it('uses the find-all endpoint and renders document results', async () => {
    postMock.mockResolvedValueOnce({
      results: [
        {
          doc_id: 'doc-1',
          doc_title: 'Vendor Contract',
          mime_type: 'application/pdf',
          language: 'en',
          score: 0.87,
          ingested_at: '2026-06-01T00:00:00Z',
          page_range: '2',
          top_chunk: {
            chunk_id: 'chunk-1',
            text: 'Force majeure clause excerpt.',
            page: 2,
            heading: 'Terms',
          },
          source: {
            doc_id: 'doc-1',
            doc_title: 'Vendor Contract',
            chunk_id: 'chunk-1',
            pages: '2',
            section: 'Terms',
            source_kind: 'document',
            source_label: 'Vendor Contract',
            folder_label: 'Contracts',
            relative_path: 'vendors/contract.pdf',
            citation: 'Vendor Contract, p. 2',
          },
          citation: 'Vendor Contract, p. 2',
        },
      ],
      total_matches: 1,
      returned: 1,
      offset: 0,
      truncated: false,
      sort_by: 'relevance',
      presentation: 'full',
    })

    renderSearchPage()

    fireEvent.click(screen.getByRole('button', { name: 'Find All mode' }))
    const input = screen.getByPlaceholderText('Find all matching documents...')
    fireEvent.change(input, { target: { value: 'force majeure' } })
    fireEvent.submit(input.closest('form') as HTMLFormElement)

    await waitFor(() => {
      expect(postMock).toHaveBeenCalledWith('/api/search/find-all', {
        query: 'force majeure',
        max_results: 25,
        offset: 0,
        presentation: 'full',
        sort_by: 'relevance',
      })
    })
    expect(await screen.findByText('Vendor Contract')).toBeInTheDocument()
    expect(screen.getByText('Force majeure clause excerpt.')).toBeInTheDocument()
    expect(screen.getByText('Vendor Contract, p. 2')).toBeInTheDocument()
    expect(screen.getByText('vendors/contract.pdf')).toBeInTheDocument()
  })
})

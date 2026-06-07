import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { get, post } from '../api'
import SearchPage from './SearchPage'

vi.mock('../api', () => ({
  get: vi.fn(),
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
const getMock = vi.mocked(get)

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
    getMock.mockReset()
    getMock.mockResolvedValue({
      mime_types: [{ value: 'application/pdf', count: 3 }],
    })
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
    expect(screen.getByText('Filename')).toBeInTheDocument()
    expect(screen.getByText('contract.pdf')).toBeInTheDocument()
    expect(screen.getByTitle('vendors/contract.pdf')).toBeInTheDocument()
    expect(screen.getByLabelText(/Relevance score 0.870/)).toBeInTheDocument()
  })

  it('serializes friendly filters into search request fields', async () => {
    postMock.mockResolvedValueOnce({
      hits: [],
      total_candidates: 0,
      has_more: false,
      possible_conflict: false,
      conflict_sources: [],
    })

    renderSearchPage()

    fireEvent.click(screen.getByRole('button', { name: /Filters/ }))
    await screen.findByRole('option', { name: 'PDF (3)' })
    fireEvent.change(screen.getByLabelText('Exact text'), { target: { value: 'force majeure' } })
    fireEvent.change(screen.getByLabelText('Language'), { target: { value: 'en' } })
    fireEvent.change(screen.getByLabelText('MIME type'), { target: { value: 'application/pdf' } })
    fireEvent.change(screen.getByLabelText('Email from'), { target: { value: 'alice@example.com' } })
    fireEvent.change(screen.getByLabelText('Email subject'), { target: { value: 'invoice' } })
    fireEvent.click(screen.getByText('Advanced identifier filter'))
    fireEvent.change(screen.getByLabelText('Document UUID'), {
      target: { value: '11111111-1111-1111-1111-111111111111' },
    })

    expect(screen.getByLabelText('Generated metadata JSON')).toHaveTextContent(
      '"email.from_address": "alice@example.com"',
    )
    expect(screen.getByText('Text: force majeure')).toBeInTheDocument()
    expect(screen.getByText('Type: PDF')).toBeInTheDocument()
    expect(screen.getByText('From: alice@example.com')).toBeInTheDocument()
    expect(screen.getByText('Document UUID: 11111111-1111-1111-1111-111111111111')).toBeInTheDocument()

    const input = screen.getByPlaceholderText('Search documents...')
    fireEvent.change(input, { target: { value: 'contract' } })
    fireEvent.submit(input.closest('form') as HTMLFormElement)

    await waitFor(() => {
      expect(postMock).toHaveBeenCalledWith('/api/search', {
        query: 'contract',
        k: 25,
        offset: 0,
        doc_id: '11111111-1111-1111-1111-111111111111',
        text_contains: 'force majeure',
        language: 'en',
        mime_type: 'application/pdf',
        metadata_filter: {
          'email.from_address': 'alice@example.com',
          'email.subject_contains': 'invoice',
        },
      })
    })
  })

  it('lets users remove individual filter chips', async () => {
    postMock.mockResolvedValueOnce({
      hits: [],
      total_candidates: 0,
      has_more: false,
      possible_conflict: false,
      conflict_sources: [],
    })

    renderSearchPage()

    fireEvent.click(screen.getByRole('button', { name: /Filters/ }))
    fireEvent.click(screen.getByText('Advanced identifier filter'))
    fireEvent.change(screen.getByLabelText('Document UUID'), {
      target: { value: '11111111-1111-1111-1111-111111111111' },
    })

    expect(screen.getByText('Document UUID: 11111111-1111-1111-1111-111111111111')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /Remove Document UUID:/ }))

    expect(screen.queryByText('Document UUID: 11111111-1111-1111-1111-111111111111')).not.toBeInTheDocument()

    const input = screen.getByPlaceholderText('Search documents...')
    fireEvent.change(input, { target: { value: 'contract' } })
    fireEvent.submit(input.closest('form') as HTMLFormElement)

    await waitFor(() => {
      expect(postMock).toHaveBeenCalledWith('/api/search', {
        query: 'contract',
        k: 25,
        offset: 0,
      })
    })
  })

  it('validates document id filters before submitting', () => {
    renderSearchPage()

    fireEvent.click(screen.getByRole('button', { name: /Filters/ }))
    fireEvent.click(screen.getByText('Advanced identifier filter'))
    fireEvent.change(screen.getByLabelText('Document UUID'), { target: { value: 'not-a-uuid' } })

    const input = screen.getByPlaceholderText('Search documents...')
    fireEvent.change(input, { target: { value: 'contract' } })
    fireEvent.submit(input.closest('form') as HTMLFormElement)

    expect(screen.getByText('Document UUID filter must be a valid UUID.')).toBeInTheDocument()
    expect(postMock).not.toHaveBeenCalled()
  })

  it('sends the selected find-all sort order', async () => {
    postMock.mockResolvedValueOnce({
      results: [],
      total_matches: 0,
      returned: 0,
      offset: 0,
      truncated: false,
      sort_by: 'date_desc',
      presentation: 'full',
    })

    renderSearchPage()

    fireEvent.click(screen.getByRole('button', { name: 'Find All mode' }))
    fireEvent.change(screen.getByLabelText('Find All sort'), { target: { value: 'date_desc' } })
    const input = screen.getByPlaceholderText('Find all matching documents...')
    fireEvent.change(input, { target: { value: 'renewal' } })
    fireEvent.submit(input.closest('form') as HTMLFormElement)

    await waitFor(() => {
      expect(postMock).toHaveBeenCalledWith('/api/search/find-all', {
        query: 'renewal',
        max_results: 25,
        offset: 0,
        presentation: 'full',
        sort_by: 'date_desc',
      })
    })
  })
})

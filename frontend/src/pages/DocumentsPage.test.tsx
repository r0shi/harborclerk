import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { get, post, del, downloadBlob } from '../api'
import { useAuth } from '../auth'
import { useLLMStatusContext } from '../components/LLMStatusBanner'
import { useJobEvents } from '../hooks/useJobEvents'
import { useSystemConfig } from '../hooks/useSystemConfig'
import DocumentsPage from './DocumentsPage'

vi.mock('../api', () => ({
  get: vi.fn(),
  post: vi.fn(),
  del: vi.fn(),
  downloadBlob: vi.fn(),
}))

vi.mock('../auth', () => ({
  useAuth: vi.fn(),
}))

vi.mock('../hooks/useJobEvents', () => ({
  useJobEvents: vi.fn(),
}))

vi.mock('../hooks/useSystemConfig', () => ({
  useSystemConfig: vi.fn(),
}))

vi.mock('../components/LLMStatusBanner', () => ({
  useLLMStatusContext: vi.fn(),
}))

const getMock = vi.mocked(get)
const postMock = vi.mocked(post)
const delMock = vi.mocked(del)
const downloadBlobMock = vi.mocked(downloadBlob)
const useAuthMock = vi.mocked(useAuth)
const useJobEventsMock = vi.mocked(useJobEvents)
const useSystemConfigMock = vi.mocked(useSystemConfig)
const useLLMStatusContextMock = vi.mocked(useLLMStatusContext)

const SAVED_STATE = {
  currentPage: 1,
  filter: '',
  filterInput: '',
  mimeFilter: '',
  langFilter: '',
  docTypeFilter: '',
  entityFilter: '',
  entityTypeFilter: '',
  entityInput: '',
  folderFilter: '',
  pipelineStatusFilter: '',
  summaryStateFilter: '',
  sortField: 'updated',
  sortDir: 'desc',
  scrollY: 0,
  expanded: ['doc-1'],
  selected: [],
}

function renderDocumentsPage() {
  render(
    <MemoryRouter>
      <DocumentsPage />
    </MemoryRouter>,
  )
}

describe('DocumentsPage', () => {
  beforeEach(() => {
    sessionStorage.clear()
    getMock.mockReset()
    postMock.mockReset()
    delMock.mockReset()
    downloadBlobMock.mockReset()
    useAuthMock.mockReset()
    useJobEventsMock.mockReset()
    useSystemConfigMock.mockReset()
    useLLMStatusContextMock.mockReset()

    useAuthMock.mockReturnValue({
      user: {
        user_id: 'user-1',
        email: 'admin@example.com',
        role: 'admin',
        preferences: { page_size: 10 },
      },
      token: 'token',
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      logout: vi.fn(),
      updatePreferences: vi.fn().mockResolvedValue(undefined),
      isAdmin: true,
    } as ReturnType<typeof useAuth>)
    useJobEventsMock.mockReturnValue(undefined)
    useSystemConfigMock.mockReturnValue({
      healthStatus: 'healthy',
      allowSourceDownload: false,
      enableCliAccess: false,
      cliShimInstallStatus: null,
      localMcpUrl: null,
      version: null,
      loaded: true,
    })
    useLLMStatusContextMock.mockReturnValue({
      status: { state: 'ready', model_id: 'qwen3', model_name: 'Qwen3' },
      markTransitioning: vi.fn(),
    })
  })

  it('fetches entities for rows restored as expanded on load', async () => {
    sessionStorage.setItem('docs-page-state', JSON.stringify(SAVED_STATE))
    const docsCalls: Array<Record<string, string | number> | undefined> = []
    getMock.mockImplementation((url, params) => {
      if (url === '/api/docs/filters') {
        return Promise.resolve({ mime_types: [], doc_types: [], languages: [], entity_types: [] })
      }
      if (url === '/api/stats/topics') return Promise.resolve({ clusters: [] })
      if (url === '/api/watch/folders') return Promise.resolve([])
      if (url === '/api/docs') {
        docsCalls.push(params)
        return Promise.resolve({
          items: [
            {
              doc_id: 'doc-1',
              title: 'Expanded doc',
              canonical_filename: 'expanded.pdf',
              status: 'active',
              pipeline_status: 'ready',
              created_at: '2026-06-04T12:00:00Z',
              updated_at: '2026-06-04T12:30:00Z',
              summary: 'Short summary',
              summary_model: 'extractive',
              doc_type: 'contract',
            },
          ],
          total: 1,
          limit: 10,
          offset: 0,
        })
      }
      if (url === '/api/docs/doc-1/entities') {
        return Promise.resolve({ entities: [{ entity_text: 'Acme Corp', entity_type: 'ORG' }] })
      }
      return Promise.reject(new Error(`Unexpected URL: ${url}`))
    })

    renderDocumentsPage()

    expect(await screen.findByText('Expanded doc')).toBeInTheDocument()
    expect(await screen.findByText('Acme Corp')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Acme Corp/ })).toHaveAttribute(
      'href',
      '/docs?entity=Acme%20Corp&entity_type=ORG',
    )
    fireEvent.click(screen.getByRole('link', { name: /Acme Corp/ }))
    await waitFor(() => {
      expect(docsCalls).toContainEqual(expect.objectContaining({ entity: 'Acme Corp', entity_type: 'ORG' }))
    })
    expect(getMock).toHaveBeenCalledWith('/api/docs/doc-1/entities')
  })

  it('sends summary state filters to the documents endpoint', async () => {
    const docsCalls: Array<Record<string, string | number> | undefined> = []
    getMock.mockImplementation((url, params) => {
      if (url === '/api/docs/filters') {
        return Promise.resolve({ mime_types: [], doc_types: [], languages: [], entity_types: [] })
      }
      if (url === '/api/stats/topics') return Promise.resolve({ clusters: [] })
      if (url === '/api/watch/folders') return Promise.resolve([])
      if (url === '/api/docs') {
        docsCalls.push(params)
        return Promise.resolve({
          items: [
            {
              doc_id: 'doc-1',
              title: 'Missing summary doc',
              canonical_filename: 'missing.pdf',
              status: 'active',
              pipeline_status: 'ready',
              created_at: '2026-06-04T12:00:00Z',
              updated_at: '2026-06-04T12:30:00Z',
              summary: null,
              summary_model: null,
              summarize_job_status: 'error',
              doc_type: 'contract',
            },
          ],
          total: 1,
          limit: 10,
          offset: 0,
        })
      }
      return Promise.reject(new Error(`Unexpected URL: ${url}`))
    })

    renderDocumentsPage()

    expect(await screen.findByText('Missing summary doc')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Summary state'), { target: { value: 'failed' } })

    await waitFor(() => {
      expect(docsCalls).toContainEqual(
        expect.objectContaining({
          summary_state: 'failed',
        }),
      )
    })
  })

  it('shows active filter chips and removes individual filters', async () => {
    const docsCalls: Array<Record<string, string | number> | undefined> = []
    getMock.mockImplementation((url, params) => {
      if (url === '/api/docs/filters') {
        return Promise.resolve({
          mime_types: [{ value: 'application/pdf', count: 1 }],
          doc_types: [],
          languages: [],
          entity_types: [],
        })
      }
      if (url === '/api/stats/topics') return Promise.resolve({ clusters: [] })
      if (url === '/api/watch/folders') return Promise.resolve([])
      if (url === '/api/docs') {
        docsCalls.push(params)
        return Promise.resolve({
          items: [
            {
              doc_id: 'doc-1',
              title: 'Filtered doc',
              canonical_filename: 'filtered.pdf',
              status: 'active',
              pipeline_status: 'ready',
              created_at: '2026-06-04T12:00:00Z',
              updated_at: '2026-06-04T12:30:00Z',
              summary: 'Short summary',
              summary_model: 'extractive',
              doc_type: 'contract',
            },
          ],
          total: 1,
          limit: 10,
          offset: 0,
        })
      }
      return Promise.reject(new Error(`Unexpected URL: ${url}`))
    })

    renderDocumentsPage()

    expect(await screen.findByText('Filtered doc')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('File type'), { target: { value: 'application/pdf' } })

    const chip = await screen.findByRole('button', { name: /Type: PDF/ })
    expect(chip).toBeInTheDocument()
    await waitFor(() => {
      expect(docsCalls).toContainEqual(expect.objectContaining({ mime_type: 'application/pdf' }))
    })

    fireEvent.click(chip)

    await waitFor(() => {
      const latestCall = docsCalls[docsCalls.length - 1]
      expect(latestCall).not.toEqual(expect.objectContaining({ mime_type: 'application/pdf' }))
    })
  })

  it('under Docker Compose, the no-model banner says which model to download, not to activate one', async () => {
    const hint = 'No local model is loaded yet. Download Qwen3.5-4B-Q4_K_M.gguf in Settings → Models.'
    useLLMStatusContextMock.mockReturnValue({
      status: { state: 'deactivated', model_id: null, model_name: null, setup_hint: hint },
      markTransitioning: vi.fn(),
    })
    getMock.mockImplementation((url) => {
      if (url === '/api/docs/filters') {
        return Promise.resolve({ mime_types: [], doc_types: [], languages: [], entity_types: [] })
      }
      if (url === '/api/stats/topics') return Promise.resolve({ clusters: [] })
      if (url === '/api/watch/folders') return Promise.resolve([])
      if (url === '/api/docs') return Promise.resolve({ items: [], total: 0, limit: 10, offset: 0 })
      return Promise.reject(new Error(`Unexpected URL: ${url}`))
    })

    renderDocumentsPage()

    const banner = await screen.findByText(/Download Qwen3\.5-4B-Q4_K_M\.gguf in Settings → Models\./)
    expect(banner.textContent).toContain('Until then, document summaries fall back to an extractive heuristic.')
    expect(screen.queryByText(/activate a model/)).not.toBeInTheDocument()
  })

  it('outside Compose, the no-model banner still says to activate a model', async () => {
    useLLMStatusContextMock.mockReturnValue({
      status: { state: 'deactivated', model_id: null, model_name: null },
      markTransitioning: vi.fn(),
    })
    getMock.mockImplementation((url) => {
      if (url === '/api/docs/filters') {
        return Promise.resolve({ mime_types: [], doc_types: [], languages: [], entity_types: [] })
      }
      if (url === '/api/stats/topics') return Promise.resolve({ clusters: [] })
      if (url === '/api/watch/folders') return Promise.resolve([])
      if (url === '/api/docs') return Promise.resolve({ items: [], total: 0, limit: 10, offset: 0 })
      return Promise.reject(new Error(`Unexpected URL: ${url}`))
    })

    renderDocumentsPage()

    expect(await screen.findByText(/activate a model\./)).toBeInTheDocument()
  })

  it('explains failed rows by family: pill, one-line notice, and the full notice when expanded', async () => {
    sessionStorage.setItem('docs-page-state', JSON.stringify(SAVED_STATE)) // doc-1 restored expanded
    const temporary = {
      family: 'temporary',
      message: 'A background service could not be reached while this file was processed.',
      action: 'Reprocess the document to try again.',
    }
    const unreadable = {
      family: 'unreadable',
      message: 'This file could not be read.',
      action: 'Check that the file opens on your computer.',
    }
    const row = (doc_id: string, title: string, error: string, error_info: object) => ({
      doc_id,
      title,
      status: 'active',
      pipeline_status: 'error',
      created_at: '2026-06-04T12:00:00Z',
      updated_at: '2026-06-04T12:30:00Z',
      error,
      error_info,
    })
    getMock.mockImplementation((url) => {
      if (url === '/api/docs/filters') {
        return Promise.resolve({ mime_types: [], doc_types: [], languages: [], entity_types: [] })
      }
      if (url === '/api/stats/topics') return Promise.resolve({ clusters: [] })
      if (url === '/api/watch/folders') return Promise.resolve([])
      if (url === '/api/docs') {
        return Promise.resolve({
          items: [
            row('doc-1', 'Blip doc', 'ConnectError: [Errno 61] Connection refused', temporary),
            row('doc-2', 'Broken doc', 'RuntimeError: Tika rejected file (422): POIXMLException', unreadable),
          ],
          total: 2,
          limit: 10,
          offset: 0,
        })
      }
      if (url.endsWith('/entities')) return Promise.resolve({ entities: [] })
      return Promise.reject(new Error(`Unexpected URL: ${url}`))
    })

    renderDocumentsPage()

    expect(await screen.findByText('Blip doc')).toBeInTheDocument()
    // Status pills follow the family instead of a uniform red "error".
    expect(screen.getByText('temporary error')).toBeInTheDocument()
    expect(screen.getByText("can't read file")).toBeInTheDocument()
    expect(screen.queryByText('error')).not.toBeInTheDocument()
    // One-line notice in each row, uncropped.
    expect(screen.getByText(`${temporary.message} ${temporary.action}`)).toHaveAttribute('data-family', 'temporary')
    expect(screen.getByText(`${unreadable.message} ${unreadable.action}`)).toHaveAttribute('data-family', 'unreadable')
    // The expanded row (doc-1) carries the full notice with the raw error behind a disclosure.
    expect(screen.getByText('Temporary problem')).toBeInTheDocument()
    expect(screen.getByText('ConnectError: [Errno 61] Connection refused').closest('details')).not.toBeNull()
    expect(screen.queryByText("This file can't be read")).not.toBeInTheDocument() // doc-2 is collapsed
  })
})

describe('DocumentsPage "Reprocess all failed" (#554, #852)', () => {
  function setup(isAdmin: boolean, pipelineStatusFilter: string, extraState: Record<string, string> = {}) {
    sessionStorage.clear()
    sessionStorage.setItem(
      'docs-page-state',
      JSON.stringify({ ...SAVED_STATE, expanded: [], pipelineStatusFilter, ...extraState }),
    )
    useAuthMock.mockReturnValue({
      user: { user_id: 'u', email: 'u@example.com', role: isAdmin ? 'admin' : 'user', preferences: { page_size: 10 } },
      token: 'token',
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      logout: vi.fn(),
      updatePreferences: vi.fn().mockResolvedValue(undefined),
      isAdmin,
    } as ReturnType<typeof useAuth>)
    useJobEventsMock.mockReturnValue(undefined)
    useSystemConfigMock.mockReturnValue({
      healthStatus: 'healthy',
      allowSourceDownload: false,
      enableCliAccess: false,
      cliShimInstallStatus: null,
      localMcpUrl: null,
      version: null,
      loaded: true,
    })
    useLLMStatusContextMock.mockReturnValue({
      status: { state: 'ready', model_id: 'qwen3', model_name: 'Qwen3' },
      markTransitioning: vi.fn(),
    })
    getMock.mockReset()
    getMock.mockImplementation((url) => {
      if (url === '/api/docs/filters') {
        return Promise.resolve({ mime_types: [], doc_types: [], languages: [], entity_types: [] })
      }
      if (url === '/api/stats/topics') return Promise.resolve({ clusters: [] })
      if (url === '/api/watch/folders') return Promise.resolve([])
      if (url === '/api/docs') {
        return Promise.resolve({
          items: [
            {
              doc_id: 'doc-1',
              title: 'Broken doc',
              canonical_filename: 'broken.pdf',
              status: 'active',
              pipeline_status: 'error',
              created_at: '2026-06-04T12:00:00Z',
              updated_at: '2026-06-04T12:30:00Z',
              summary: null,
              summary_model: null,
              doc_type: null,
            },
          ],
          total: 1,
          limit: 10,
          offset: 0,
        })
      }
      return Promise.reject(new Error(`Unexpected URL: ${url}`))
    })
    renderDocumentsPage()
  }

  it('an admin on the Failed view gets the action', async () => {
    setup(true, 'error')
    expect(await screen.findByText('Broken doc')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Reprocess all failed' })).toBeInTheDocument()
  })

  it('a non-admin on the Failed view does not', async () => {
    setup(false, 'error')
    expect(await screen.findByText('Broken doc')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Reprocess all failed' })).not.toBeInTheDocument()
  })

  it('an admin on any other view does not', async () => {
    setup(true, '')
    expect(await screen.findByText('Broken doc')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Reprocess all failed' })).not.toBeInTheDocument()
  })

  it('with another filter active, the confirmation says it covers the whole library', async () => {
    postMock.mockReset()
    postMock.mockResolvedValue({ requeued: 0, remaining: 500, limit: 1000 })
    setup(true, 'error', { mimeFilter: 'application/pdf' })
    expect(await screen.findByText('Broken doc')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Reprocess all failed' }))
    expect(await screen.findByText(/Reprocess all 500 failed documents in the library\?/)).toHaveTextContent(
      'Your other filters do not apply: this covers every failed document.',
    )
  })

  it('with only the Failed filter, makes no claim about other filters', async () => {
    postMock.mockReset()
    postMock.mockResolvedValue({ requeued: 0, remaining: 500, limit: 1000 })
    setup(true, 'error')
    expect(await screen.findByText('Broken doc')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Reprocess all failed' }))
    expect(await screen.findByText(/Reprocess all 500 failed documents in the library\?/)).toBeInTheDocument()
    expect(screen.queryByText(/other filters do not apply/)).not.toBeInTheDocument()
  })
})

describe('DocumentsPage empty corpus (#852)', () => {
  function renderEmpty(isAdmin: boolean) {
    sessionStorage.clear()
    useAuthMock.mockReturnValue({
      user: { user_id: 'u', email: 'u@example.com', role: isAdmin ? 'admin' : 'user', preferences: { page_size: 10 } },
      token: 'token',
      loading: false,
      needsSetup: false,
      login: vi.fn(),
      logout: vi.fn(),
      updatePreferences: vi.fn().mockResolvedValue(undefined),
      isAdmin,
    } as ReturnType<typeof useAuth>)
    useJobEventsMock.mockReturnValue(undefined)
    useSystemConfigMock.mockReturnValue({
      healthStatus: 'healthy',
      allowSourceDownload: false,
      enableCliAccess: false,
      cliShimInstallStatus: null,
      localMcpUrl: null,
      version: null,
      loaded: true,
    })
    useLLMStatusContextMock.mockReturnValue({
      status: { state: 'ready', model_id: 'qwen3', model_name: 'Qwen3' },
      markTransitioning: vi.fn(),
    })
    getMock.mockReset()
    getMock.mockImplementation((url) => {
      if (url === '/api/docs/filters') {
        return Promise.resolve({ mime_types: [], doc_types: [], languages: [], entity_types: [] })
      }
      if (url === '/api/stats/topics') return Promise.resolve({ clusters: [] })
      if (url === '/api/watch/folders') return Promise.resolve([])
      if (url === '/api/docs') return Promise.resolve({ items: [], total: 0, limit: 10, offset: 0 })
      return Promise.reject(new Error(`Unexpected URL: ${url}`))
    })
    renderDocumentsPage()
  }

  it('an admin gets the link to add a watched folder', async () => {
    renderEmpty(true)
    expect(await screen.findByRole('link', { name: 'Add a watched folder' })).toHaveAttribute('href', '/folders')
  })

  it('a non-admin gets a note instead of the link', async () => {
    renderEmpty(false)
    expect(await screen.findByText(/An administrator adds the folders Harbor Clerk watches\./)).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Add a watched folder' })).not.toBeInTheDocument()
  })
})

import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
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
      loaded: true,
    })
    useLLMStatusContextMock.mockReturnValue({
      status: { state: 'ready', model_id: 'qwen3', model_name: 'Qwen3' },
      markTransitioning: vi.fn(),
    })
  })

  it('fetches entities for rows restored as expanded on load', async () => {
    sessionStorage.setItem('docs-page-state', JSON.stringify(SAVED_STATE))
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
})

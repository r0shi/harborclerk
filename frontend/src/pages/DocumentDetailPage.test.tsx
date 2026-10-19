import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { get, post, del } from '../api'
import { useAuth } from '../auth'
import { useJobEvents } from '../hooks/useJobEvents'
import { useSystemConfig } from '../hooks/useSystemConfig'
import DocumentDetailPage from './DocumentDetailPage'

vi.mock('../api', () => ({
  get: vi.fn(),
  post: vi.fn(),
  del: vi.fn(),
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

const getMock = vi.mocked(get)
const postMock = vi.mocked(post)
const delMock = vi.mocked(del)
const useAuthMock = vi.mocked(useAuth)
const useJobEventsMock = vi.mocked(useJobEvents)
const useSystemConfigMock = vi.mocked(useSystemConfig)

const READY_DOC = {
  doc_id: 'doc-1',
  title: 'Ready document',
  canonical_filename: 'ready.pdf',
  status: 'active',
  pipeline_status: 'ready',
  pipeline_seq: 1,
  summary: null,
  doc_type: null,
  mime_type: 'application/pdf',
  source_path: '/tmp/ready.pdf',
  has_text_layer: true,
  needs_ocr: false,
  extracted_chars: 953,
  size_bytes: 1024,
  ocr_languages_used: null,
  error: null,
  created_at: '2026-06-04T12:00:00Z',
  updated_at: '2026-06-04T12:30:00Z',
  jobs: [
    {
      job_id: 'job-extract',
      stage: 'extract',
      status: 'done',
      created_at: '2026-06-04T12:00:00Z',
      finished_at: '2026-06-04T12:01:00Z',
    },
    {
      job_id: 'job-ocr',
      stage: 'ocr',
      status: 'skipped',
      error: null,
      created_at: '2026-06-04T12:01:00Z',
      finished_at: '2026-06-04T12:01:00Z',
    },
    {
      job_id: 'job-entities',
      stage: 'entities',
      status: 'skipped',
      error: 'spaCy NER models not available',
      created_at: '2026-06-04T12:02:00Z',
      finished_at: '2026-06-04T12:02:00Z',
    },
    {
      job_id: 'job-summarize',
      stage: 'summarize',
      status: 'skipped',
      error: null,
      created_at: '2026-06-04T12:03:00Z',
      finished_at: '2026-06-04T12:03:00Z',
    },
    {
      job_id: 'job-embed',
      stage: 'embed',
      status: 'done',
      progress_current: 2,
      progress_total: 2,
      created_at: '2026-06-04T12:04:00Z',
      finished_at: '2026-06-04T12:04:00Z',
    },
    {
      job_id: 'job-finalize',
      stage: 'finalize',
      status: 'done',
      created_at: '2026-06-04T12:05:00Z',
      finished_at: '2026-06-04T12:05:00Z',
    },
  ],
}

function renderDocumentDetail() {
  render(
    <MemoryRouter initialEntries={['/docs/doc-1']}>
      <Routes>
        <Route path="/docs/:id" element={<DocumentDetailPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('DocumentDetailPage', () => {
  beforeEach(() => {
    getMock.mockReset()
    postMock.mockReset()
    delMock.mockReset()
    useAuthMock.mockReset()
    useJobEventsMock.mockReset()
    useSystemConfigMock.mockReset()

    useAuthMock.mockReturnValue({
      isAdmin: true,
      user: { preferences: { page_size: 10, enabled_languages: ['en'] } },
    } as ReturnType<typeof useAuth>)
    useSystemConfigMock.mockReturnValue({
      healthStatus: 'healthy',
      allowSourceDownload: false,
      enableCliAccess: false,
      cliShimInstallStatus: null,
      localMcpUrl: null,
      version: null,
      loaded: true,
    })
    useJobEventsMock.mockReturnValue(undefined)
    getMock.mockImplementation((url) => {
      if (url === '/api/docs/doc-1') return Promise.resolve(READY_DOC)
      if (url === '/api/docs/doc-1/related') return Promise.resolve({ related: [] })
      return Promise.reject(new Error(`Unexpected URL: ${url}`))
    })
  })

  it('renders skipped ingestion stages as skipped instead of pending', async () => {
    renderDocumentDetail()

    expect(await screen.findByText('Ingestion complete')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Ingestion Jobs/ }))

    expect(screen.getAllByText('skipped')).toHaveLength(3)
    expect(screen.getByText('not needed')).toBeInTheDocument()
    expect(screen.getByText('spaCy NER models not available')).toBeInTheDocument()
    expect(screen.getByText('skipped by maintenance')).toBeInTheDocument()
    expect(screen.queryByText('pending')).not.toBeInTheDocument()
  })

  it('uses document artifacts over stale optional-stage pending jobs', async () => {
    getMock.mockImplementation((url) => {
      if (url === '/api/docs/doc-1') {
        return Promise.resolve({
          ...READY_DOC,
          summary: 'A valid summary exists.',
          jobs: READY_DOC.jobs.map((job) => {
            if (job.stage === 'ocr') return { ...job, status: 'queued', finished_at: null }
            if (job.stage === 'summarize') return { ...job, status: 'queued', finished_at: null }
            return job
          }),
        })
      }
      if (url === '/api/docs/doc-1/related') return Promise.resolve({ related: [] })
      return Promise.reject(new Error(`Unexpected URL: ${url}`))
    })

    renderDocumentDetail()

    expect(await screen.findByText('Ingestion complete')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Ingestion Jobs/ }))

    expect(screen.getByText('not needed')).toBeInTheDocument()
    expect(screen.getByText('summary available')).toBeInTheDocument()
    expect(screen.queryByText('pending')).not.toBeInTheDocument()
  })

  // #833: window.confirm silently returns false in the macOS app's WKWebView.
  it('deletes through the in-page confirm, even where window.confirm returns false', async () => {
    const nativeConfirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    delMock.mockResolvedValue(undefined)
    renderDocumentDetail()
    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }))
    expect(screen.getByText('Delete this document? This cannot be undone.')).toBeInTheDocument()
    expect(delMock).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm delete' }))
    await waitFor(() => expect(delMock).toHaveBeenCalledWith('/api/docs/doc-1'))
    expect(nativeConfirm).not.toHaveBeenCalled()
    nativeConfirm.mockRestore()
  })

  it('sends nothing when the document delete is cancelled', async () => {
    renderDocumentDetail()
    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('button', { name: 'Confirm delete' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Delete' })).toBeInTheDocument()
    expect(delMock).not.toHaveBeenCalled()
  })

  it('shows a failed delete on the page, clears the armed state and stays on the document', async () => {
    delMock.mockRejectedValue(new Error('Document is locked'))
    renderDocumentDetail()
    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }))
    fireEvent.click(screen.getByRole('button', { name: 'Confirm delete' }))
    expect(await screen.findByText('Document is locked')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Confirm delete' })).not.toBeInTheDocument()
    const deleteButton = screen.getByRole('button', { name: 'Delete' })
    expect(deleteButton).toBeEnabled() // actionLoading is released on failure
    expect(screen.getByRole('heading', { name: 'Ready document' })).toBeInTheDocument()
    expect(delMock).toHaveBeenCalledTimes(1)
  })

  // Pins current behaviour: Reprocess does not disarm a pending Delete. While it runs, Confirm
  // delete is disabled (actionLoading); once it finishes, the confirmation is still there.
  it('keeps an armed Delete through a Reprocess, disabled while the Reprocess runs', async () => {
    let finishReprocess: () => void = () => {}
    postMock.mockImplementation(
      () =>
        new Promise((resolve) => {
          finishReprocess = () => resolve(undefined)
        }),
    )
    renderDocumentDetail()
    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }))
    fireEvent.click(screen.getByRole('button', { name: 'Reprocess' }))
    await waitFor(() => expect(postMock).toHaveBeenCalledWith('/api/docs/doc-1/reprocess'))
    expect(screen.getByRole('button', { name: 'Confirm delete' })).toBeDisabled()

    finishReprocess()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Confirm delete' })).toBeEnabled())
    expect(delMock).not.toHaveBeenCalled()
  })

  it('does not carry an armed Delete to a related document opened from the page', async () => {
    const OTHER_DOC = { ...READY_DOC, doc_id: 'doc-2', title: 'Related document', canonical_filename: 'related.pdf' }
    getMock.mockImplementation((url) => {
      if (url === '/api/docs/doc-1') return Promise.resolve(READY_DOC)
      if (url === '/api/docs/doc-1/related')
        return Promise.resolve({
          related: [{ doc_id: 'doc-2', title: 'Related document', summary: null, similarity: 0.9 }],
        })
      if (url === '/api/docs/doc-2') return Promise.resolve(OTHER_DOC)
      if (url === '/api/docs/doc-2/related') return Promise.resolve({ related: [] })
      return Promise.reject(new Error(`Unexpected URL: ${url}`))
    })
    renderDocumentDetail()
    fireEvent.click(await screen.findByRole('button', { name: 'Delete' })) // arm on doc-1
    expect(screen.getByRole('button', { name: 'Confirm delete' })).toBeInTheDocument()

    fireEvent.click(await screen.findByRole('link', { name: /Related document/ })) // same route, doc-2
    expect(await screen.findByRole('heading', { name: 'Related document' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Confirm delete' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Delete' })).toBeInTheDocument()
    expect(delMock).not.toHaveBeenCalled()
  })
})

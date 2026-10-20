import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { get, post, del } from '../api'
import { useAuth } from '../auth'
import { useJobEvents, type JobEvent } from '../hooks/useJobEvents'
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

  it('names the stage whose error failed the document, not a summarize failure listed first', async () => {
    const embedError = 'EmbedderUnavailableError: embedder call failed after 7/7 attempts: HTTP 503'
    const failed = {
      ...READY_DOC,
      pipeline_status: 'error',
      error: embedError,
      error_info: { family: 'temporary', message: 'The embedding service was unavailable.', action: 'Reprocess.' },
      jobs: [
        {
          job_id: 'job-summarize',
          stage: 'summarize',
          status: 'error',
          error: 'ConnectError: [Errno 61] Connection refused',
          created_at: '2026-06-04T12:03:00Z',
        },
        { job_id: 'job-embed', stage: 'embed', status: 'error', error: embedError, created_at: '2026-06-04T12:04:00Z' },
      ],
    }
    getMock.mockImplementation((url) => {
      if (url === '/api/docs/doc-1') return Promise.resolve(failed)
      if (url === '/api/docs/doc-1/related') return Promise.resolve({ related: [] })
      return Promise.resolve({})
    })
    renderDocumentDetail()

    expect(await screen.findByText('(during Embed)')).toBeInTheDocument()
    expect(screen.queryByText('(during Summarize)')).not.toBeInTheDocument()
  })

  it.each([
    {
      name: 'a cancel writes one text on every job: the gating stage is named, not summarize',
      docError: 'Cancelled by user',
      jobs: [
        ['summarize', 'Cancelled by user'],
        ['embed', 'Cancelled by user'],
      ],
      expected: 'Embed',
      notExpected: 'Summarize',
    },
    {
      name: 'two gating stages with different errors: the one matching the document error is named',
      docError: "KeyError: 'label'",
      jobs: [
        ['embed', 'ReadTimeout'],
        ['entities', "KeyError: 'label'"],
      ],
      expected: 'Entities',
      notExpected: 'Embed',
    },
  ])('stage pick: $name', async ({ docError, jobs, expected, notExpected }) => {
    const failed = {
      ...READY_DOC,
      pipeline_status: 'error',
      error: docError,
      error_info: { family: 'other', message: 'Processing failed.', action: 'Reprocess.' },
      jobs: jobs.map(([stage, error]) => ({
        job_id: `job-${stage}`,
        stage,
        status: 'error',
        error,
        created_at: '2026-06-04T12:03:00Z',
      })),
    }
    getMock.mockImplementation((url) => {
      if (url === '/api/docs/doc-1') return Promise.resolve(failed)
      if (url === '/api/docs/doc-1/related') return Promise.resolve({ related: [] })
      return Promise.resolve({})
    })
    renderDocumentDetail()

    expect(await screen.findByText(`(during ${expected})`)).toBeInTheDocument()
    expect(screen.queryByText(`(during ${notExpected})`)).not.toBeInTheDocument()
  })

  it('explains a failed document in plain language, with the raw error kept once', async () => {
    const raw = 'ConnectError: [Errno 61] Connection refused'
    const info = {
      family: 'temporary',
      message: 'A background service could not be reached while this file was processed.',
      action: 'Reprocess the document to try again.',
    }
    const failed = {
      ...READY_DOC,
      pipeline_status: 'error',
      error: raw,
      error_info: info,
      jobs: [
        { ...READY_DOC.jobs[0] },
        {
          job_id: 'job-embed',
          stage: 'embed',
          status: 'error',
          error: raw,
          error_info: info,
          created_at: '2026-06-04T12:04:00Z',
          finished_at: '2026-06-04T12:04:00Z',
        },
      ],
    }
    getMock.mockImplementation((url) => {
      if (url === '/api/docs/doc-1') return Promise.resolve(failed)
      if (url === '/api/docs/doc-1/related') return Promise.resolve({ related: [] })
      return Promise.resolve({})
    })
    renderDocumentDetail()

    expect(await screen.findByText(info.message)).toBeInTheDocument()
    expect(screen.getByText(info.action)).toBeInTheDocument()
    expect(screen.getByText(info.message).closest('[data-family]')).toHaveAttribute('data-family', 'temporary')
    // Shown once, behind the disclosure, not as the old red "Error: ..." line.
    expect(screen.getAllByText(raw)).toHaveLength(1)
    expect(screen.queryByText(`Error: ${raw}`)).not.toBeInTheDocument()
  })

  it('explains a pipeline error the document carries even when no job row is in error', async () => {
    const info = {
      family: 'unreadable',
      message: 'The source file could not be read.',
      action: 'Check that the file is still in the watched folder and can be opened.',
    }
    const failed = {
      ...READY_DOC,
      pipeline_status: 'error',
      error: 'WatchedFileMissing: the watched file of doc doc-1 cannot be read in place',
      error_info: info,
      jobs: [{ ...READY_DOC.jobs[0] }],
    }
    getMock.mockImplementation((url) => {
      if (url === '/api/docs/doc-1') return Promise.resolve(failed)
      if (url === '/api/docs/doc-1/related') return Promise.resolve({ related: [] })
      return Promise.resolve({})
    })
    renderDocumentDetail()

    expect(await screen.findByText(info.message)).toBeInTheDocument()
    expect(screen.getByText(info.message).closest('[data-family]')).toHaveAttribute('data-family', 'unreadable')
  })

  it('refetches the document when a live job event reports an error, so its explanation arrives', async () => {
    let listener: ((event: JobEvent) => void) | undefined
    useJobEventsMock.mockImplementation((cb) => {
      listener = cb
    })
    const info = {
      family: 'temporary',
      message: 'The embedding service was unavailable, so this document could not be indexed.',
      action: 'Reprocess the document once the embedding service is running again.',
    }
    const running = {
      ...READY_DOC,
      pipeline_status: 'embedding',
      jobs: [{ ...READY_DOC.jobs[0] }, { ...READY_DOC.jobs[5], status: 'running', finished_at: null }],
    }
    const failed = {
      ...running,
      pipeline_status: 'error',
      error: 'Embedder unavailable: gave up after 6 attempts',
      error_info: info,
      jobs: [running.jobs[0], { ...running.jobs[1], status: 'error', error: 'x', error_info: info }],
    }
    let detailCalls = 0
    getMock.mockImplementation((url) => {
      if (url === '/api/docs/doc-1') return Promise.resolve(detailCalls++ === 0 ? running : failed)
      if (url === '/api/docs/doc-1/related') return Promise.resolve({ related: [] })
      return Promise.resolve({})
    })
    renderDocumentDetail()
    expect(await screen.findByText('Ready document')).toBeInTheDocument()
    expect(detailCalls).toBe(1)

    act(() => {
      listener?.({ doc_id: 'doc-1', stage: 'embed', status: 'error', error: 'Embedder unavailable: gave up' })
    })

    expect(await screen.findByText(info.message)).toBeInTheDocument()
    expect(detailCalls).toBeGreaterThanOrEqual(2)
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

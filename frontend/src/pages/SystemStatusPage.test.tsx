import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { get, post } from '../api'
import { listMailAccounts } from '../api/mail'
import { useLLMStatusContext } from '../components/LLMStatusBanner'
import SystemStatusPage from './SystemStatusPage'

vi.mock('../api', () => ({
  get: vi.fn(),
  post: vi.fn(),
}))

vi.mock('../api/mail', () => ({
  listMailAccounts: vi.fn(),
}))

vi.mock('../components/LLMStatusBanner', () => ({
  useLLMStatusContext: vi.fn(),
}))

const getMock = vi.mocked(get)
const postMock = vi.mocked(post)
const listMailAccountsMock = vi.mocked(listMailAccounts)
const useLLMStatusContextMock = vi.mocked(useLLMStatusContext)

const HEALTHY = {
  status: 'healthy',
  checks: {
    postgres: 'ok',
    storage: 'ok',
    tika: 'ok',
    reranker: 'ok',
  },
}

const SUMMARY = {
  state: 'needs_attention',
  counts: {
    active_documents: 3,
    ready_documents: 1,
    processing_documents: 1,
    failed_documents: 1,
    queued_jobs: 2,
    running_jobs: 1,
    failed_jobs: 1,
    watched_folders: 1,
    unavailable_folders: 0,
    ner_skipped_documents: 0,
    stuck_jobs: 1,
  },
  needs_attention: [
    {
      kind: 'failed_documents',
      severity: 'error',
      title: 'Documents failed ingest',
      detail: '1 active document needs review.',
      count: 1,
      action_label: 'Review failed documents',
      action_href: '/docs?pipeline_status=error',
    },
    {
      kind: 'stuck_jobs',
      severity: 'error',
      title: 'Processing jobs may be stuck',
      detail: '1 running job appears stale.',
      count: 1,
      action_label: 'Recover stuck jobs',
      action_kind: 'reaper',
    },
  ],
  recent_failed_documents: [
    {
      doc_id: 'doc-1',
      title: 'Broken scan',
      pipeline_status: 'error',
      error: 'Tika failed',
      failed_stage: 'extract',
      updated_at: '2026-06-04T12:00:00Z',
    },
  ],
  recent_processing_documents: [
    {
      doc_id: 'doc-2',
      title: 'Still embedding',
      pipeline_status: 'embedding',
      updated_at: '2026-06-04T12:01:00Z',
    },
  ],
}

function mockRequests(summary = SUMMARY) {
  getMock.mockImplementation((url) => {
    if (url === '/api/system/health') return Promise.resolve(HEALTHY)
    if (url === '/api/system/status-summary') return Promise.resolve(summary)
    if (url === '/api/system/stats') return Promise.resolve({})
    return Promise.reject(new Error(`Unexpected URL: ${url}`))
  })
  postMock.mockResolvedValue({ reaped: 1 })
  listMailAccountsMock.mockResolvedValue([])
  useLLMStatusContextMock.mockReturnValue({
    status: { state: 'ready', model_id: 'qwen3', model_name: 'Qwen3' },
    markTransitioning: vi.fn(),
  })
}

function renderStatusPage() {
  render(
    <MemoryRouter>
      <SystemStatusPage />
    </MemoryRouter>,
  )
}

describe('SystemStatusPage', () => {
  beforeEach(() => {
    getMock.mockReset()
    postMock.mockReset()
    listMailAccountsMock.mockReset()
    useLLMStatusContextMock.mockReset()
  })

  it('renders needs-attention recovery actions and failed document links', async () => {
    mockRequests()

    renderStatusPage()

    expect(await screen.findByText('Documents failed ingest')).toBeInTheDocument()
    const reviewLink = screen.getByRole('link', { name: 'Review failed documents' })
    expect(reviewLink).toHaveAttribute('href', '/docs?pipeline_status=error')
    expect(screen.getByText('Broken scan')).toBeInTheDocument()
    expect(screen.getByText('Still embedding')).toBeInTheDocument()
  })

  it('runs the reaper action from a stuck-job issue', async () => {
    mockRequests()

    renderStatusPage()

    fireEvent.click(await screen.findByRole('button', { name: 'Recover stuck jobs' }))

    await waitFor(() => expect(postMock).toHaveBeenCalledWith('/api/system/reaper-run'))
    expect(await screen.findByText('Recovered 1 stale job.')).toBeInTheDocument()
  })

  it('shows warning-only entity issues as review items with maintenance CTA', async () => {
    mockRequests({
      state: 'needs_attention',
      counts: {
        ...SUMMARY.counts,
        processing_documents: 0,
        failed_documents: 0,
        queued_jobs: 0,
        running_jobs: 0,
        failed_jobs: 0,
        ner_skipped_documents: 12,
        stuck_jobs: 0,
      },
      needs_attention: [
        {
          kind: 'entity_extraction_skipped',
          severity: 'warning',
          title: 'Entity extraction skipped some documents',
          detail: '12 documents were processed while spaCy NER models were unavailable.',
          count: 12,
          action_label: 'Open maintenance',
          action_href: '/settings/maintenance',
        },
      ],
      recent_failed_documents: [],
      recent_processing_documents: [],
    })

    renderStatusPage()

    expect(await screen.findByText('Entity extraction skipped some documents')).toBeInTheDocument()
    expect(screen.getAllByText('Review').length).toBeGreaterThanOrEqual(1)
    expect(screen.getByRole('link', { name: 'Open maintenance' })).toHaveAttribute('href', '/settings/maintenance')
  })
})

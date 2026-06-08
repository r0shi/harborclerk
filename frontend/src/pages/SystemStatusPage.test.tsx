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
    embedder: 'ok',
    reranker: 'ok',
    local_https_gateway: 'ok',
  },
}

const STATS = {
  postgres: {
    db_size_mb: 12.4,
    active_connections: 3,
    cache_hit_ratio: 0.99,
    total_chunks: 40,
    dead_tuples: 0,
  },
  storage: {
    object_count: 2,
    total_size_mb: 5.5,
  },
  queues: {
    io_queued: 2,
    io_running: 1,
    cpu_queued: 0,
    cpu_running: 1,
    llm_queued: 3,
    llm_running: 1,
  },
}

const SUMMARY = {
  state: 'needs_attention',
  counts: {
    active_documents: 3,
    ready_documents: 1,
    stored_ready_documents: 1,
    processing_documents: 1,
    summarizing_documents: 0,
    pipeline_processing_documents: 1,
    stranded_documents: 0,
    completed_status_stale_documents: 0,
    failed_documents: 1,
    queued_jobs: 2,
    running_jobs: 1,
    summarizing_queued_jobs: 0,
    summarizing_running_jobs: 0,
    total_queued_jobs: 2,
    total_running_jobs: 1,
    failed_jobs: 1,
    failed_summarize_jobs: 0,
    blocked_summarize_jobs: 0,
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
      processing_stage: 'embed',
      job_status: 'running',
      updated_at: '2026-06-04T12:01:00Z',
    },
  ],
}

function mockRequests(summary = SUMMARY) {
  getMock.mockImplementation((url) => {
    if (url === '/api/system/health') return Promise.resolve(HEALTHY)
    if (url === '/api/system/status-summary') return Promise.resolve(summary)
    if (url === '/api/system/stats') return Promise.resolve(STATS)
    return Promise.reject(new Error(`Unexpected URL: ${url}`))
  })
  postMock.mockResolvedValue({ reaped: 1 })
  listMailAccountsMock.mockResolvedValue([])
  useLLMStatusContextMock.mockReturnValue({
    status: {
      state: 'ready',
      model_id: 'qwen3',
      model_name: 'Qwen3',
      summarize: { backend: 'apple-intelligence', name: 'Apple Intelligence', state: 'ready' },
    },
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
    expect(screen.getByText('Local HTTPS Gateway')).toBeInTheDocument()
    const reviewLink = screen.getByRole('link', { name: 'Review failed documents' })
    expect(reviewLink).toHaveAttribute('href', '/docs?pipeline_status=error')
    expect(screen.getByText('Broken scan')).toBeInTheDocument()
    expect(screen.getByText('Still embedding')).toBeInTheDocument()
  })

  it('renders service and worker diagnostics', async () => {
    mockRequests()

    renderStatusPage()

    expect(await screen.findByText('Diagnostics')).toBeInTheDocument()
    expect(screen.getByText('Embedder')).toBeInTheDocument()
    expect(screen.getAllByText('Local AI').length).toBeGreaterThanOrEqual(2)
    expect(screen.getByText('Workers / queues')).toBeInTheDocument()
    expect(screen.getByText('Qwen3')).toBeInTheDocument()
    expect(screen.getByText('Apple Intelligence')).toBeInTheDocument()
    expect(screen.getByText('IO workers')).toBeInTheDocument()
    expect(screen.getByText('2 queued, 1 running')).toBeInTheDocument()
    expect(screen.getByText('LLM worker')).toBeInTheDocument()
    expect(screen.getByText('3 queued, 1 running')).toBeInTheDocument()
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
        pipeline_processing_documents: 0,
        stranded_documents: 0,
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

  it('shows summary generation failures without failed documents', async () => {
    mockRequests({
      state: 'needs_attention',
      counts: {
        ...SUMMARY.counts,
        processing_documents: 0,
        pipeline_processing_documents: 0,
        stranded_documents: 0,
        failed_documents: 0,
        failed_summarize_jobs: 11,
        queued_jobs: 0,
        running_jobs: 0,
        failed_jobs: 11,
        ner_skipped_documents: 0,
        stuck_jobs: 0,
      },
      needs_attention: [
        {
          kind: 'summary_generation_failed',
          severity: 'warning',
          title: 'Summaries failed to generate',
          detail: '11 document summaries failed to generate. Documents remain searchable.',
          count: 11,
          action_label: 'Open maintenance',
          action_href: '/settings/maintenance',
        },
      ],
      recent_failed_documents: [],
      recent_processing_documents: [],
    })

    renderStatusPage()

    expect(await screen.findByText('Summaries failed to generate')).toBeInTheDocument()
    expect(screen.getByText('Summary failures')).toBeInTheDocument()
    expect(screen.getByText('11')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Open maintenance' })).toHaveAttribute('href', '/settings/maintenance')
  })

  it('shows blocked summary retries without failed documents', async () => {
    mockRequests({
      state: 'needs_attention',
      counts: {
        ...SUMMARY.counts,
        processing_documents: 0,
        pipeline_processing_documents: 0,
        stranded_documents: 0,
        failed_documents: 0,
        failed_summarize_jobs: 0,
        blocked_summarize_jobs: 11,
        queued_jobs: 0,
        running_jobs: 0,
        failed_jobs: 0,
        ner_skipped_documents: 0,
        stuck_jobs: 0,
      },
      needs_attention: [
        {
          kind: 'summary_generation_blocked',
          severity: 'warning',
          title: 'Apple Intelligence summaries are paused',
          detail: '11 summary jobs are waiting because Apple Intelligence is unavailable.',
          count: 11,
          action_label: 'Open Models',
          action_href: '/settings/models',
        },
      ],
      recent_failed_documents: [],
      recent_processing_documents: [],
    })

    renderStatusPage()

    expect(await screen.findByText('Apple Intelligence summaries are paused')).toBeInTheDocument()
    expect(screen.getByText('Summary paused')).toBeInTheDocument()
    expect(screen.getByText('11')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Open Models' })).toHaveAttribute('href', '/settings/models')
  })

  it('separates stale pipeline state from live processing', async () => {
    mockRequests({
      state: 'needs_attention',
      counts: {
        ...SUMMARY.counts,
        processing_documents: 0,
        pipeline_processing_documents: 12,
        stranded_documents: 12,
        failed_documents: 0,
        queued_jobs: 0,
        running_jobs: 0,
        failed_jobs: 0,
        stuck_jobs: 0,
      },
      needs_attention: [
        {
          kind: 'stranded_pipeline_state',
          severity: 'warning',
          title: 'Some documents are marked processing without active jobs',
          detail: '12 active documents have an in-progress pipeline state but no queued or running ingest job.',
          count: 12,
          action_label: 'Open maintenance',
          action_href: '/settings/maintenance',
        },
      ],
      recent_failed_documents: [],
      recent_processing_documents: [],
    })

    renderStatusPage()

    expect(await screen.findByText('Some documents are marked processing without active jobs')).toBeInTheDocument()
    expect(screen.getByText('Needs recovery')).toBeInTheDocument()
    expect(screen.getByText('12')).toBeInTheDocument()
    expect(screen.queryByText('Processing now')).not.toBeInTheDocument()
  })

  it('shows summarize-only backlog without inflating foreground processing', async () => {
    mockRequests({
      state: 'processing',
      counts: {
        ...SUMMARY.counts,
        ready_documents: 4,
        processing_documents: 0,
        summarizing_documents: 4,
        pipeline_processing_documents: 0,
        stranded_documents: 0,
        completed_status_stale_documents: 0,
        failed_documents: 0,
        queued_jobs: 0,
        running_jobs: 0,
        summarizing_queued_jobs: 3,
        summarizing_running_jobs: 1,
        total_queued_jobs: 3,
        total_running_jobs: 1,
        failed_jobs: 0,
        stuck_jobs: 0,
      },
      needs_attention: [],
      recent_failed_documents: [],
      recent_processing_documents: [],
    })

    renderStatusPage()

    expect(await screen.findByText('4 summarizing')).toBeInTheDocument()
    expect(screen.getAllByText('Summarizing').length).toBeGreaterThanOrEqual(1)
    expect(screen.getByText('Processing')).toBeInTheDocument()
    expect(screen.queryByText('Processing now')).not.toBeInTheDocument()
  })

  it('repairs completed documents with stale document status', async () => {
    mockRequests({
      state: 'needs_attention',
      counts: {
        ...SUMMARY.counts,
        ready_documents: 13,
        stored_ready_documents: 10,
        processing_documents: 0,
        pipeline_processing_documents: 3,
        stranded_documents: 0,
        completed_status_stale_documents: 3,
        failed_documents: 0,
        queued_jobs: 0,
        running_jobs: 0,
        failed_jobs: 0,
        stuck_jobs: 0,
      },
      needs_attention: [
        {
          kind: 'completed_status_stale',
          severity: 'warning',
          title: 'Completed documents need status cleanup',
          detail: '3 active documents completed ingest but still show an in-progress document status.',
          count: 3,
          action_label: 'Repair statuses',
          action_kind: 'repair_completed_statuses',
        },
      ],
      recent_failed_documents: [],
      recent_processing_documents: [],
    })
    postMock.mockResolvedValueOnce({ repaired: 3 })

    renderStatusPage()

    expect(await screen.findByText('Completed documents need status cleanup')).toBeInTheDocument()
    expect(screen.getByText('Status cleanup')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Repair statuses' }))

    await waitFor(() => expect(postMock).toHaveBeenCalledWith('/api/system/repair-completed-statuses'))
    expect(await screen.findByText('Repaired 3 completed document statuses.')).toBeInTheDocument()
  })
})

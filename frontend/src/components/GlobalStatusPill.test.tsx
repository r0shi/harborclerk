import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { get } from '../api'
import { useAuth } from '../auth'
import { useSystemConfig, type SystemConfig } from '../hooks/useSystemConfig'
import { useLLMStatusContext } from './LLMStatusBanner'
import GlobalStatusPill from './GlobalStatusPill'

vi.mock('../api', () => ({
  get: vi.fn(),
}))

vi.mock('../auth', () => ({
  useAuth: vi.fn(),
}))

vi.mock('../hooks/useSystemConfig', () => ({
  useSystemConfig: vi.fn(),
}))

vi.mock('./LLMStatusBanner', () => ({
  useLLMStatusContext: vi.fn(),
}))

const getMock = vi.mocked(get)
const useAuthMock = vi.mocked(useAuth)
const useSystemConfigMock = vi.mocked(useSystemConfig)
const useLLMStatusContextMock = vi.mocked(useLLMStatusContext)

const READY_CONFIG: SystemConfig = {
  healthStatus: 'healthy',
  allowSourceDownload: false,
  enableCliAccess: false,
  cliShimInstallStatus: null,
  localMcpUrl: null,
  loaded: true,
}

interface TestStatusSummary {
  state: 'ready' | 'processing' | 'needs_attention'
  counts: {
    processing_documents: number
    summarizing_documents?: number
    stranded_documents?: number
    completed_status_stale_documents?: number
    queued_jobs: number
    running_jobs: number
    summarizing_queued_jobs?: number
    summarizing_running_jobs?: number
    failed_documents: number
    failed_summarize_jobs?: number
    blocked_summarize_jobs?: number
    unavailable_folders: number
    ner_skipped_documents: number
    stuck_jobs: number
  }
  needs_attention: Array<{
    kind: string
    severity: 'error' | 'warning'
    title: string
    detail: string
    count?: number
  }>
}

const READY_SUMMARY: TestStatusSummary = {
  state: 'ready',
  counts: {
    processing_documents: 0,
    summarizing_documents: 0,
    stranded_documents: 0,
    completed_status_stale_documents: 0,
    queued_jobs: 0,
    running_jobs: 0,
    summarizing_queued_jobs: 0,
    summarizing_running_jobs: 0,
    failed_documents: 0,
    unavailable_folders: 0,
    ner_skipped_documents: 0,
    stuck_jobs: 0,
  },
  needs_attention: [],
}

function mockPill({
  config = READY_CONFIG,
  llmState = 'ready',
  isAdmin = true,
  summary = READY_SUMMARY,
}: {
  config?: SystemConfig
  llmState?: 'deactivated' | 'loading' | 'ready' | 'unknown'
  isAdmin?: boolean
  summary?: TestStatusSummary
}) {
  useAuthMock.mockReturnValue({ isAdmin } as ReturnType<typeof useAuth>)
  useSystemConfigMock.mockReturnValue(config)
  useLLMStatusContextMock.mockReturnValue({
    status: {
      state: llmState,
      model_id: llmState === 'ready' ? 'qwen3' : null,
      model_name: llmState === 'ready' ? 'Qwen3' : null,
    },
    markTransitioning: vi.fn(),
  })
  getMock.mockResolvedValue(summary)
}

function renderPill() {
  render(
    <MemoryRouter>
      <GlobalStatusPill />
    </MemoryRouter>,
  )
}

describe('GlobalStatusPill', () => {
  beforeEach(() => {
    getMock.mockReset()
    useAuthMock.mockReset()
    useSystemConfigMock.mockReset()
    useLLMStatusContextMock.mockReset()
  })

  it('links degraded system health to Status', async () => {
    mockPill({ config: { ...READY_CONFIG, healthStatus: 'degraded' } })

    renderPill()

    const link = await screen.findByRole('link', { name: 'Status: Needs attention' })
    expect(link).toHaveAttribute('href', '/settings/status')
    expect(screen.getByText('Needs attention')).toBeInTheDocument()
  })

  it('links inactive local AI to Models', async () => {
    mockPill({ llmState: 'deactivated' })

    renderPill()

    const link = await screen.findByRole('link', { name: 'Status: Choose model' })
    expect(link).toHaveAttribute('href', '/settings/models')
  })

  it('shows ready when system health and local AI are ready', async () => {
    mockPill({})

    renderPill()

    const link = await screen.findByRole('link', { name: 'Status: Ready' })
    expect(link).toHaveAttribute('href', '/settings/status')
  })

  it('surfaces failed documents from the status summary', async () => {
    mockPill({
      summary: {
        ...READY_SUMMARY,
        state: 'needs_attention',
        counts: {
          ...READY_SUMMARY.counts,
          failed_documents: 3,
        },
        needs_attention: [
          {
            kind: 'failed_documents',
            severity: 'error',
            title: 'Documents failed ingest',
            detail: '3 active documents need review.',
            count: 3,
          },
        ],
      },
    })

    renderPill()

    const link = await screen.findByRole('link', { name: 'Status: 3 failed docs' })
    expect(link).toHaveAttribute('href', '/settings/status')
    expect(link).toHaveAttribute('title', '3 active documents need review.')
  })

  it('names entity-only warning as skipped entities', async () => {
    mockPill({
      summary: {
        ...READY_SUMMARY,
        state: 'needs_attention',
        counts: {
          ...READY_SUMMARY.counts,
          ner_skipped_documents: 12,
        },
        needs_attention: [
          {
            kind: 'entity_extraction_skipped',
            severity: 'warning',
            title: 'Entity extraction skipped some documents',
            detail: '12 documents need reprocessing before entity filters are complete.',
            count: 12,
          },
        ],
      },
    })

    renderPill()

    const link = await screen.findByRole('link', { name: 'Status: Entities skipped' })
    expect(link).toHaveAttribute('href', '/settings/status')
    expect(link).toHaveAttribute('title', '12 documents need reprocessing before entity filters are complete.')
  })

  it('names summary-only warning as summary failures', async () => {
    mockPill({
      summary: {
        ...READY_SUMMARY,
        state: 'needs_attention',
        counts: {
          ...READY_SUMMARY.counts,
          failed_summarize_jobs: 8,
        },
        needs_attention: [
          {
            kind: 'summary_generation_failed',
            severity: 'warning',
            title: 'Summaries failed to generate',
            detail: '8 document summaries failed to generate. Documents remain searchable.',
            count: 8,
          },
        ],
      },
    })

    renderPill()

    const link = await screen.findByRole('link', { name: 'Status: Summary failures' })
    expect(link).toHaveAttribute('href', '/settings/status')
    expect(link).toHaveAttribute('title', '8 document summaries failed to generate. Documents remain searchable.')
  })

  it('names blocked summary retries as summary paused', async () => {
    mockPill({
      summary: {
        ...READY_SUMMARY,
        state: 'needs_attention',
        counts: {
          ...READY_SUMMARY.counts,
          blocked_summarize_jobs: 8,
        },
        needs_attention: [
          {
            kind: 'summary_generation_blocked',
            severity: 'warning',
            title: 'Apple Intelligence summaries are paused',
            detail: '8 summary jobs are waiting because Apple Intelligence is unavailable.',
            count: 8,
          },
        ],
      },
    })

    renderPill()

    const link = await screen.findByRole('link', { name: 'Status: Summary paused' })
    expect(link).toHaveAttribute('href', '/settings/status')
    expect(link).toHaveAttribute('title', '8 summary jobs are waiting because Apple Intelligence is unavailable.')
  })

  it('shows document processing before local AI ready state', async () => {
    mockPill({
      summary: {
        ...READY_SUMMARY,
        state: 'processing',
        counts: {
          ...READY_SUMMARY.counts,
          processing_documents: 2,
        },
      },
    })

    renderPill()

    expect(await screen.findByRole('link', { name: 'Status: 2 processing' })).toHaveAttribute(
      'href',
      '/settings/status',
    )
  })

  it('names summarize-only background work separately from foreground processing', async () => {
    mockPill({
      summary: {
        ...READY_SUMMARY,
        state: 'processing',
        counts: {
          ...READY_SUMMARY.counts,
          summarizing_documents: 9,
          summarizing_queued_jobs: 8,
          summarizing_running_jobs: 1,
        },
      },
    })

    renderPill()

    const link = await screen.findByRole('link', { name: 'Status: Summarizing 9' })
    expect(link).toHaveAttribute('href', '/settings/status')
    expect(link).toHaveAttribute('title', 'Document summaries are being generated')
  })

  it('names stranded pipeline-state warnings as stale state', async () => {
    mockPill({
      summary: {
        ...READY_SUMMARY,
        state: 'needs_attention',
        counts: {
          ...READY_SUMMARY.counts,
          stranded_documents: 7,
        },
        needs_attention: [
          {
            kind: 'stranded_pipeline_state',
            severity: 'warning',
            title: 'Some documents are marked processing without active jobs',
            detail: '7 active documents have an in-progress pipeline state but no queued or running ingest job.',
            count: 7,
          },
        ],
      },
    })

    renderPill()

    const link = await screen.findByRole('link', { name: 'Status: Stale state' })
    expect(link).toHaveAttribute('href', '/settings/status')
    expect(link).toHaveAttribute(
      'title',
      '7 active documents have an in-progress pipeline state but no queued or running ingest job.',
    )
  })

  it('names completed stale-status warnings as status cleanup', async () => {
    mockPill({
      summary: {
        ...READY_SUMMARY,
        state: 'needs_attention',
        counts: {
          ...READY_SUMMARY.counts,
          completed_status_stale_documents: 7,
        },
        needs_attention: [
          {
            kind: 'completed_status_stale',
            severity: 'warning',
            title: 'Completed documents need status cleanup',
            detail: '7 active documents completed ingest but still show an in-progress document status.',
            count: 7,
          },
        ],
      },
    })

    renderPill()

    const link = await screen.findByRole('link', { name: 'Status: Status cleanup' })
    expect(link).toHaveAttribute('href', '/settings/status')
    expect(link).toHaveAttribute(
      'title',
      '7 active documents completed ingest but still show an in-progress document status.',
    )
  })

  it('does not request the admin-only summary for non-admin users', () => {
    mockPill({ isAdmin: false, llmState: 'deactivated' })

    renderPill()

    expect(getMock).not.toHaveBeenCalledWith('/api/system/status-summary')
    expect(screen.getByRole('link', { name: 'Status: Choose model' })).toHaveAttribute('href', '/settings/models')
  })
})

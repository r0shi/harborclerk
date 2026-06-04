import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
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
  loaded: true,
}

interface TestStatusSummary {
  state: 'ready' | 'processing' | 'needs_attention'
  counts: {
    processing_documents: number
    queued_jobs: number
    running_jobs: number
    failed_documents: number
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
    queued_jobs: 0,
    running_jobs: 0,
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

  it('does not request the admin-only summary for non-admin users', () => {
    mockPill({ isAdmin: false, llmState: 'deactivated' })

    renderPill()

    expect(getMock).not.toHaveBeenCalledWith('/api/system/status-summary')
    expect(screen.getByRole('link', { name: 'Status: Choose model' })).toHaveAttribute('href', '/settings/models')
  })
})

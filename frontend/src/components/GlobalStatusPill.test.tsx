import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useSystemConfig, type SystemConfig } from '../hooks/useSystemConfig'
import { useLLMStatusContext } from './LLMStatusBanner'
import GlobalStatusPill from './GlobalStatusPill'

vi.mock('../hooks/useSystemConfig', () => ({
  useSystemConfig: vi.fn(),
}))

vi.mock('./LLMStatusBanner', () => ({
  useLLMStatusContext: vi.fn(),
}))

const useSystemConfigMock = vi.mocked(useSystemConfig)
const useLLMStatusContextMock = vi.mocked(useLLMStatusContext)

const READY_CONFIG: SystemConfig = {
  healthStatus: 'healthy',
  allowSourceDownload: false,
  enableCliAccess: false,
  cliShimInstallStatus: null,
  loaded: true,
}

function mockPill({
  config = READY_CONFIG,
  llmState = 'ready',
}: {
  config?: SystemConfig
  llmState?: 'deactivated' | 'loading' | 'ready' | 'unknown'
}) {
  useSystemConfigMock.mockReturnValue(config)
  useLLMStatusContextMock.mockReturnValue({
    status: {
      state: llmState,
      model_id: llmState === 'ready' ? 'qwen3' : null,
      model_name: llmState === 'ready' ? 'Qwen3' : null,
    },
    markTransitioning: vi.fn(),
  })
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
    useSystemConfigMock.mockReset()
    useLLMStatusContextMock.mockReset()
  })

  it('links degraded system health to Status', () => {
    mockPill({ config: { ...READY_CONFIG, healthStatus: 'degraded' } })

    renderPill()

    const link = screen.getByRole('link', { name: 'Status: Needs attention' })
    expect(link).toHaveAttribute('href', '/settings/status')
    expect(screen.getByText('Needs attention')).toBeInTheDocument()
  })

  it('links inactive local AI to Models', () => {
    mockPill({ llmState: 'deactivated' })

    renderPill()

    const link = screen.getByRole('link', { name: 'Status: Choose model' })
    expect(link).toHaveAttribute('href', '/settings/models')
  })

  it('shows ready when system health and local AI are ready', () => {
    mockPill({})

    renderPill()

    const link = screen.getByRole('link', { name: 'Status: Ready' })
    expect(link).toHaveAttribute('href', '/settings/status')
  })
})

import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useLLMStatus, type LLMStatus } from '../hooks/useLLMStatus'
import { LLMStatusBanner, LLMStatusProvider } from './LLMStatusBanner'

vi.mock('../hooks/useLLMStatus', () => ({ useLLMStatus: vi.fn() }))

const useLLMStatusMock = vi.mocked(useLLMStatus)

function renderWith(status: LLMStatus) {
  useLLMStatusMock.mockReturnValue({ status, markTransitioning: vi.fn() })
  render(
    <MemoryRouter>
      <LLMStatusProvider>
        <LLMStatusBanner />
      </LLMStatusProvider>
    </MemoryRouter>,
  )
}

describe('LLMStatusBanner when the active model does not fit this Mac (#709)', () => {
  beforeEach(() => {
    useLLMStatusMock.mockReset()
  })

  it('says why loading never ends, with the Models page figures, and links to the Models page', () => {
    renderWith({
      state: 'loading',
      model_id: 'qwen36-35b-a3b',
      model_name: 'Qwen3.6 35B-A3B',
      does_not_fit_here: { system_ram_gb: 16, min_ram_gb: 48 },
    })
    const banner = screen.getByRole('status')
    expect(banner.textContent).toContain('Model not started')
    expect(banner.textContent).toContain('Qwen3.6 35B-A3B does not fit this Mac (16 GB): needs about 48 GB.')
    expect(banner.textContent).not.toContain('Loading model')
    expect(screen.getByRole('link', { name: 'Pick a model that fits' })).toHaveAttribute('href', '/models')
  })

  it('keeps the loading spinner for a model this Mac holds', () => {
    renderWith({ state: 'loading', model_id: 'qwen35-4b', model_name: 'Qwen3.5 4B', does_not_fit_here: null })
    expect(screen.getByRole('status').textContent).toContain('Loading model')
    expect(screen.queryByText('Model not started')).not.toBeInTheDocument()
  })

  it('shows nothing once the server is ready, whatever the field says', () => {
    renderWith({
      state: 'ready',
      model_id: 'qwen35-4b',
      model_name: 'Qwen3.5 4B',
      does_not_fit_here: { system_ram_gb: 16, min_ram_gb: 48 },
    })
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })
})

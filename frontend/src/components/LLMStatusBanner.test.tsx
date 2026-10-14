import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useLLMStatus, type LLMStatus } from '../hooks/useLLMStatus'
import { LLMStatusBanner, LLMStatusProvider } from './LLMStatusBanner'

vi.mock('../hooks/useLLMStatus', () => ({ useLLMStatus: vi.fn() }))

const useLLMStatusMock = vi.mocked(useLLMStatus)

// A fresh element each time: React skips re-rendering a subtree handed the identical element object, and a
// `rerender` that reused one would never re-read the mocked hook.
const tree = () => (
  <MemoryRouter>
    <LLMStatusProvider>
      <LLMStatusBanner />
    </LLMStatusProvider>
  </MemoryRouter>
)

function renderWith(status: LLMStatus) {
  useLLMStatusMock.mockReturnValue({ status, markTransitioning: vi.fn() })
  return render(tree())
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

  it('flashes the ready check mark, not the warning, when a loading model comes up while the field is stale', () => {
    // The field is computed per poll; one stale answer can arrive with `ready`. The warning is for a server
    // that is not loading, so it is tied to `loading`, and the loading→ready flash keeps its own words.
    const unfit = { system_ram_gb: 16, min_ram_gb: 48 }
    const { rerender } = renderWith({
      state: 'loading',
      model_id: 'qwen36-35b-a3b',
      model_name: 'Qwen3.6 35B-A3B',
      does_not_fit_here: unfit,
    })
    expect(screen.getByRole('status').textContent).toContain('Model not started')

    useLLMStatusMock.mockReturnValue({
      status: { state: 'ready', model_id: 'qwen36-35b-a3b', model_name: 'Qwen3.6 35B-A3B', does_not_fit_here: unfit },
      markTransitioning: vi.fn(),
    })
    rerender(tree())

    const banner = screen.getByRole('status')
    expect(banner.textContent).toContain('Model ready')
    expect(banner.textContent).not.toContain('Model not started')
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

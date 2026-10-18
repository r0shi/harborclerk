import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { get } from '../api'
import { useOptionalLLMStatus } from './LLMStatusBanner'
import { LocalAISetupPrompt } from './LocalAISetupPrompt'
import OnboardingWizard from './OnboardingWizard'

vi.mock('./LLMStatusBanner', () => ({ useOptionalLLMStatus: vi.fn() }))
vi.mock('../api', () => ({
  ApiError: class ApiError extends Error {},
  get: vi.fn(),
  patch: vi.fn(),
  post: vi.fn(),
}))

const useOptionalLLMStatusMock = vi.mocked(useOptionalLLMStatus)
const getMock = vi.mocked(get)

// Under Docker Compose the model is LLM_MODEL_FILE, active once downloaded (#654): the setup step is the
// server's own advice, never "Download a model that fits this Mac. Activate it."
const HINT = 'No local model is loaded yet. Download Qwen3.5-4B-Q4_K_M.gguf in Settings → Models.'
const compose = {
  state: 'deactivated' as const,
  model_id: null,
  model_name: null,
  setup_hint: HINT,
  compose_model_file: 'Qwen3.5-4B-Q4_K_M.gguf',
}

function renderPrompt(variant: 'ask' | 'research') {
  render(
    <MemoryRouter>
      <LocalAISetupPrompt variant={variant} />
    </MemoryRouter>,
  )
}

function renderWizardAtModels(platform: 'macos' | 'docker') {
  getMock.mockImplementation((path: string) => {
    if (path === '/api/watch/system') {
      return Promise.resolve({ platform, picker: platform === 'docker' ? 'none' : 'native', watch_root: null })
    }
    if (path === '/api/languages') {
      return Promise.resolve({
        languages: [{ code: 'en', display_name: 'English', built_in: true, enabled: true, tools: {} }],
      })
    }
    return Promise.reject(new Error(`Unexpected path ${path}`))
  })
  render(
    <MemoryRouter initialEntries={['/']}>
      <Routes>
        <Route path="/" element={<OnboardingWizard onComplete={vi.fn()} />} />
      </Routes>
    </MemoryRouter>,
  )
  fireEvent.click(screen.getByRole('button', { name: 'Next →' }))
  fireEvent.click(screen.getByRole('button', { name: 'Skip' }))
}

describe('local AI setup under Docker Compose (#654)', () => {
  beforeEach(() => {
    useOptionalLLMStatusMock.mockReset()
    getMock.mockReset()
  })

  it.each(['ask', 'research'] as const)('the %s empty state shows the server’s setup hint', (variant) => {
    useOptionalLLMStatusMock.mockReturnValue(compose)
    renderPrompt(variant)
    expect(screen.getByText(HINT)).toBeInTheDocument()
    expect(screen.queryByText(/fits this Mac/)).not.toBeInTheDocument()
    expect(screen.queryByText(/Activate it/)).not.toBeInTheDocument()
  })

  it('outside Compose the empty state keeps the Mac setup path', () => {
    useOptionalLLMStatusMock.mockReturnValue(null)
    renderPrompt('ask')
    expect(screen.getByText('Download a model that fits this Mac.')).toBeInTheDocument()
    expect(screen.getByText('Activate it when the download completes.')).toBeInTheDocument()
  })

  it('onboarding’s local AI step shows the server’s setup hint', () => {
    useOptionalLLMStatusMock.mockReturnValue(compose)
    renderWizardAtModels('docker')
    expect(screen.getByText(HINT)).toBeInTheDocument()
    expect(screen.queryByText(/fits this Mac/)).not.toBeInTheDocument()
  })

  it('onboarding on Docker before the status says, still does not say Activate', async () => {
    useOptionalLLMStatusMock.mockReturnValue(null)
    renderWizardAtModels('docker')
    expect(await screen.findByText(/Download the model LLM_MODEL_FILE in \.env names/)).toBeInTheDocument()
    expect(screen.queryByText(/Activate it/)).not.toBeInTheDocument()
  })

  it('onboarding on a Mac keeps the Mac setup path', () => {
    useOptionalLLMStatusMock.mockReturnValue(null)
    renderWizardAtModels('macos')
    expect(screen.getByText('Download a model that fits this Mac.')).toBeInTheDocument()
  })
})

import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { get } from '../api'
import { useOptionalLLMStatus } from './LLMStatusBanner'
import { LocalAISetupPrompt } from './LocalAISetupPrompt'
import OnboardingWizard from './OnboardingWizard'
import { composeSetupStep } from '../hooks/useLLMStatus'

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

  it('onboarding on a Mac keeps the Mac setup path', async () => {
    useOptionalLLMStatusMock.mockReturnValue(null)
    renderWizardAtModels('macos')
    // The platform word arrives with /api/watch/system.
    expect(await screen.findByText('Download a model that fits this Mac.')).toBeInTheDocument()
  })

  it('onboarding on Docker once the status says it is not Compose keeps the Mac path', async () => {
    // WATCH_ROOT makes the platform "docker" in a source run or a plain `docker run` too, where Activate works.
    useOptionalLLMStatusMock.mockReturnValue({
      state: 'deactivated',
      model_id: null,
      model_name: null,
      compose_model_file: null,
    })
    renderWizardAtModels('docker')
    // The platform word arrives with /api/watch/system.
    expect(await screen.findByText('Download a model that fits this server.')).toBeInTheDocument()
    expect(screen.queryByText(/LLM_MODEL_FILE/)).not.toBeInTheDocument()
  })

  // The served model is there and loading, or crash-looping: no setup hint, so "download it" would be wrong.
  it.each([
    ['loading with the model id set', { state: 'loading' as const, model_id: 'qwen3.5-4b', model_name: 'Qwen3.5 4B' }],
    ['loading before the id is known', { state: 'loading' as const, model_id: null, model_name: null }],
    ['crash-looping with the model id set', { state: 'unknown' as const, model_id: 'qwen3.5-4b', model_name: null }],
  ])('the empty state, %s, points at the llama-server log', (_label, status) => {
    useOptionalLLMStatusMock.mockReturnValue({
      ...status,
      setup_hint: null,
      compose_model_file: 'Qwen3.5-4B-Q4_K_M.gguf',
    })
    renderPrompt('ask')
    expect(
      screen.getByText("The local model is loading; if this persists, check 'docker compose logs llama-server'."),
    ).toBeInTheDocument()
    expect(screen.queryByText(/download it/)).not.toBeInTheDocument()
  })

  it('with no model id, no hint and not loading, the empty state still says to download the file', () => {
    useOptionalLLMStatusMock.mockReturnValue({ ...compose, setup_hint: null })
    renderPrompt('ask')
    expect(screen.getByText(/\(Qwen3\.5-4B-Q4_K_M\.gguf\); download it in Settings → Models\./)).toBeInTheDocument()
    expect(screen.queryByText(/is loading/)).not.toBeInTheDocument()
  })

  it('composeSetupStep stands in for the status on Docker only until the status is known', () => {
    expect(composeSetupStep(null, 'docker')).toMatch(/LLM_MODEL_FILE/)
    expect(composeSetupStep({ state: 'unknown', model_id: null, model_name: null }, 'docker')).toMatch(/LLM_MODEL_FILE/)
    expect(
      composeSetupStep({ state: 'ready', model_id: 'x', model_name: 'X', compose_model_file: null }, 'docker'),
    ).toBeNull()
    expect(composeSetupStep(null, 'macos')).toBeNull()
  })
})

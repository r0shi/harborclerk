import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { get } from '../api'
import { useAuth } from '../auth'
import { useLLMStatusContext } from '../components/LLMStatusBanner'
import ModelsPage from './ModelsPage'

vi.mock('../api', () => ({ get: vi.fn(), post: vi.fn(), put: vi.fn(), del: vi.fn() }))
vi.mock('../auth', () => ({ useAuth: vi.fn() }))
vi.mock('../components/LLMStatusBanner', () => ({ useLLMStatusContext: vi.fn() }))

const getMock = vi.mocked(get)
const useAuthMock = vi.mocked(useAuth)
const useLLMStatusContextMock = vi.mocked(useLLMStatusContext)

// Under Docker Compose the served model is LLM_MODEL_FILE (#654): the server runs every model at its own
// context, nothing activates one, and RoPE scaling is not applied.
const model = {
  size_bytes: 2_740_937_888,
  context_window: 262144,
  supports_tools: true,
  supports_research: true,
  downloading: false,
  superseded_by: null,
  memory_bytes: 4_000_000_000,
  min_ram_gb: 8,
  max_context_here: 8192,
  fits_here: true,
  system_ram_gb: 2,
}
const models = [
  {
    ...model,
    id: 'qwen35-4b',
    name: 'Qwen3.5 4B',
    downloaded: true,
    active: true,
    yarn_available: false,
    yarn_extended_context: null,
  },
  {
    ...model,
    id: 'qwen3-8b',
    name: 'Qwen3 8B',
    context_window: 32768,
    downloaded: true,
    active: false,
    yarn_available: true,
    yarn_extended_context: 131072,
  },
]

function answer(url: string): unknown {
  if (url === '/api/chat/models') return models
  if (url === '/api/chat/models/yarn') return { yarn_enabled: true }
  if (url === '/api/chat/models/summary-afm') return { summary_force_apple_intelligence: false }
  if (url === '/api/chat/models/orphaned') return []
  throw new Error(`unexpected GET ${url}`)
}

function renderWith(composeModelFile: string | null) {
  useLLMStatusContextMock.mockReturnValue({
    status: {
      state: 'ready',
      model_id: 'qwen35-4b',
      model_name: 'Qwen3.5 4B',
      compose_model_file: composeModelFile,
    },
    markTransitioning: vi.fn(),
  })
  render(
    <MemoryRouter>
      <ModelsPage />
    </MemoryRouter>,
  )
}

describe('ModelsPage under Docker Compose (#654)', () => {
  beforeEach(() => {
    getMock.mockReset()
    getMock.mockImplementation(async (url: string) => answer(url) as never)
    useAuthMock.mockReturnValue({ token: 't' } as never)
  })

  it('says LLM_MODEL_FILE chooses the model and offers no Activate, Deactivate or YaRN', async () => {
    renderWith('Qwen3.5-4B-Q4_K_M.gguf')
    expect(
      await screen.findByText('The served model is chosen by LLM_MODEL_FILE in .env: Qwen3.5-4B-Q4_K_M.gguf.'),
    ).toBeInTheDocument()
    await screen.findByText('Qwen3 8B')
    expect(screen.queryByRole('button', { name: 'Activate' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Deactivate' })).not.toBeInTheDocument()
    expect(screen.queryByText('Extended Context (YaRN)')).not.toBeInTheDocument()
    expect(screen.queryByText(/w\/ YaRN|via YaRN/)).not.toBeInTheDocument()
    expect(screen.queryByText('Download, then activate one model.')).not.toBeInTheDocument()
  })

  it('says so when LLM_MODEL_FILE is empty', async () => {
    renderWith('')
    expect(
      await screen.findByText(
        'The served model is chosen by LLM_MODEL_FILE in .env, which is empty: no local model runs.',
      ),
    ).toBeInTheDocument()
  })

  it('outside Compose keeps Activate and YaRN', async () => {
    renderWith(null)
    await screen.findByText('Qwen3 8B')
    expect(screen.getByRole('button', { name: 'Activate' })).toBeInTheDocument()
    expect(screen.getByText('Extended Context (YaRN)')).toBeInTheDocument()
    expect(screen.getByText('Download, then activate one model.')).toBeInTheDocument()
  })
})

import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { get } from '../api'
import { useAuth } from '../auth'
import { useLLMStatusContext } from '../components/LLMStatusBanner'
import type { LLMStatus } from '../hooks/useLLMStatus'
import ModelsPage from './ModelsPage'

vi.mock('../api', () => ({ get: vi.fn(), post: vi.fn(), put: vi.fn(), del: vi.fn() }))
vi.mock('../auth', () => ({ useAuth: vi.fn() }))
vi.mock('../components/LLMStatusBanner', () => ({ useLLMStatusContext: vi.fn() }))

const getMock = vi.mocked(get)
const useAuthMock = vi.mocked(useAuth)
const useLLMStatusContextMock = vi.mocked(useLLMStatusContext)

const FILE = 'Qwen3.5-4B-Q4_K_M.gguf'

// Under Docker Compose the served model is LLM_MODEL_FILE (#654): the server runs every model at its own context,
// nothing activates one, and RoPE scaling is not applied. The list itself says so (compose_model_file on every
// row), so the page knows before any status poll.
function rows(composeModelFile: string | null | undefined, { active = true, fits = true } = {}) {
  const base = {
    size_bytes: 2_740_937_888,
    supports_tools: true,
    supports_research: true,
    downloading: false,
    superseded_by: null,
    memory_bytes: 4_000_000_000,
    min_ram_gb: 8,
    max_context_here: 8192,
    fits_here: fits,
    system_ram_gb: 2,
    ...(composeModelFile === undefined ? {} : { compose_model_file: composeModelFile }),
  }
  return [
    {
      ...base,
      id: 'qwen35-4b',
      name: 'Qwen3.5 4B',
      context_window: 262144,
      downloaded: true,
      active,
      yarn_available: false,
      yarn_extended_context: null,
    },
    {
      ...base,
      id: 'qwen3-8b',
      name: 'Qwen3 8B',
      context_window: 32768,
      downloaded: true,
      active: false,
      yarn_available: true,
      yarn_extended_context: 131072,
    },
  ]
}

function renderWith(models: ReturnType<typeof rows>, status: Partial<LLMStatus> = { state: 'unknown' }) {
  getMock.mockImplementation(async (url: string) => {
    if (url === '/api/chat/models') return models as never
    if (url === '/api/chat/models/yarn') return { yarn_enabled: true } as never
    if (url === '/api/chat/models/summary-afm') return { summary_force_apple_intelligence: false } as never
    if (url === '/api/chat/models/orphaned') return [] as never
    throw new Error(`unexpected GET ${url}`)
  })
  useLLMStatusContextMock.mockReturnValue({
    status: { state: 'unknown', model_id: null, model_name: null, ...status } as LLMStatus,
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
    useAuthMock.mockReturnValue({ token: 't' } as never)
  })

  it('before any status poll, offers no Activate, Deactivate or YaRN, and names LLM_MODEL_FILE', async () => {
    renderWith(rows(FILE))
    expect(
      await screen.findByText(`The served model is chosen by LLM_MODEL_FILE in .env: ${FILE}.`),
    ).toBeInTheDocument()
    await screen.findByText('Qwen3 8B')
    expect(screen.queryByRole('button', { name: 'Activate' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Deactivate' })).not.toBeInTheDocument()
    expect(screen.queryByText('Extended Context (YaRN)')).not.toBeInTheDocument()
    expect(screen.queryByText(/w\/ YaRN|via YaRN/)).not.toBeInTheDocument()
    expect(screen.queryByText('Download, then activate one model.')).not.toBeInTheDocument()
  })

  it('with the served model running, says only how to switch', async () => {
    renderWith(rows(FILE, { active: true }), { state: 'ready', model_id: 'qwen35-4b', setup_hint: null })
    expect(await screen.findByText(/To use another model, set LLM_MODEL_FILE to its file and run/)).toBeInTheDocument()
    expect(screen.queryByText(/Download/)).not.toBeInTheDocument()
  })

  it('with the file not downloaded yet, passes on the server’s advice to download it', async () => {
    const hint = `No local model is loaded yet. Download ${FILE} in Settings → Models.`
    renderWith(rows(FILE, { active: false }), { state: 'deactivated', setup_hint: hint })
    expect(await screen.findByText(new RegExp(hint.replace(/[.]/g, '\\.')))).toBeInTheDocument()
  })

  it('with a file that is not a listed model, says to set it to one of theirs, not to download it', async () => {
    const hint =
      'LLM_MODEL_FILE my-finetune.gguf is not one of the models listed in Settings → Models; set it to one of their files in .env.'
    renderWith(rows('my-finetune.gguf', { active: false }), { state: 'deactivated', setup_hint: hint })
    expect(await screen.findByText(/is not one of the models listed in Settings → Models/)).toBeInTheDocument()
    // Not told to download a file the server already has (the missing-file advice).
    expect(screen.queryByText(/Download my-finetune\.gguf/)).not.toBeInTheDocument()
  })

  it('with LLM_MODEL_FILE empty, says no model runs and how to run one', async () => {
    renderWith(rows('', { active: false }))
    expect(
      await screen.findByText(
        'The served model is chosen by LLM_MODEL_FILE in .env, which is empty: no local model runs.',
      ),
    ).toBeInTheDocument()
    expect(screen.getByText(/To run one, set LLM_MODEL_FILE to a model's file below and run/)).toBeInTheDocument()
  })

  it('warns in Docker terms when a model may not fit the container', async () => {
    renderWith(rows(FILE, { fits: false }))
    await screen.findByText('Qwen3 8B')
    expect(screen.getAllByText("May not fit in this container's memory (2 GB): needs about 8 GB.").length).toBe(2)
    expect(screen.queryByText(/this Mac/)).not.toBeInTheDocument()
    expect(screen.queryByText(/will not start it here/)).not.toBeInTheDocument()
  })

  it('outside Compose keeps Activate and YaRN', async () => {
    renderWith(rows(undefined, { active: false }), { state: 'ready' })
    await screen.findByText('Qwen3 8B')
    expect(screen.getAllByRole('button', { name: 'Activate' }).length).toBe(2)
    expect(screen.getByText('Extended Context (YaRN)')).toBeInTheDocument()
    expect(screen.getByText('Download, then activate one model.')).toBeInTheDocument()
  })
})

import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { get } from '../api'
import { useAuth } from '../auth'
import { LLMStatusProvider } from '../components/LLMStatusBanner'
import ModelsPage from './ModelsPage'

vi.mock('../api', () => ({ get: vi.fn(), post: vi.fn(), put: vi.fn(), del: vi.fn() }))
vi.mock('../auth', () => ({ useAuth: vi.fn() }))

const getMock = vi.mocked(get)
const useAuthMock = vi.mocked(useAuth)

// A 16 GB Mac with YaRN on: one model fits, one fits only at a smaller context, one does not fit at all.
const models = [
  {
    id: 'qwen3-4b',
    name: 'Qwen3 4B',
    size_bytes: 2_497_280_256,
    context_window: 32768,
    supports_tools: true,
    supports_research: true,
    downloaded: true,
    active: false,
    downloading: false,
    yarn_available: false,
    yarn_extended_context: null,
    memory_bytes: 8_328_000_000,
    min_ram_gb: 16,
    max_context_here: 32768,
    fits_here: true,
    system_ram_gb: 16,
  },
  {
    id: 'qwen3-8b',
    name: 'Qwen3 8B',
    size_bytes: 5_027_783_488,
    context_window: 32768,
    supports_tools: true,
    supports_research: true,
    downloaded: true,
    active: false,
    downloading: false,
    yarn_available: true,
    yarn_extended_context: 131072,
    memory_bytes: 25_350_000_000,
    min_ram_gb: 36,
    max_context_here: 22528, // what the API gives a 16 GB Mac with YaRN on: clamped under even the plain window
    fits_here: false,
    system_ram_gb: 16,
  },
  {
    id: 'qwen36-35b-a3b',
    name: 'Qwen3.6 35B-A3B',
    size_bytes: 22_134_528_992,
    context_window: 262144,
    supports_tools: true,
    supports_research: true,
    downloaded: true,
    active: true,
    downloading: false,
    yarn_available: false,
    yarn_extended_context: null,
    memory_bytes: 28_800_000_000,
    min_ram_gb: 36,
    max_context_here: 0,
    fits_here: false,
    system_ram_gb: 16,
  },
]

function answer(url: string): unknown {
  if (url === '/api/chat/models') return models
  if (url === '/api/chat/models/yarn') return { yarn_enabled: true }
  if (url === '/api/chat/models/summary-afm') return { summary_force_apple_intelligence: false }
  if (url === '/api/chat/models/orphaned') return []
  throw new Error(`unexpected GET ${url}`)
}

describe('ModelsPage memory budget (#556)', () => {
  beforeEach(() => {
    getMock.mockReset()
    getMock.mockImplementation(async (url: string) => answer(url) as never)
    useAuthMock.mockReturnValue({ token: 't' } as never)
  })

  async function renderPage() {
    render(
      <MemoryRouter>
        <LLMStatusProvider>
          <ModelsPage />
        </LLMStatusProvider>
      </MemoryRouter>,
    )
    await screen.findByText('Qwen3 4B')
  }

  it('groups models by the Mac they need and says what each needs to run', async () => {
    await renderPage()
    expect(screen.getByText('For Macs with 16 GB or more')).toBeInTheDocument()
    expect(screen.getByText('For Macs with 36 GB or more')).toBeInTheDocument()
    expect(screen.getByText('8.3 GB to run')).toBeInTheDocument()
  })

  it('offers Activate only for a model this Mac can hold, and says why not otherwise', async () => {
    await renderPage()
    const rows = screen.getAllByRole('row')
    const row = (name: string) => rows.find((r) => r.textContent?.includes(name))!
    expect(row('Qwen3 4B').textContent).toContain('Activate')
    expect(row('Qwen3 8B').textContent).toContain('Activate')
    expect(row('Qwen3.6 35B-A3B').textContent).not.toContain('Activate')
    expect(row('Qwen3.6 35B-A3B').textContent).toContain('Does not fit this Mac (16 GB): needs about 36 GB.')
    expect(row('Qwen3.6 35B-A3B').textContent).toContain('the LLM server will not start it here')
  })

  it('marks a clamped context against the window the launcher will ask for, YaRN included', async () => {
    await renderPage()
    const rows = screen.getAllByRole('row')
    const q8 = rows.find((r) => r.textContent?.includes('Qwen3 8B'))!
    expect(q8.textContent).toContain('(22,528 here)')
    expect(q8.textContent).toContain('Fits this Mac (16 GB) at up to 22,528 tokens')
    const q4 = rows.find((r) => r.textContent?.includes('Qwen3 4B'))!
    expect(q4.textContent).not.toContain('here)')
  })
})

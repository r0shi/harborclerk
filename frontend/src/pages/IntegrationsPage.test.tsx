import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { del, get, put } from '../api'
import IntegrationsPage from './IntegrationsPage'

vi.mock('../api', () => ({
  del: vi.fn(),
  get: vi.fn(),
  put: vi.fn(),
}))

vi.mock('../hooks/useSystemConfig', () => ({
  useSystemConfig: () => ({
    enableCliAccess: true,
    cliShimInstallStatus: 'installed',
  }),
}))

const getMock = vi.mocked(get)
const putMock = vi.mocked(put)
const delMock = vi.mocked(del)

function renderPage() {
  render(
    <MemoryRouter>
      <IntegrationsPage />
    </MemoryRouter>,
  )
}

function expectPreContains(text: string) {
  expect(
    screen.getByText((content, element) => {
      return element?.tagName.toLowerCase() === 'pre' && content.includes(text)
    }),
  ).toBeInTheDocument()
}

describe('IntegrationsPage', () => {
  beforeEach(() => {
    getMock.mockReset()
    putMock.mockReset()
    delMock.mockReset()
    getMock.mockImplementation((path: string) => {
      if (path === '/api/integrations/settings') {
        return Promise.resolve({ public_url: '', oauth_refresh_token_days: 90 })
      }
      if (path === '/api/integrations/connections') {
        return Promise.resolve([])
      }
      return Promise.reject(new Error(`Unexpected path ${path}`))
    })
  })

  it('surfaces Codex and OpenClaw CLI-first setup guidance', async () => {
    renderPage()

    expect(await screen.findByText('Choose a Surface')).toBeInTheDocument()
    expect(screen.getByText('Data boundary')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /Codex/ }))

    expect(screen.getByText(/Codex works best with Harbor Clerk as a local CLI skill/)).toBeInTheDocument()
    expect(screen.getByText(/export HARBOR_CLERK_URL=/)).toBeInTheDocument()
    expect(screen.getByText(/harbor-clerk search "contract renewal" --json/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /OpenClaw/ }))

    expect(screen.getByText(/Prefer the CLI skill for local/)).toBeInTheDocument()
    expect(screen.getByText(/Full only for local runs/)).toBeInTheDocument()
  })

  it('renders the checked-in CLI skill markdown with find-all guidance', async () => {
    renderPage()

    expect(await screen.findByText('Skill markdown')).toBeInTheDocument()
    expect(screen.getByText(/Find every matching document/)).toBeInTheDocument()
    expect(screen.getByText(/harbor-clerk find-all/)).toBeInTheDocument()
  })

  it('renders authless MCP examples with token-path URLs', async () => {
    getMock.mockImplementation((path: string) => {
      if (path === '/api/integrations/settings') {
        return Promise.resolve({ public_url: 'https://clerk.example/', oauth_refresh_token_days: 90 })
      }
      if (path === '/api/integrations/connections') {
        return Promise.resolve([])
      }
      return Promise.reject(new Error(`Unexpected path ${path}`))
    })

    renderPage()

    expect(await screen.findByText('Choose a Surface')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /Claude/ }))
    expectPreContains('"url": "https://clerk.example/t/YOUR_API_KEY"')
    expectPreContains('claude mcp add harbor-clerk "https://clerk.example/t/YOUR_API_KEY"')

    fireEvent.click(screen.getByRole('button', { name: /Gemini/ }))
    expectPreContains('"uri": "https://clerk.example/t/YOUR_API_KEY"')

    fireEvent.click(screen.getByRole('button', { name: /OpenClaw/ }))
    expectPreContains(`openclaw mcp set harbor-clerk '{"url":"https://clerk.example/t/YOUR_API_KEY"}'`)
    expectPreContains('"url": "https://clerk.example/t/YOUR_API_KEY"')
  })
})

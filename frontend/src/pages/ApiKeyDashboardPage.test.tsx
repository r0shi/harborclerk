import { render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { del, get } from '../api'
import ApiKeyDashboardPage from './ApiKeyDashboardPage'

vi.mock('../api', () => ({
  del: vi.fn(),
  get: vi.fn(),
}))

const getMock = vi.mocked(get)

const zeros = Object.fromEntries(['1h', '6h', '12h', '24h', '7d', '30d'].map((r) => [r, 0]))
const EMPTY_USAGE = {
  requests: zeros,
  errors: zeros,
  denials: zeros,
  rate_limited: zeros,
  last_used_at: null,
  top_tools: {},
}

function stub(expiresAt: string | null) {
  getMock.mockImplementation((path: string) => {
    if (path === '/api/api-keys')
      return Promise.resolve([
        {
          key_id: 'k1',
          name: 'Cloud connector',
          is_active: true,
          permission_tier: 'read',
          scope_summary: 'Read only, 1 topic',
          expires_at: expiresAt,
        },
      ])
    if (path === '/api/api-keys/k1/usage') return Promise.resolve(EMPTY_USAGE)
    if (path === '/api/api-keys/k1/usage/timeline') return Promise.resolve([])
    if (path === '/api/api-keys/k1/usage/requests')
      return Promise.resolve({ items: [], total: 0, page: 1, page_size: 25 })
    return Promise.reject(new Error(`Unexpected path ${path}`))
  })
}

function renderDashboard() {
  render(
    <MemoryRouter initialEntries={['/settings/api-keys/k1']}>
      <Routes>
        <Route path="/settings/api-keys/:keyId" element={<ApiKeyDashboardPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

// #833: the server's scope_summary no longer carries the (UTC) expiry date; the dashboard
// appends the expiry itself, in local time, as the API Keys table shows it.
describe('ApiKeyDashboardPage expiry subtitle', () => {
  beforeEach(() => {
    getMock.mockReset()
    vi.mocked(del).mockReset()
    vi.stubEnv('TZ', 'Pacific/Auckland') // UTC+13 in January
  })

  afterEach(() => {
    vi.unstubAllEnvs()
  })

  it('appends the local expiry date to the scope summary', async () => {
    // 2027-01-15T23:59:59Z is 2027-01-16 in Auckland; the old server summary said 2027-01-15.
    stub('2027-01-15T23:59:59Z')
    renderDashboard()
    expect(
      await screen.findByText(`Read only, 1 topic · expires ${new Date(2027, 0, 16).toLocaleDateString()}`),
    ).toBeInTheDocument()
  })

  it('shows the scope summary alone for a key that never expires', async () => {
    stub(null)
    renderDashboard()
    expect(await screen.findByText('Read only, 1 topic')).toBeInTheDocument()
    expect(screen.queryByText(/expires/)).not.toBeInTheDocument()
  })
})

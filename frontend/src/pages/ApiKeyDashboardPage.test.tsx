import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useNavigate } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { del, get } from '../api'
import { endOfLocalDayIso } from '../utils/apiKeyForm'
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
        {
          key_id: 'k2',
          name: 'Desktop client',
          is_active: true,
          permission_tier: 'full',
          scope_summary: 'Full access',
          expires_at: null,
        },
      ])
    if (/^\/api\/api-keys\/k[12]\/usage$/.test(path)) return Promise.resolve(EMPTY_USAGE)
    if (/^\/api\/api-keys\/k[12]\/usage\/timeline$/.test(path)) return Promise.resolve([])
    if (/^\/api\/api-keys\/k[12]\/usage\/requests$/.test(path))
      return Promise.resolve({ items: [], total: 0, page: 1, page_size: 25 })
    return Promise.reject(new Error(`Unexpected path ${path}`))
  })
}

// Stands in for anything that changes the key id on the same route (history, a typed URL).
function GoToOtherKey() {
  const navigate = useNavigate()
  return <button onClick={() => navigate('/settings/api-keys/k2')}>Go to k2</button>
}

function renderDashboard() {
  render(
    <MemoryRouter initialEntries={['/settings/api-keys/k1']}>
      <GoToOtherKey />
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
    stub(endOfLocalDayIso('2027-01-16')) // 2027-01-16T10:59:59.000Z: the server's UTC date would say the 16th too
    renderDashboard()
    expect(
      await screen.findByText(`Read only, 1 topic · expires ${new Date(2027, 0, 16).toLocaleDateString()}`),
    ).toBeInTheDocument()
  })

  it('names the local time for an expiry that is not the end of a local day', async () => {
    // A key saved by the old UI: 2027-01-15T23:59:59Z is 12:59 PM on the 16th in Auckland.
    stub('2027-01-15T23:59:59Z')
    renderDashboard()
    const moment = new Date(2027, 0, 16, 12, 59).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
    expect(await screen.findByText(`Read only, 1 topic · expires ${moment}`)).toBeInTheDocument()
  })

  it('shows the scope summary alone for a key that never expires', async () => {
    stub(null)
    renderDashboard()
    expect(await screen.findByText('Read only, 1 topic')).toBeInTheDocument()
    expect(screen.queryByText(/expires/)).not.toBeInTheDocument()
  })
})

describe('ApiKeyDashboardPage purge confirmation (#833)', () => {
  beforeEach(() => {
    getMock.mockReset()
    vi.mocked(del).mockReset()
  })

  it("does not carry an armed Purge Events to another key's dashboard", async () => {
    stub(null)
    renderDashboard()
    fireEvent.click(await screen.findByRole('button', { name: 'Purge Events' }))
    expect(screen.getByText('Delete all logged events?')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Go to k2' }))
    expect(await screen.findByText('Desktop client')).toBeInTheDocument()
    expect(screen.queryByText('Delete all logged events?')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Purge Events' })).toBeInTheDocument()
    expect(vi.mocked(del)).not.toHaveBeenCalled()
  })
})

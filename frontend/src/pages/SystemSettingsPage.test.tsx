import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useAuth } from '../auth'
import SystemSettingsPage from './SystemSettingsPage'

vi.mock('../auth', () => ({
  useAuth: vi.fn(),
}))

const useAuthMock = vi.mocked(useAuth)

function mockAuth(isAdmin: boolean) {
  useAuthMock.mockReturnValue({
    user: {
      user_id: 'user-1',
      email: isAdmin ? 'admin@example.com' : 'user@example.com',
      role: isAdmin ? 'admin' : 'user',
      preferences: {},
    },
    token: 'test-token',
    loading: false,
    needsSetup: false,
    login: vi.fn(),
    logout: vi.fn(),
    updatePreferences: vi.fn(),
    isAdmin,
  } as ReturnType<typeof useAuth>)
}

function renderSettings() {
  render(
    <MemoryRouter>
      <SystemSettingsPage />
    </MemoryRouter>,
  )
}

describe('SystemSettingsPage', () => {
  beforeEach(() => {
    useAuthMock.mockReset()
  })

  it('shows the full settings hub for admins', () => {
    mockAuth(true)

    renderSettings()

    expect(screen.getByRole('heading', { name: 'Settings' })).toBeInTheDocument()
    expect(screen.getAllByText('Integrations').length).toBeGreaterThan(0)
    expect(screen.getByText('API Keys')).toBeInTheDocument()
    expect(screen.getByText('Models')).toBeInTheDocument()
    expect(screen.getByText('Status')).toBeInTheDocument()
    expect(screen.getByText('Diagnostics')).toBeInTheDocument()
  })

  it('keeps non-admin settings focused on preferences', () => {
    mockAuth(false)

    renderSettings()

    expect(screen.getByText('Preferences')).toBeInTheDocument()
    expect(screen.getByText('Admin-only settings are hidden for this account.')).toBeInTheDocument()
    expect(screen.queryByText('API Keys')).not.toBeInTheDocument()
    expect(screen.queryByText('Models')).not.toBeInTheDocument()
  })
})

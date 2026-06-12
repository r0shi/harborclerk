import { render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useAuth } from '../auth'
import SettingsLayout from './SettingsLayout'

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

function renderSettingsLayout(path = '/settings') {
  render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/settings" element={<SettingsLayout />}>
          <Route index element={<div>Settings hub</div>} />
          <Route path="models" element={<div>Models child</div>} />
        </Route>
      </Routes>
    </MemoryRouter>,
  )
}

describe('SettingsLayout', () => {
  beforeEach(() => {
    useAuthMock.mockReset()
  })

  it('shows admin settings sections and renders the child route', () => {
    mockAuth(true)

    renderSettingsLayout('/settings/models')

    expect(screen.getByText('Models child')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Models' })).toHaveAttribute('href', '/settings/models')
    expect(screen.getByRole('link', { name: 'API Keys' })).toHaveAttribute('href', '/settings/api-keys')
    expect(screen.getByRole('link', { name: 'Diagnostics' })).toHaveAttribute('href', '/settings/diagnostics')
  })

  it('hides admin-only settings links for non-admin users', () => {
    mockAuth(false)

    renderSettingsLayout()

    expect(screen.getByText('Settings hub')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Preferences' })).toHaveAttribute('href', '/settings/preferences')
    expect(screen.queryByRole('link', { name: 'API Keys' })).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Models' })).not.toBeInTheDocument()
  })
})

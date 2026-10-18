import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { get } from '../api'
import OnboardingWizard from './OnboardingWizard'
import { WATCHED_FOLDERS_DOCKER_GUIDE_URL } from '../data/docsLinks'

vi.mock('../api', () => ({
  ApiError: class ApiError extends Error {},
  get: vi.fn(),
  patch: vi.fn(),
  post: vi.fn(),
}))

const getMock = vi.mocked(get)

function renderWizard(onComplete = vi.fn()) {
  render(
    <MemoryRouter initialEntries={['/']}>
      <Routes>
        <Route path="/" element={<OnboardingWizard onComplete={onComplete} />} />
        <Route path="/settings/models" element={<div>Models route</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('OnboardingWizard', () => {
  beforeEach(() => {
    getMock.mockReset()
    getMock.mockImplementation((path: string) => {
      if (path === '/api/watch/system') {
        return Promise.resolve({ platform: 'macos', picker: 'native', watch_root: null })
      }
      if (path === '/api/languages') {
        return Promise.resolve({
          languages: [{ code: 'en', display_name: 'English', built_in: true, enabled: true, tools: {} }],
        })
      }
      return Promise.reject(new Error(`Unexpected path ${path}`))
    })
  })

  it('asks about local AI during startup and can route to Models', () => {
    const onComplete = vi.fn()

    renderWizard(onComplete)

    fireEvent.click(screen.getByRole('button', { name: 'Next →' }))
    fireEvent.click(screen.getByRole('button', { name: 'Skip' }))

    expect(screen.getByRole('heading', { name: 'Set up local AI (optional)' })).toBeInTheDocument()
    expect(screen.getByText(/Ask and Research need one downloaded and active local AI model/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Choose a model' }))

    expect(onComplete).toHaveBeenCalledOnce()
    expect(screen.getByText('Models route')).toBeInTheDocument()
  })

  it('on Docker, links the folder setup guide on GitHub in a new tab and drops the Mac-only copy', async () => {
    getMock.mockImplementation((path: string) => {
      if (path === '/api/watch/system') {
        return Promise.resolve({ platform: 'docker', picker: 'none', watch_root: '/data/watch' })
      }
      if (path === '/api/languages') return Promise.resolve({ languages: [] })
      return Promise.reject(new Error(`Unexpected path ${path}`))
    })

    renderWizard()

    const link = await screen.findByRole('link', { name: 'Read folder setup docs' })
    expect(link).toHaveAttribute('href', WATCHED_FOLDERS_DOCKER_GUIDE_URL)
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
    expect(screen.queryByRole('button', { name: 'Pick a folder to watch' })).not.toBeInTheDocument()
    expect(screen.queryByText(/on this Mac/)).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Next →' }))
    fireEvent.click(screen.getByRole('button', { name: 'Skip' }))
    fireEvent.click(screen.getByRole('button', { name: 'Skip' }))
    expect(screen.getByRole('heading', { name: 'Track ingestion progress' })).toBeInTheDocument()
    expect(screen.queryByText('Server menubar')).not.toBeInTheDocument()
  })
})

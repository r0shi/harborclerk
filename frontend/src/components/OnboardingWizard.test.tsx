import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { get } from '../api'
import { useSystemConfig, type SystemConfig } from '../hooks/useSystemConfig'
import OnboardingWizard from './OnboardingWizard'

vi.mock('../api', () => ({
  ApiError: class ApiError extends Error {},
  get: vi.fn(),
  patch: vi.fn(),
  post: vi.fn(),
}))

vi.mock('../hooks/useSystemConfig', () => ({ useSystemConfig: vi.fn() }))

const getMock = vi.mocked(get)
const useSystemConfigMock = vi.mocked(useSystemConfig)

function systemConfig(version: string | null): SystemConfig {
  return {
    healthStatus: 'healthy',
    allowSourceDownload: false,
    enableCliAccess: false,
    cliShimInstallStatus: null,
    localMcpUrl: null,
    version,
    loaded: true,
  }
}

function renderWizard(onComplete = vi.fn(), isAdmin = true) {
  render(
    <MemoryRouter initialEntries={['/']}>
      <Routes>
        <Route path="/" element={<OnboardingWizard onComplete={onComplete} isAdmin={isAdmin} />} />
        <Route path="/settings/models" element={<div>Models route</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('OnboardingWizard', () => {
  beforeEach(() => {
    useSystemConfigMock.mockReturnValue(systemConfig('0.9.3+unstamped'))
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

  it('a non-admin gets no folder picker on macOS, only a note (#852)', async () => {
    renderWizard(vi.fn(), false)

    expect(await screen.findByText('An administrator adds the folders Harbor Clerk watches.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Pick a folder to watch' })).not.toBeInTheDocument()
    expect(screen.queryByText(/pick a folder to watch/i)).not.toBeInTheDocument()
  })

  it('on macOS, shows the picker and the Mac copy on every page', async () => {
    renderWizard()

    expect(await screen.findByRole('button', { name: 'Pick a folder to watch' })).toBeInTheDocument()
    expect(screen.getByText(/Drop documents into a folder on this Mac/)).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Read folder setup docs' })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Next →' }))
    fireEvent.click(screen.getByRole('button', { name: 'Skip' }))
    expect(screen.getByText('Download a model that fits this Mac.')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Skip' }))
    expect(screen.getByText('Server menubar')).toBeInTheDocument()
    expect(screen.getByText(/Two places to watch:/)).toBeInTheDocument()
  })

  it('renders platform-neutral copy and a Retry when system info fails', async () => {
    let systemCalls = 0
    getMock.mockImplementation((path: string) => {
      if (path === '/api/languages') return Promise.resolve({ languages: [] })
      if (path === '/api/watch/system') systemCalls += 1
      return Promise.reject(new Error('unavailable'))
    })

    renderWizard()

    expect(await screen.findByRole('alert')).toHaveTextContent("Couldn't load folder setup (unavailable).")
    expect(screen.queryByRole('button', { name: 'Loading…' })).not.toBeInTheDocument()
    expect(screen.getByText(/indexes the documents in the folders it watches/)).toBeInTheDocument()
    expect(screen.queryByText(/on this Mac/)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Pick a folder to watch' })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByRole('alert')).toBeInTheDocument()
    expect(systemCalls).toBe(2)

    fireEvent.click(screen.getByRole('button', { name: 'Next →' }))
    fireEvent.click(screen.getByRole('button', { name: 'Skip' }))
    expect(screen.getByText('Download a model that fits this machine.')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Skip' }))
    expect(screen.queryByText('Server menubar')).not.toBeInTheDocument()
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
    expect(link).toHaveAttribute(
      'href',
      'https://github.com/r0shi/harborclerk/blob/main/docs/watched-folders-docker.md',
    )
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
    expect(screen.queryByRole('button', { name: 'Pick a folder to watch' })).not.toBeInTheDocument()
    expect(screen.queryByText(/on this Mac/)).not.toBeInTheDocument()
    expect(screen.getByText(/Mount a folder of documents under/)).toBeInTheDocument()
    expect(screen.getByText('/data/watch')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Next →' }))
    fireEvent.click(screen.getByRole('button', { name: 'Skip' }))
    // Before the LLM status says otherwise, Docker gets the Compose step (#654), not the Mac path.
    expect(
      screen.getByText('Download the model LLM_MODEL_FILE in .env names, in Settings → Models.'),
    ).toBeInTheDocument()
    expect(screen.queryByText(/fits this/)).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Skip' }))
    expect(screen.getByRole('heading', { name: 'Track ingestion progress' })).toBeInTheDocument()
    expect(screen.queryByText('Server menubar')).not.toBeInTheDocument()
  })

  it('retries system info and shows the platform once it answers', async () => {
    let fail = true
    getMock.mockImplementation((path: string) => {
      if (path === '/api/languages') return Promise.resolve({ languages: [] })
      if (path === '/api/watch/system') {
        return fail
          ? Promise.reject(new Error('unavailable'))
          : Promise.resolve({ platform: 'macos', picker: 'native', watch_root: null })
      }
      return Promise.reject(new Error(`Unexpected path ${path}`))
    })

    renderWizard()
    await screen.findByRole('alert')
    fail = false
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))

    expect(await screen.findByRole('button', { name: 'Pick a folder to watch' })).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('links the guide at the release tag on a release build', async () => {
    useSystemConfigMock.mockReturnValue(systemConfig('0.9.3'))
    getMock.mockImplementation((path: string) => {
      if (path === '/api/watch/system') {
        return Promise.resolve({ platform: 'docker', picker: 'none', watch_root: '/data/watch' })
      }
      if (path === '/api/languages') return Promise.resolve({ languages: [] })
      return Promise.reject(new Error(`Unexpected path ${path}`))
    })

    renderWizard()

    expect(await screen.findByRole('link', { name: 'Read folder setup docs' })).toHaveAttribute(
      'href',
      'https://github.com/r0shi/harborclerk/blob/v0.9.3/docs/watched-folders-docker.md',
    )
  })
})

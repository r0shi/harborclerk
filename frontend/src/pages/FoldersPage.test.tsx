import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { get } from '../api'
import { useSystemConfig, type SystemConfig } from '../hooks/useSystemConfig'
import FoldersPage from './FoldersPage'

vi.mock('../api', () => ({
  ApiError: class ApiError extends Error {},
  del: vi.fn(),
  get: vi.fn(),
  patch: vi.fn(),
  post: vi.fn(),
}))

const auth = vi.hoisted(() => ({ isAdmin: true }))
vi.mock('../auth', () => ({ useAuth: () => auth }))
vi.mock('../hooks/useFolderProgress', () => ({ useFolderProgress: () => {} }))
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

const MAIN_GUIDE = 'https://github.com/r0shi/harborclerk/blob/main/docs/watched-folders-docker.md'

const DOCKER_FOLDER = {
  folder_id: 'f1',
  path: '/data/watch/contracts',
  display_name: 'contracts',
  enabled: true,
  auto_discovered: true,
  unavailable_reason: null,
  skipped_count: 0,
  skipped_extensions: [],
}

const PROGRESS = {
  total_files: 3,
  completed_files: 2,
  by_stage: {},
  scan_status: 'idle',
  ingest_status: 'idle',
  last_scan_at: null,
}

function mockApi(folders: unknown[], system = { platform: 'docker', picker: 'none', watch_root: '/data/watch' }) {
  getMock.mockImplementation((path: string) => {
    if (path === '/api/watch/system') return Promise.resolve(system)
    if (path === '/api/watch/folders') return Promise.resolve(folders)
    if (path.endsWith('/progress')) return Promise.resolve(PROGRESS)
    return Promise.reject(new Error(`Unexpected path ${path}`))
  })
}

function renderPage() {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <FoldersPage />
    </QueryClientProvider>,
  )
}

describe('FoldersPage on Docker', () => {
  beforeEach(() => {
    auth.isAdmin = true
    getMock.mockReset()
    useSystemConfigMock.mockReturnValue(systemConfig('0.9.3+unstamped'))
  })

  it('links the mount guide on GitHub in a new tab, not an SPA /docs route', async () => {
    mockApi([DOCKER_FOLDER])
    renderPage()

    const link = await screen.findByRole('link', { name: /How to add a folder/ })
    expect(link).toHaveAttribute('href', MAIN_GUIDE)
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
  })

  it('links the guide at the release tag on a release build', async () => {
    useSystemConfigMock.mockReturnValue(systemConfig('0.9.3'))
    mockApi([DOCKER_FOLDER])
    renderPage()

    const link = await screen.findByRole('link', { name: /How to add a folder/ })
    expect(link).toHaveAttribute(
      'href',
      'https://github.com/r0shi/harborclerk/blob/v0.9.3/docs/watched-folders-docker.md',
    )
  })

  it('has no Add Folder button and keeps an active mount from being deleted', async () => {
    mockApi([DOCKER_FOLDER])
    renderPage()

    expect(await screen.findByText('/data/watch/contracts')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Add Folder/ })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Delete' })).toBeDisabled()
    expect(screen.getByText('auto-discovered')).toBeInTheDocument()
  })

  it('tells an empty Docker install to mount under the watch root', async () => {
    mockApi([])
    renderPage()

    expect(await screen.findByText('Mount a folder under /data/watch to begin.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Add Folder/ })).not.toBeInTheDocument()
  })

  it('shows Add Folder and no Docker banner on macOS', async () => {
    mockApi([], { platform: 'macos', picker: 'native', watch_root: null } as never)
    renderPage()

    expect(await screen.findAllByRole('button', { name: /Add Folder/ })).not.toHaveLength(0)
    expect(screen.queryByRole('link', { name: /How to add a folder/ })).not.toBeInTheDocument()
  })
})

const MAC_FOLDER = { ...DOCKER_FOLDER, path: '/Users/me/Documents', display_name: 'Documents', auto_discovered: false }
const MAC_SYSTEM = { platform: 'macos', picker: 'native', watch_root: null } as never

describe('FoldersPage for a non-admin (#852)', () => {
  beforeEach(() => {
    auth.isAdmin = false
    getMock.mockReset()
    useSystemConfigMock.mockReturnValue(systemConfig('0.9.3+unstamped'))
  })

  it('shows the folders but none of the management controls', async () => {
    mockApi([MAC_FOLDER], MAC_SYSTEM)
    renderPage()

    expect(await screen.findByText('/Users/me/Documents')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Add Folder/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Rename/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Disable|Enable/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Delete' })).not.toBeInTheDocument()
  })

  it('tells an empty install that an administrator adds folders, with no Add Folder button', async () => {
    mockApi([], MAC_SYSTEM)
    renderPage()

    expect(await screen.findByText('An administrator adds the folders Harbor Clerk watches.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Add Folder/ })).not.toBeInTheDocument()
  })

  it('an admin sees every control on the same folder', async () => {
    auth.isAdmin = true
    mockApi([MAC_FOLDER], MAC_SYSTEM)
    renderPage()

    expect(await screen.findByText('/Users/me/Documents')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Add Folder/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Rename/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Disable' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Delete' })).toBeEnabled()
  })
})

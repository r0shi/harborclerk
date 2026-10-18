import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { get } from '../api'
import { WATCHED_FOLDERS_DOCKER_GUIDE_URL } from '../data/docsLinks'
import FoldersPage from './FoldersPage'

vi.mock('../api', () => ({
  ApiError: class ApiError extends Error {},
  del: vi.fn(),
  get: vi.fn(),
  patch: vi.fn(),
  post: vi.fn(),
}))

vi.mock('../auth', () => ({ useAuth: () => ({ isAdmin: false }) }))
vi.mock('../hooks/useFolderProgress', () => ({ useFolderProgress: () => {} }))

const getMock = vi.mocked(get)

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
    getMock.mockReset()
  })

  it('links the mount guide on GitHub in a new tab, not an SPA /docs route', async () => {
    mockApi([DOCKER_FOLDER])
    renderPage()

    const link = await screen.findByRole('link', { name: /How to add a folder/ })
    expect(link).toHaveAttribute('href', WATCHED_FOLDERS_DOCKER_GUIDE_URL)
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
    expect(WATCHED_FOLDERS_DOCKER_GUIDE_URL).toMatch(/^https:\/\/github\.com\/.+\/watched-folders-docker\.md$/)
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

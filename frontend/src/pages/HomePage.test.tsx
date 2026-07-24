import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { get } from '../api'
import HomePage from './HomePage'

vi.mock('../api', () => ({
  get: vi.fn(),
}))

vi.mock('../auth', () => ({
  useAuth: () => ({ token: 'test-token' }),
}))

const getMock = vi.mocked(get)

function renderHome() {
  render(
    <MemoryRouter initialEntries={['/']}>
      <Routes>
        <Route path="/" element={<HomePage />} />
        <Route path="/folders" element={<div>Folders route</div>} />
        <Route path="/search" element={<div>Search route</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('HomePage', () => {
  beforeEach(() => {
    getMock.mockReset()
  })

  it('routes to folders when no watched folders exist', async () => {
    getMock.mockResolvedValueOnce([]).mockResolvedValueOnce({ total: 0 })

    renderHome()

    expect(await screen.findByText('Folders route')).toBeInTheDocument()
    expect(getMock).toHaveBeenCalledWith('/api/watch/folders')
    expect(getMock).toHaveBeenCalledWith('/api/docs', { limit: 0 })
  })

  it('routes to folders when watched folders have no documents', async () => {
    getMock.mockResolvedValueOnce([{ folder_id: 'folder-1' }]).mockResolvedValueOnce({ total: 0 })

    renderHome()

    expect(await screen.findByText('Folders route')).toBeInTheDocument()
  })

  it('routes to search when the corpus has documents', async () => {
    getMock.mockResolvedValueOnce([{ folder_id: 'folder-1' }]).mockResolvedValueOnce({ total: 3 })

    renderHome()

    expect(await screen.findByText('Search route')).toBeInTheDocument()
  })

  it('falls back to search on routing probe failure', async () => {
    getMock.mockRejectedValueOnce(new Error('network'))

    renderHome()

    await waitFor(() => expect(screen.getByText('Search route')).toBeInTheDocument())
  })
})

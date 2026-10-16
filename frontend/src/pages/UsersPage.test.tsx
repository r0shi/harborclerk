import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { del, get, patch, post } from '../api'
import UsersPage from './UsersPage'

vi.mock('../api', () => ({
  del: vi.fn(),
  get: vi.fn(),
  patch: vi.fn(),
  post: vi.fn(),
}))

const delMock = vi.mocked(del)
const getMock = vi.mocked(get)

const USER = {
  user_id: 'u1',
  email: 'reader@example.com',
  role: 'user',
  is_active: true,
  created_at: '2026-10-01T00:00:00Z',
}

describe('UsersPage delete (#833)', () => {
  beforeEach(() => {
    delMock.mockReset()
    getMock.mockReset()
    vi.mocked(patch).mockReset()
    vi.mocked(post).mockReset()
    delMock.mockResolvedValue(undefined)
    getMock.mockResolvedValue([USER])
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('deletes through the in-page confirm, even where window.confirm returns false (WKWebView)', async () => {
    const nativeConfirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    render(<UsersPage />)
    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }))
    expect(screen.getByText('Delete user reader@example.com?')).toBeInTheDocument()
    expect(delMock).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm delete' }))
    await waitFor(() => expect(delMock).toHaveBeenCalledWith('/api/users/u1'))
    expect(nativeConfirm).not.toHaveBeenCalled()
  })

  it('shows a failed delete on the page and clears the armed state', async () => {
    delMock.mockRejectedValue(new Error('Cannot delete the last admin'))
    render(<UsersPage />)
    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }))
    fireEvent.click(screen.getByRole('button', { name: 'Confirm delete' }))
    expect(await screen.findByText('Cannot delete the last admin')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Confirm delete' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Delete' })).toBeInTheDocument()
    expect(delMock).toHaveBeenCalledTimes(1)
  })

  it('sends nothing when the delete is cancelled', async () => {
    render(<UsersPage />)
    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('button', { name: 'Confirm delete' })).not.toBeInTheDocument()
    expect(delMock).not.toHaveBeenCalled()
  })
})

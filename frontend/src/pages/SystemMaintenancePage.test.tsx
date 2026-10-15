import { act, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { get, post } from '../api'
import SystemMaintenancePage from './SystemMaintenancePage'

vi.mock('../api', () => ({
  get: vi.fn(),
  post: vi.fn(),
}))

const getMock = vi.mocked(get)
const postMock = vi.mocked(post)

describe('SystemMaintenancePage — Run Migrations', () => {
  beforeEach(() => {
    getMock.mockReset()
    postMock.mockReset()
    getMock.mockResolvedValue({})
  })

  it('disables the button while a run is pending, so a second click cannot start a second run', async () => {
    let finish: (value: { schema_version: string }) => void = () => {}
    postMock.mockImplementation(
      () =>
        new Promise((resolve) => {
          finish = resolve as typeof finish
        }),
    )
    render(
      <MemoryRouter>
        <SystemMaintenancePage />
      </MemoryRouter>,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Run Migrations' }))
    const pending = screen.getByRole('button', { name: 'Running migrations…' })
    expect(pending).toBeDisabled()
    fireEvent.click(pending)
    expect(postMock).toHaveBeenCalledTimes(1)
    expect(postMock).toHaveBeenCalledWith('/api/system/run-migrations')

    await act(async () => {
      finish({ schema_version: '0009_fts_over_folded_text' })
    })
    expect(screen.getByRole('button', { name: 'Run Migrations' })).toBeEnabled()
    expect(screen.getByText('Migrations complete. Schema version: 0009_fts_over_folded_text')).toBeInTheDocument()
  })

  it('enables the button again after a failed run', async () => {
    postMock.mockRejectedValue(new Error('Migration timed out'))
    render(
      <MemoryRouter>
        <SystemMaintenancePage />
      </MemoryRouter>,
    )

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Run Migrations' }))
    })
    expect(screen.getByRole('button', { name: 'Run Migrations' })).toBeEnabled()
    expect(screen.getByText('Migration timed out')).toBeInTheDocument()
  })
})

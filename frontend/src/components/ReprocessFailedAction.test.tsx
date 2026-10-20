import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { post } from '../api'
import ReprocessFailedAction from './ReprocessFailedAction'

vi.mock('../api', () => ({ post: vi.fn() }))

const postMock = vi.mocked(post)
const DRY_RUN = '/api/docs/reprocess-failed?dry_run=true'
const RUN = '/api/docs/reprocess-failed'

describe('ReprocessFailedAction (#554)', () => {
  beforeEach(() => {
    postMock.mockReset()
  })

  it('confirms with the endpoint’s own count, then reports how many were queued', async () => {
    postMock.mockImplementation((url: string) =>
      Promise.resolve(url === DRY_RUN ? { requeued: 0, remaining: 7 } : { requeued: 7, remaining: 0 }),
    )
    const onDone = vi.fn()
    render(<ReprocessFailedAction onDone={onDone} />)

    fireEvent.click(screen.getByRole('button', { name: 'Reprocess all failed' }))
    expect(await screen.findByText('Reprocess 7 failed documents?')).toBeInTheDocument()
    expect(postMock).toHaveBeenCalledTimes(1)
    expect(postMock).toHaveBeenCalledWith(DRY_RUN)

    fireEvent.click(screen.getByRole('button', { name: 'Confirm reprocess' }))
    expect(await screen.findByRole('status')).toHaveTextContent('Queued 7 documents for reprocessing.')
    expect(postMock).toHaveBeenLastCalledWith(RUN)
    expect(onDone).toHaveBeenCalledOnce()
  })

  it('does nothing until confirmed: Cancel makes no real call', async () => {
    postMock.mockResolvedValue({ requeued: 0, remaining: 3 })
    render(<ReprocessFailedAction onDone={vi.fn()} />)

    fireEvent.click(screen.getByRole('button', { name: 'Reprocess all failed' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel' }))

    expect(screen.getByRole('button', { name: 'Reprocess all failed' })).toBeInTheDocument()
    expect(postMock).toHaveBeenCalledTimes(1)
    expect(postMock).toHaveBeenCalledWith(DRY_RUN)
  })

  it('says how many are left when more than one batch failed, and offers the rest', async () => {
    postMock.mockImplementation((url: string) =>
      Promise.resolve(url === DRY_RUN ? { requeued: 0, remaining: 1500 } : { requeued: 1000, remaining: 500 }),
    )
    render(<ReprocessFailedAction onDone={vi.fn()} />)

    fireEvent.click(screen.getByRole('button', { name: 'Reprocess all failed' }))
    expect(await screen.findByText(/The 1,000 most recent are queued now/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm reprocess' }))

    expect(await screen.findByRole('status')).toHaveTextContent(
      'Queued 1,000 documents for reprocessing. 500 failed documents still waiting.',
    )
    expect(screen.getByRole('button', { name: 'Reprocess the rest' })).toBeInTheDocument()
  })

  it('makes no real call when nothing matches', async () => {
    postMock.mockResolvedValue({ requeued: 0, remaining: 0 })
    render(<ReprocessFailedAction onDone={vi.fn()} />)

    fireEvent.click(screen.getByRole('button', { name: 'Reprocess all failed' }))
    expect(await screen.findByRole('status')).toHaveTextContent('No failed documents to reprocess.')
    expect(postMock).toHaveBeenCalledTimes(1)
  })

  it('shows the server’s refusal', async () => {
    postMock.mockRejectedValue(new Error('Admin access required'))
    render(<ReprocessFailedAction onDone={vi.fn()} />)

    fireEvent.click(screen.getByRole('button', { name: 'Reprocess all failed' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Admin access required')
  })
})

import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { post } from '../api'
import ReprocessFailedAction from './ReprocessFailedAction'

vi.mock('../api', () => ({ post: vi.fn() }))

const postMock = vi.mocked(post)
const DRY_RUN = '/api/docs/reprocess-failed?dry_run=true'

describe('ReprocessFailedAction (#554)', () => {
  beforeEach(() => {
    postMock.mockReset()
  })

  it('confirms with the endpoint’s own count, then reports how many were queued', async () => {
    postMock.mockImplementation((url: string) =>
      Promise.resolve(url === DRY_RUN ? { requeued: 0, remaining: 7, limit: 1000 } : { requeued: 7, remaining: 0 }),
    )
    const onDone = vi.fn()
    render(<ReprocessFailedAction onDone={onDone} />)

    fireEvent.click(screen.getByRole('button', { name: 'Reprocess all failed' }))
    expect(await screen.findByText('Reprocess all 7 failed documents in the library?')).toBeInTheDocument()
    expect(postMock).toHaveBeenCalledTimes(1)
    expect(postMock).toHaveBeenCalledWith(DRY_RUN)

    fireEvent.click(screen.getByRole('button', { name: 'Confirm reprocess' }))
    expect(await screen.findByRole('status')).toHaveTextContent('Queued 7 documents for reprocessing.')
    expect(postMock).toHaveBeenLastCalledWith('/api/docs/reprocess-failed?limit=1000')
    expect(onDone).toHaveBeenCalledOnce()
  })

  it('does nothing until confirmed: Cancel makes no real call', async () => {
    postMock.mockResolvedValue({ requeued: 0, remaining: 3, limit: 1000 })
    render(<ReprocessFailedAction onDone={vi.fn()} />)

    fireEvent.click(screen.getByRole('button', { name: 'Reprocess all failed' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel' }))

    expect(screen.getByRole('button', { name: 'Reprocess all failed' })).toBeInTheDocument()
    expect(postMock).toHaveBeenCalledTimes(1)
    expect(postMock).toHaveBeenCalledWith(DRY_RUN)
  })

  it('takes the batch size from the dry run, sends it with the real call, and offers the rest', async () => {
    // A limit the frontend has no copy of: the wording and the call must follow the server.
    postMock.mockImplementation((url: string) =>
      Promise.resolve(url === DRY_RUN ? { requeued: 0, remaining: 40, limit: 25 } : { requeued: 25, remaining: 15 }),
    )
    render(<ReprocessFailedAction onDone={vi.fn()} />)

    fireEvent.click(screen.getByRole('button', { name: 'Reprocess all failed' }))
    expect(await screen.findByText(/The 25 most recent are queued now/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm reprocess' }))

    expect(await screen.findByRole('status')).toHaveTextContent(
      'Queued 25 documents for reprocessing. 15 failed documents still waiting.',
    )
    expect(postMock).toHaveBeenLastCalledWith('/api/docs/reprocess-failed?limit=25')
    expect(screen.getByRole('button', { name: 'Reprocess the rest' })).toBeInTheDocument()
  })

  it('says nothing about batches when everything fits in one', async () => {
    postMock.mockResolvedValue({ requeued: 0, remaining: 25, limit: 25 })
    render(<ReprocessFailedAction onDone={vi.fn()} />)

    fireEvent.click(screen.getByRole('button', { name: 'Reprocess all failed' }))
    expect(await screen.findByText('Reprocess all 25 failed documents in the library?')).toBeInTheDocument()
    expect(screen.queryByText(/most recent are queued now/)).not.toBeInTheDocument()
  })

  it('with other filters active, says they do not apply and the count is the whole library', async () => {
    // The view may show 3 failed documents in one folder; the endpoint covers all 500.
    postMock.mockResolvedValue({ requeued: 0, remaining: 500, limit: 1000 })
    render(<ReprocessFailedAction onDone={vi.fn()} otherFiltersActive />)

    fireEvent.click(screen.getByRole('button', { name: 'Reprocess all failed' }))
    expect(
      await screen.findByText(
        'Reprocess all 500 failed documents in the library? Your other filters do not apply: this covers every failed document.',
      ),
    ).toBeInTheDocument()
    expect(postMock).toHaveBeenCalledWith(DRY_RUN)
  })

  it('without other filters, makes no claim about them', async () => {
    postMock.mockResolvedValue({ requeued: 0, remaining: 500, limit: 1000 })
    render(<ReprocessFailedAction onDone={vi.fn()} />)

    fireEvent.click(screen.getByRole('button', { name: 'Reprocess all failed' }))
    expect(await screen.findByText('Reprocess all 500 failed documents in the library?')).toBeInTheDocument()
    expect(screen.queryByText(/other filters do not apply/)).not.toBeInTheDocument()
  })

  it('makes no real call when nothing matches', async () => {
    postMock.mockResolvedValue({ requeued: 0, remaining: 0, limit: 1000 })
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

import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { get, patch, post } from '../api'
import ApiKeysPage from './ApiKeysPage'

vi.mock('../api', () => ({
  del: vi.fn(),
  get: vi.fn(),
  patch: vi.fn(),
  post: vi.fn(),
}))

const getMock = vi.mocked(get)
const patchMock = vi.mocked(patch)
const postMock = vi.mocked(post)

function key(maxSnippetChars: number | null) {
  return {
    key_id: 'k1',
    name: 'Cloud connector',
    is_active: true,
    created_at: '2026-10-01T00:00:00Z',
    expires_at: null,
    permission_tier: 'read',
    tool_overrides: {},
    scope_topic_ids: null,
    scope_folder_ids: null,
    max_snippet_chars: maxSnippetChars,
    scope_summary: 'Read only',
    rate_limit_rpm: null,
    rate_limit_rph: null,
  }
}

function stubKeys(keys: ReturnType<typeof key>[]) {
  getMock.mockImplementation((path: string) => {
    if (path === '/api/api-keys') return Promise.resolve(keys)
    if (path === '/api/stats/topics') return Promise.resolve({ clusters: [] })
    if (path === '/api/watch/folders') return Promise.resolve([])
    return Promise.reject(new Error(`Unexpected path ${path}`))
  })
}

async function openEdit() {
  render(
    <MemoryRouter>
      <ApiKeysPage />
    </MemoryRouter>,
  )
  fireEvent.click(await screen.findByRole('button', { name: 'Edit' }))
}

function capInput() {
  return screen.getByLabelText('Max snippet chars') as HTMLInputElement
}

function rpmInput() {
  return screen.getAllByPlaceholderText('System default')[0] as HTMLInputElement
}

describe('ApiKeysPage snippet cap', () => {
  beforeEach(() => {
    getMock.mockReset()
    patchMock.mockReset()
    postMock.mockReset()
    patchMock.mockResolvedValue({})
    postMock.mockImplementation((path: string) =>
      path === '/api/api-keys/scope-preview'
        ? Promise.resolve({ accessible_documents: 1, total_documents: 1 })
        : Promise.resolve({ key_id: 'k2', name: 'n', raw_key: 'hc_x', mcp_path: '/t/hc_x', created_at: '' }),
    )
  })

  it('does not send an unchanged cap on edit, so editing a key capped at 0 keeps its cap', async () => {
    stubKeys([key(0)])
    await openEdit()
    expect(capInput().value).toBe('0')
    fireEvent.change(rpmInput(), { target: { value: '30' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(patchMock).toHaveBeenCalledTimes(1))
    const body = patchMock.mock.calls[0][1] as Record<string, unknown>
    expect(body).not.toHaveProperty('max_snippet_chars')
    expect(body.rate_limit_rpm).toBe(30)
  })

  it('does not send a legacy negative cap, shown as 0, when it is left unchanged', async () => {
    stubKeys([key(-5)])
    await openEdit()
    expect(capInput().value).toBe('0')
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(patchMock).toHaveBeenCalledTimes(1))
    expect(patchMock.mock.calls[0][1]).not.toHaveProperty('max_snippet_chars')
  })

  it('sends 0 for "0" and null for an emptied field on edit', async () => {
    stubKeys([key(200)])
    await openEdit()
    fireEvent.change(capInput(), { target: { value: '0' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(patchMock).toHaveBeenCalledTimes(1))
    expect(patchMock.mock.calls[0][1]).toMatchObject({ max_snippet_chars: 0 })

    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }))
    fireEvent.change(capInput(), { target: { value: '' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(patchMock).toHaveBeenCalledTimes(2))
    expect(patchMock.mock.calls[1][1]).toMatchObject({ max_snippet_chars: null })
  })

  it('rejects a negative cap with a visible message and sends nothing', async () => {
    stubKeys([key(200)])
    await openEdit()
    fireEvent.change(capInput(), { target: { value: '-3' } })
    expect(screen.getByRole('alert')).toHaveTextContent('0 or more')
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByText(/must be a whole number of 0 or more/, { selector: 'div' })).toBeInTheDocument()
    expect(patchMock).not.toHaveBeenCalled()
  })

  it('creates a key with a cap of 0 when "0" is entered', async () => {
    stubKeys([])
    render(
      <MemoryRouter>
        <ApiKeysPage />
      </MemoryRouter>,
    )
    fireEvent.click(await screen.findByRole('button', { name: 'Create Key' }))
    fireEvent.change(screen.getByPlaceholderText('e.g. Claude Desktop'), { target: { value: 'Capped' } })
    fireEvent.change(capInput(), { target: { value: '0' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))
    await waitFor(() => expect(postMock).toHaveBeenCalledWith('/api/api-keys', expect.anything()))
    const body = postMock.mock.calls.find((c) => c[0] === '/api/api-keys')?.[1] as Record<string, unknown>
    expect(body.max_snippet_chars).toBe(0)
  })
})

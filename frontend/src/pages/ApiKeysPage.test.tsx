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

function key(fields: Record<string, unknown> = {}) {
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
    max_snippet_chars: null,
    scope_summary: 'Read only',
    rate_limit_rpm: null,
    rate_limit_rph: null,
    ...fields,
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

function renderPage() {
  render(
    <MemoryRouter>
      <ApiKeysPage />
    </MemoryRouter>,
  )
}

async function openEdit() {
  renderPage()
  fireEvent.click(await screen.findByRole('button', { name: 'Edit' }))
}

function capInput() {
  return screen.getByLabelText('Max snippet chars') as HTMLInputElement
}

async function save() {
  fireEvent.click(screen.getByRole('button', { name: 'Save' }))
}

async function savedBody(call = 0): Promise<Record<string, unknown>> {
  await waitFor(() => expect(patchMock).toHaveBeenCalledTimes(call + 1))
  return patchMock.mock.calls[call][1] as Record<string, unknown>
}

describe('ApiKeysPage edit and create', () => {
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

  it('sends only the changed field: a key with an API-set expiry time and a negative rpm keeps both', async () => {
    stubKeys([key({ expires_at: '2026-12-01T10:30:00Z', rate_limit_rpm: -5, max_snippet_chars: 0 })])
    await openEdit()
    fireEvent.change(screen.getByLabelText('Key name'), { target: { value: 'Renamed connector' } })
    await save()
    expect(await savedBody()).toEqual({ name: 'Renamed connector' })
  })

  it('does not send an unchanged cap, so editing a key capped at 0 keeps its cap', async () => {
    stubKeys([key({ max_snippet_chars: 0 })])
    await openEdit()
    expect(capInput().value).toBe('0')
    fireEvent.change(screen.getByLabelText('Requests per minute'), { target: { value: '30' } })
    await save()
    expect(await savedBody()).toEqual({ rate_limit_rpm: 30 })
  })

  it('sends nothing when nothing changed, a legacy negative cap (shown as 0) included', async () => {
    stubKeys([key({ max_snippet_chars: -5 })])
    await openEdit()
    expect(capInput().value).toBe('0')
    await save()
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Save' })).not.toBeInTheDocument())
    expect(patchMock).not.toHaveBeenCalled()
  })

  it('sends 0 for "0" and null for an emptied field', async () => {
    stubKeys([key({ max_snippet_chars: 200 })])
    await openEdit()
    fireEvent.change(capInput(), { target: { value: '0' } })
    await save()
    expect(await savedBody()).toEqual({ max_snippet_chars: 0 })

    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }))
    fireEvent.change(capInput(), { target: { value: '' } })
    await save()
    expect(await savedBody(1)).toEqual({ max_snippet_chars: null })
  })

  it.each(['-3', '5e', '5OO', '50-'])('rejects a cap of %j with a visible message and sends nothing', async (raw) => {
    stubKeys([key({ max_snippet_chars: 50 })])
    await openEdit()
    fireEvent.change(capInput(), { target: { value: raw } })
    expect(capInput().value).toBe(raw) // a text input keeps what was typed; a number input would report ""
    expect(screen.getByRole('alert')).toHaveTextContent('whole number')
    await save()
    expect(await screen.findByText(/Max snippet chars must be a whole number/, { selector: 'div' })).toBeInTheDocument()
    expect(patchMock).not.toHaveBeenCalled()
  })

  it('rejects a mistyped rate limit instead of resetting it to the default', async () => {
    stubKeys([key({ rate_limit_rpm: 30 })])
    await openEdit()
    fireEvent.change(screen.getByLabelText('Requests per minute'), { target: { value: '3O' } })
    await save()
    expect(await screen.findByText(/Requests\/min must be a whole number/, { selector: 'div' })).toBeInTheDocument()
    expect(patchMock).not.toHaveBeenCalled()
  })

  it('creates a key with a cap of 0 when "0" is entered, and refuses a mistyped cap', async () => {
    stubKeys([])
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'Create Key' }))
    fireEvent.change(screen.getByPlaceholderText('e.g. Claude Desktop'), { target: { value: 'Capped' } })
    fireEvent.change(capInput(), { target: { value: '5OO' } })
    expect(screen.getByRole('alert')).toHaveTextContent('whole number')
    // Inside a <form> the input's pattern blocks the submit natively; handleSubmit re-checks regardless.
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))
    await new Promise((r) => setTimeout(r, 50))
    expect(postMock).not.toHaveBeenCalledWith('/api/api-keys', expect.anything())

    fireEvent.change(capInput(), { target: { value: '0' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))
    await waitFor(() => expect(postMock).toHaveBeenCalledWith('/api/api-keys', expect.anything()))
    const body = postMock.mock.calls.find((c) => c[0] === '/api/api-keys')?.[1] as Record<string, unknown>
    expect(body.max_snippet_chars).toBe(0)
  })
})

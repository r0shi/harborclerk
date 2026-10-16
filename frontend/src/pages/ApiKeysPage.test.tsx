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
    if (path === '/api/stats/topics')
      return Promise.resolve({
        clusters: [
          { cluster_id: 1, name: 'Leases' },
          { cluster_id: 2, name: 'Invoices' },
        ],
      })
    if (path === '/api/watch/folders') return Promise.resolve([{ folder_id: 'f1', path: '/Users/x/Contracts' }])
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

  // --- a changed field is sent, alone ------------------------------------------

  it.each([
    ['tier', {}, () => fireEvent.click(screen.getByRole('radio', { name: 'full' })), { permission_tier: 'full' }],
    [
      'tool overrides',
      {},
      () => fireEvent.click(screen.getByLabelText('kb_read_document')),
      { tool_overrides: { kb_read_document: true } },
    ],
    ['topics', {}, async () => fireEvent.click(await screen.findByLabelText('Leases')), { scope_topic_ids: [1] }],
    [
      'folders',
      {},
      async () => fireEvent.click(await screen.findByLabelText('/Users/x/Contracts')),
      { scope_folder_ids: ['f1'] },
    ],
    [
      'expiry',
      { expires_at: '2026-12-01T10:30:00Z' },
      () => fireEvent.change(document.querySelector('input[type="date"]')!, { target: { value: '2027-01-15' } }),
      { expires_at: '2027-01-15T23:59:59Z' },
    ],
    [
      'rpm',
      {},
      () => fireEvent.change(screen.getByLabelText('Requests per minute'), { target: { value: '30' } }),
      { rate_limit_rpm: 30 },
    ],
    [
      'rph',
      {},
      () => fireEvent.change(screen.getByLabelText('Requests per hour'), { target: { value: '100' } }),
      { rate_limit_rph: 100 },
    ],
    [
      'unlimited rpm',
      { rate_limit_rpm: 30 },
      () => fireEvent.click(screen.getByLabelText('Unlimited requests/min')),
      { rate_limit_rpm: 0 },
    ],
    [
      'unlimited rph',
      {},
      () => fireEvent.click(screen.getByLabelText('Unlimited requests/hour')),
      { rate_limit_rph: 0 },
    ],
    [
      'name',
      { name: '  Cloud connector  ' },
      () => fireEvent.change(screen.getByLabelText('Key name'), { target: { value: ' Renamed ' } }),
      { name: 'Renamed' },
    ],
  ] as const)('sends a changed %s, and only it', async (_label, stored, change, expected) => {
    stubKeys([key(stored)])
    await openEdit()
    await change()
    await save()
    expect(await savedBody()).toEqual(expected)
  })

  it('sends nothing when topics are re-ticked in a different order from the stored list', async () => {
    stubKeys([key({ scope_topic_ids: [1, 2] })])
    await openEdit()
    const leases = await screen.findByLabelText('Leases')
    fireEvent.click(leases) // [2]
    fireEvent.click(leases) // [2, 1]: the same set
    await save()
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Save' })).not.toBeInTheDocument())
    expect(patchMock).not.toHaveBeenCalled()
  })

  it('does not report a change for a stored name with surrounding spaces', async () => {
    stubKeys([key({ name: '  Cloud connector  ' })])
    await openEdit()
    fireEvent.change(screen.getByLabelText('Requests per minute'), { target: { value: '30' } })
    await save()
    expect(await savedBody()).toEqual({ rate_limit_rpm: 30 })
  })

  it('shows a legacy negative rate limit as Unlimited; unticking it sends null', async () => {
    stubKeys([key({ rate_limit_rpm: -5 })])
    await openEdit()
    const unlimited = screen.getByLabelText('Unlimited requests/min') as HTMLInputElement
    expect(unlimited.checked).toBe(true)
    fireEvent.click(unlimited)
    await save()
    expect(await savedBody()).toEqual({ rate_limit_rpm: null })
  })

  it('refuses a blank name inside the modal, where it can be seen', async () => {
    stubKeys([key()])
    await openEdit()
    fireEvent.change(screen.getByLabelText('Key name'), { target: { value: '   ' } })
    await save()
    const dialogError = await screen.findByText('Key name cannot be empty.')
    expect(dialogError.closest('.fixed')).not.toBeNull() // rendered inside the modal overlay
    expect(patchMock).not.toHaveBeenCalled()
  })

  // --- create ---------------------------------------------------------------------

  async function openCreate() {
    stubKeys([])
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'Create Key' }))
    fireEvent.change(screen.getByPlaceholderText('e.g. Claude Desktop'), { target: { value: 'Capped' } })
  }

  function createdBody(): Record<string, unknown> | undefined {
    return postMock.mock.calls.find((c) => c[0] === '/api/api-keys')?.[1] as Record<string, unknown> | undefined
  }

  it('refuses a cap past the 32-bit column on create, with an inline message and no POST', async () => {
    await openCreate()
    fireEvent.change(capInput(), { target: { value: '3000000000' } })
    expect(screen.getByRole('alert')).toHaveTextContent('whole number')
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))
    expect(await screen.findByText(/Max snippet chars must be a whole number/, { selector: 'div' })).toBeInTheDocument()
    expect(createdBody()).toBeUndefined()
  })

  it('accepts " 50" on create: the parser trims, and no pattern blocks the submit', async () => {
    await openCreate()
    fireEvent.change(capInput(), { target: { value: ' 50' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))
    await waitFor(() => expect(createdBody()).toBeDefined())
    expect(createdBody()).toMatchObject({ name: 'Capped', max_snippet_chars: 50 })
  })

  it('refuses a blank name on create and trims a padded one', async () => {
    await openCreate()
    fireEvent.change(screen.getByPlaceholderText('e.g. Claude Desktop'), { target: { value: '   ' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))
    expect(await screen.findByText('Key name cannot be empty.')).toBeInTheDocument()
    expect(createdBody()).toBeUndefined()
    fireEvent.change(screen.getByPlaceholderText('e.g. Claude Desktop'), { target: { value: '  Padded  ' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))
    await waitFor(() => expect(createdBody()).toBeDefined())
    expect(createdBody()?.name).toBe('Padded')
  })

  it('creates a key with a cap of 0 when "0" is entered, and refuses a mistyped cap', async () => {
    await openCreate()
    fireEvent.change(capInput(), { target: { value: '5OO' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))
    expect(await screen.findByText(/Max snippet chars must be a whole number/, { selector: 'div' })).toBeInTheDocument()
    expect(createdBody()).toBeUndefined()

    fireEvent.change(capInput(), { target: { value: '0' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))
    await waitFor(() => expect(createdBody()).toBeDefined())
    expect(createdBody()?.max_snippet_chars).toBe(0)
  })
})

import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { del, get, patch, post } from '../api'
import { EXPIRY_REQUIRED_ERROR, endOfLocalDayIso } from '../utils/apiKeyForm'
import ApiKeysPage from './ApiKeysPage'

vi.mock('../api', () => ({
  del: vi.fn(),
  get: vi.fn(),
  patch: vi.fn(),
  post: vi.fn(),
}))

const delMock = vi.mocked(del)
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

function stubKeys(keys: ReturnType<typeof key>[], failing: string[] = []) {
  getMock.mockImplementation((path: string) => {
    if (failing.includes(path)) return Promise.reject(new Error('500 Internal Server Error'))
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
    delMock.mockReset()
    getMock.mockReset()
    patchMock.mockReset()
    postMock.mockReset()
    delMock.mockResolvedValue(undefined)
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
      { expires_at: endOfLocalDayIso('2027-01-15') },
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

// --- #833 ---------------------------------------------------------------------------

describe('ApiKeysPage (#833)', () => {
  beforeEach(() => {
    delMock.mockReset()
    getMock.mockReset()
    patchMock.mockReset()
    postMock.mockReset()
    delMock.mockResolvedValue(undefined)
    patchMock.mockResolvedValue({})
    postMock.mockImplementation((path: string) =>
      path === '/api/api-keys/scope-preview'
        ? Promise.resolve({ accessible_documents: 1, total_documents: 1 })
        : Promise.resolve({ key_id: 'k2', name: 'n', raw_key: 'hc_x', mcp_path: '/t/hc_x', created_at: '' }),
    )
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  // 1. Revoke ---------------------------------------------------------------------------

  it('revokes through the in-page confirm, even where window.confirm returns false (WKWebView)', async () => {
    const nativeConfirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
    stubKeys([key()])
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'Revoke' }))
    expect(screen.getByText('Revoke this key? This cannot be undone.')).toBeInTheDocument()
    expect(delMock).not.toHaveBeenCalled() // the first click only asks
    fireEvent.click(screen.getByRole('button', { name: 'Confirm revoke' }))
    await waitFor(() => expect(delMock).toHaveBeenCalledWith('/api/api-keys/k1'))
    expect(delMock).toHaveBeenCalledTimes(1)
    expect(nativeConfirm).not.toHaveBeenCalled()
    // The list is reloaded after the revoke.
    await waitFor(() => expect(getMock.mock.calls.filter((c) => c[0] === '/api/api-keys')).toHaveLength(2))
  })

  it('sends nothing when the revoke is cancelled', async () => {
    stubKeys([key()])
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'Revoke' }))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('button', { name: 'Confirm revoke' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Revoke' })).toBeInTheDocument()
    expect(delMock).not.toHaveBeenCalled()
  })

  it('shows a failed revoke on the page', async () => {
    delMock.mockRejectedValue(new Error('Key not found'))
    stubKeys([key()])
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'Revoke' }))
    fireEvent.click(screen.getByRole('button', { name: 'Confirm revoke' }))
    expect(await screen.findByText('Key not found')).toBeInTheDocument()
  })

  // 2. An emptied date is invalid, never "never expires" ----------------------------------

  function dateInput() {
    return screen.getByLabelText('Expiry date') as HTMLInputElement
  }

  it('refuses an emptied expiry date in the edit modal instead of saving "never expires"', async () => {
    stubKeys([key({ expires_at: '2027-03-01T10:00:00Z' })])
    await openEdit()
    fireEvent.change(dateInput(), { target: { value: '' } }) // what a cleared date segment reports
    expect(dateInput()).toBeInTheDocument() // the input stays, so the operator can finish the date
    expect((screen.getByLabelText('Set expiry date') as HTMLInputElement).checked).toBe(true)
    expect(screen.getByRole('alert')).toHaveTextContent(EXPIRY_REQUIRED_ERROR)
    await save()
    expect(await screen.findByText(EXPIRY_REQUIRED_ERROR, { selector: 'div' })).toBeInTheDocument()
    expect(patchMock).not.toHaveBeenCalled()
  })

  it('refuses an emptied expiry date on create', async () => {
    stubKeys([])
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'Create Key' }))
    fireEvent.change(screen.getByPlaceholderText('e.g. Claude Desktop'), { target: { value: 'Expiring' } })
    fireEvent.click(screen.getByLabelText('Set expiry date'))
    fireEvent.change(dateInput(), { target: { value: '' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))
    expect(await screen.findByText(EXPIRY_REQUIRED_ERROR, { selector: 'div' })).toBeInTheDocument()
    expect(postMock.mock.calls.find((c) => c[0] === '/api/api-keys')).toBeUndefined()
  })

  it('re-ticking "Set expiry date" restores the key\'s date, not a new default, and sends nothing', async () => {
    stubKeys([key({ expires_at: '2027-03-01T10:00:00Z' })])
    await openEdit()
    const stored = dateInput().value
    const box = screen.getByLabelText('Set expiry date')
    fireEvent.click(box) // untick
    expect(screen.queryByLabelText('Expiry date')).not.toBeInTheDocument()
    fireEvent.click(box) // tick again
    expect(dateInput().value).toBe(stored)
    await save()
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Save' })).not.toBeInTheDocument())
    expect(patchMock).not.toHaveBeenCalled()
  })

  it('ticking and unticking "Set expiry date" on a key with no expiry sends nothing', async () => {
    stubKeys([key()]) // expires_at: null
    await openEdit()
    const box = screen.getByLabelText('Set expiry date')
    fireEvent.click(box) // tick: a default date appears
    expect(dateInput().value).not.toBe('')
    fireEvent.click(box) // untick: the date is kept in state, but the key still never expires
    await save()
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Save' })).not.toBeInTheDocument())
    expect(patchMock).not.toHaveBeenCalled()
  })

  it('removes an expiry only when the box is unticked', async () => {
    stubKeys([key({ expires_at: '2027-03-01T10:00:00Z' })])
    await openEdit()
    fireEvent.click(screen.getByLabelText('Set expiry date'))
    await save()
    expect(await savedBody()).toEqual({ expires_at: null })
  })

  // 3. One time-zone convention -------------------------------------------------------------

  describe('east of UTC', () => {
    beforeEach(() => {
      vi.stubEnv('TZ', 'Pacific/Auckland') // UTC+13 in January
    })
    afterEach(() => {
      vi.unstubAllEnvs()
    })

    it('shows a local end-of-day expiry as its date in the table and the modal, with the end-of-day hint', async () => {
      stubKeys([key({ expires_at: endOfLocalDayIso('2027-01-16') })])
      renderPage()
      expect(await screen.findByText(new Date(2027, 0, 16).toLocaleDateString())).toBeInTheDocument()
      fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
      expect(dateInput().value).toBe('2027-01-16')
      expect(screen.getByText(/stops working at the end of this day/)).toBeInTheDocument()
    })

    it('shows a legacy T23:59:59Z by the local moment it stops: 12:59 PM on the 16th', async () => {
      // The old modal showed the 15th and the old table the 16th, neither with the time.
      stubKeys([key({ expires_at: '2027-01-15T23:59:59Z' })])
      renderPage()
      const moment = new Date(2027, 0, 16, 12, 59).toLocaleString(undefined, {
        dateStyle: 'medium',
        timeStyle: 'short',
      })
      expect(await screen.findByText(moment)).toBeInTheDocument()
      fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
      expect(dateInput().value).toBe('2027-01-16')
      expect(screen.getByText(/The key stops working at/)).toHaveTextContent(
        `The key stops working at ${moment}, the time it was saved with.`,
      )
    })

    it('saves a chosen date as the end of that local day, in UTC', async () => {
      stubKeys([key()])
      await openEdit()
      fireEvent.click(screen.getByLabelText('Set expiry date'))
      fireEvent.change(dateInput(), { target: { value: '2027-01-15' } })
      await save()
      expect(await savedBody()).toEqual({ expires_at: '2027-01-15T10:59:59.000Z' })
    })
  })

  describe('west of UTC', () => {
    beforeEach(() => {
      vi.stubEnv('TZ', 'America/Los_Angeles') // UTC-8 in January
    })
    afterEach(() => {
      vi.unstubAllEnvs()
    })

    it('never claims end of day for a legacy T23:59:59Z, which stops at 3:59 PM local', async () => {
      stubKeys([key({ expires_at: '2027-01-15T23:59:59Z' })])
      renderPage()
      const moment = new Date(2027, 0, 15, 15, 59).toLocaleString(undefined, {
        dateStyle: 'medium',
        timeStyle: 'short',
      })
      expect(await screen.findByText(moment)).toBeInTheDocument() // the table
      fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
      expect(dateInput().value).toBe('2027-01-15')
      const hint = screen.getByText(/The key stops working at/)
      expect(hint).toHaveTextContent(`The key stops working at ${moment}, the time it was saved with.`)
      expect(screen.queryByText(/stops working at the end of this day/)).not.toBeInTheDocument()

      // Choosing another date does set the end of that local day, and the hint says so.
      fireEvent.change(dateInput(), { target: { value: '2027-01-20' } })
      expect(screen.getByText(/stops working at the end of this day/)).toBeInTheDocument()
      await save()
      expect(await savedBody()).toEqual({ expires_at: '2027-01-21T07:59:59.000Z' })
    })
  })

  // The key table does not wait for the topic and folder lists ----------------------------------

  it('renders the table and Revoke while the scope lists are still loading; the forms say they wait', async () => {
    getMock.mockImplementation((path: string) => {
      if (path === '/api/api-keys') return Promise.resolve([key({ scope_topic_ids: [1] })])
      return new Promise(() => {}) // /api/stats/topics and /api/watch/folders never answer
    })
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'Revoke' }))
    fireEvent.click(screen.getByRole('button', { name: 'Confirm revoke' }))
    await waitFor(() => expect(delMock).toHaveBeenCalledWith('/api/api-keys/k1'))

    fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
    expect(screen.getByRole('status')).toHaveTextContent('Loading topics and folders…')
    expect(screen.getByText(/Document scope:/)).toHaveTextContent('limited to 1 topic') // from the stored ids
    expect(screen.queryByRole('list', { name: 'Unlisted topics' })).not.toBeInTheDocument() // not "stale" yet
    expect(screen.queryByText(/no longer listed/)).not.toBeInTheDocument()
    expect(screen.queryByRole('checkbox', { name: 'Leases' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))

    fireEvent.click(screen.getByRole('button', { name: 'Create Key' }))
    expect(screen.getByRole('status')).toHaveTextContent('Loading topics and folders…')
  })

  // 4. Stored scope ids that are no longer listed ---------------------------------------------

  it('shows a stored topic id that is no longer listed, and keeps it until it is removed', async () => {
    stubKeys([key({ scope_topic_ids: [1, 99] })])
    await openEdit()
    expect(screen.getByText(/Document scope: limited to 2 topics/)).toHaveTextContent('(1 no longer listed)')
    const chips = await screen.findByRole('list', { name: 'Unlisted topics' })
    expect(chips).toHaveTextContent('Topic 99')

    // Unticking the only visible topic leaves the key limited to the unlisted one, and says so.
    fireEvent.click(await screen.findByLabelText('Leases'))
    expect(screen.getByText(/Document scope:/)).toHaveTextContent('Document scope: limited to 1 topic')
    await save()
    expect(await savedBody()).toEqual({ scope_topic_ids: [99] })
  })

  it('removing the last unlisted topic chip drops the restriction only after an in-modal confirmation', async () => {
    stubKeys([key({ scope_topic_ids: [99] })])
    await openEdit()
    fireEvent.click(await screen.findByRole('button', { name: 'Remove topic 99' }))
    expect(screen.queryByRole('list', { name: 'Unlisted topics' })).not.toBeInTheDocument()
    expect(screen.getByText(/Document scope:/)).toHaveTextContent('Document scope: no restriction')
    await save()
    expect(await screen.findByText(/removes the key's last document restriction/)).toBeInTheDocument()
    expect(patchMock).not.toHaveBeenCalled() // Save alone does not unrestrict the key
    fireEvent.click(screen.getByRole('button', { name: 'Remove restriction and save' }))
    expect(await savedBody()).toEqual({ scope_topic_ids: null })
  })

  it('unticking the last listed restriction also asks first, and "Keep editing" sends nothing', async () => {
    stubKeys([key({ scope_folder_ids: ['f1'] })])
    await openEdit()
    fireEvent.click(await screen.findByLabelText('/Users/x/Contracts'))
    await save()
    fireEvent.click(await screen.findByRole('button', { name: 'Keep editing' }))
    expect(screen.queryByText(/removes the key's last document restriction/)).not.toBeInTheDocument()
    expect(patchMock).not.toHaveBeenCalled()
  })

  it('withdraws the question when the scope is edited again', async () => {
    stubKeys([key({ scope_topic_ids: [1] })])
    await openEdit()
    const leases = await screen.findByLabelText('Leases')
    fireEvent.click(leases) // no restriction left
    await save()
    expect(await screen.findByText(/removes the key's last document restriction/)).toBeInTheDocument()
    fireEvent.click(screen.getByLabelText('Invoices')) // restricted again, to a different topic
    expect(screen.queryByText(/removes the key's last document restriction/)).not.toBeInTheDocument()
    await save()
    expect(await savedBody()).toEqual({ scope_topic_ids: [2] })
  })

  it('closing the modal drops the unrestrict question', async () => {
    stubKeys([key({ scope_topic_ids: [1] }), key({ key_id: 'k2', name: 'Second key', scope_topic_ids: [2] })])
    renderPage()
    fireEvent.click((await screen.findAllByRole('button', { name: 'Edit' }))[0])
    fireEvent.click(await screen.findByLabelText('Leases')) // k1 loses its only restriction
    await save()
    expect(await screen.findByText(/removes the key's last document restriction/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    fireEvent.click(screen.getAllByRole('button', { name: 'Edit' })[1])
    expect(await screen.findByText('Edit Key: Second key')).toBeInTheDocument()
    expect(screen.queryByText(/removes the key's last document restriction/)).not.toBeInTheDocument()
    expect((screen.getByLabelText('Invoices') as HTMLInputElement).checked).toBe(true) // k2's own scope
    expect(patchMock).not.toHaveBeenCalled()
  })

  it('does not ask when the key had no restriction to begin with', async () => {
    stubKeys([key()])
    await openEdit()
    fireEvent.click(screen.getByRole('radio', { name: 'full' }))
    await save()
    expect(await savedBody()).toEqual({ permission_tier: 'full' })
  })

  // A list that failed to load is not an empty one.

  it('says a failed topic list failed, keeps the stored topics and offers nothing to remove', async () => {
    stubKeys([key({ scope_topic_ids: [3, 4] })], ['/api/stats/topics'])
    await openEdit()
    expect(screen.getByText(/Document scope:/)).toHaveTextContent('limited to 2 topics')
    expect(screen.getByText(/Document scope:/)).toHaveTextContent('(topics could not be loaded)')
    expect(screen.getByText(/The topic list could not be loaded/)).toHaveTextContent(
      "The key's 2 stored topic restrictions are kept as is.",
    )
    expect(screen.queryByRole('list', { name: 'Unlisted topics' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Remove topic/ })).not.toBeInTheDocument()
    expect(screen.queryByText(/no longer listed/)).not.toBeInTheDocument()
    // Other edits save without touching the stored topics.
    fireEvent.change(screen.getByLabelText('Requests per minute'), { target: { value: '30' } })
    await save()
    expect(await savedBody()).toEqual({ rate_limit_rpm: 30 })
  })

  it('says a failed folder list failed while the topic list still works', async () => {
    stubKeys([key({ scope_folder_ids: ['f9'] })], ['/api/watch/folders'])
    await openEdit()
    expect(screen.getByText(/The folder list could not be loaded/)).toHaveTextContent(
      "The key's 1 stored folder restriction is kept as is.",
    )
    expect(screen.queryByRole('button', { name: /Remove folder/ })).not.toBeInTheDocument()
    expect(await screen.findByLabelText('Leases')).toBeInTheDocument()
  })

  it('shows and removes a stored folder id that is no longer listed', async () => {
    stubKeys([key({ scope_folder_ids: ['f1', 'gone-folder'] })])
    await openEdit()
    expect(await screen.findByRole('list', { name: 'Unlisted folders' })).toHaveTextContent('Folder gone-folder')
    fireEvent.click(screen.getByRole('button', { name: 'Remove folder gone-folder' }))
    await save()
    expect(await savedBody()).toEqual({ scope_folder_ids: ['f1'] })
  })
})

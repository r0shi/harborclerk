import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import DocumentErrorNotice from './DocumentErrorNotice'
import { failurePill, type ErrorInfo } from '../utils/documentFailure'

const TEMPORARY: ErrorInfo = {
  family: 'temporary',
  message: 'A background service could not be reached while this file was processed.',
  action: 'Reprocess the document to try again.',
}
const UNREADABLE: ErrorInfo = {
  family: 'unreadable',
  message: 'This file could not be read.',
  action: 'Check that the file opens on your computer.',
}
const LONG_RAW =
  'RuntimeError: Tika rejected file (422): org.apache.poi.ooxml.POIXMLException: ' + 'x'.repeat(400) + ' END-OF-ERROR'

describe('DocumentErrorNotice', () => {
  it('leads with the plain-language message and action, not the raw error', () => {
    render(<DocumentErrorNotice info={TEMPORARY} raw="ConnectError: [Errno 61] Connection refused" />)
    expect(screen.getByText('Temporary problem')).toBeInTheDocument()
    expect(screen.getByText(TEMPORARY.message)).toBeInTheDocument()
    expect(screen.getByText(TEMPORARY.action)).toBeInTheDocument()
    // The raw error sits behind a collapsed "Technical details" disclosure.
    const details = screen.getByText('Technical details').closest('details')
    expect(details).not.toHaveAttribute('open')
  })

  it('keeps the whole raw error, uncropped, and copies it', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })
    render(<DocumentErrorNotice info={UNREADABLE} raw={LONG_RAW} />)
    fireEvent.click(screen.getByText('Technical details'))
    const pre = screen.getByText((_, el) => el?.tagName === 'PRE' && el.textContent === LONG_RAW)
    expect(pre.className).toContain('whitespace-pre-wrap')
    expect(pre.className).not.toContain('truncate')
    fireEvent.click(screen.getByRole('button', { name: 'Copy' }))
    expect(writeText).toHaveBeenCalledWith(LONG_RAW)
    expect(await screen.findByRole('button', { name: 'Copied' })).toBeInTheDocument()
  })

  it('renders a temporary failure calmer than an unreadable file', () => {
    const { container, rerender } = render(<DocumentErrorNotice info={TEMPORARY} raw="x" />)
    const temporaryBox = container.firstElementChild as HTMLElement
    expect(temporaryBox.dataset.family).toBe('temporary')
    expect(temporaryBox.className).toContain('amber')
    expect(temporaryBox.className).not.toContain('red')

    rerender(<DocumentErrorNotice info={UNREADABLE} raw="x" />)
    const unreadableBox = container.firstElementChild as HTMLElement
    expect(unreadableBox.dataset.family).toBe('unreadable')
    expect(unreadableBox.className).toContain('red')
    expect(screen.getByText("This file can't be read")).toBeInTheDocument()

    expect(failurePill(TEMPORARY).state).toBe('running')
    expect(failurePill(UNREADABLE).state).toBe('error')
  })

  it('shows the raw error when the server sent no explanation, and nothing when there is no error', () => {
    const { container, rerender } = render(<DocumentErrorNotice raw="KeyError: 'pages'" />)
    expect(screen.getByText('Processing failed')).toBeInTheDocument()
    expect(container.querySelector('pre')?.textContent).toBe("KeyError: 'pages'")

    rerender(<DocumentErrorNotice />)
    expect(container).toBeEmptyDOMElement()
  })

  it('compact fallback does not point at details it does not show', () => {
    render(<DocumentErrorNotice compact raw="KeyError: 'pages'" />)
    expect(screen.getByText(/Processing failed\./)).toHaveTextContent('Open the document for the technical details.')
    expect(screen.queryByText(/below/)).not.toBeInTheDocument()
  })

  it('compact form is one wrapped line with message and action', () => {
    render(<DocumentErrorNotice compact info={TEMPORARY} raw="ConnectError" />)
    const line = screen.getByText(`${TEMPORARY.message} ${TEMPORARY.action}`)
    expect(line.className).not.toContain('truncate')
    expect(screen.queryByText('ConnectError')).not.toBeInTheDocument()
  })
})

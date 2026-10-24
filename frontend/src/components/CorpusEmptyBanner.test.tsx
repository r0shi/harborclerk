import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { describe, expect, it } from 'vitest'
import CorpusEmptyBanner from './CorpusEmptyBanner'

function renderBanner(state: 'no-folders' | 'no-documents' | null, isAdmin: boolean) {
  render(
    <MemoryRouter>
      <CorpusEmptyBanner state={state} isAdmin={isAdmin} />
    </MemoryRouter>,
  )
}

describe('CorpusEmptyBanner (#852)', () => {
  it('asks an admin to add a folder', () => {
    renderBanner('no-folders', true)
    expect(screen.getByRole('status')).toHaveTextContent(
      'No watched folders yet — add one to start indexing documents.',
    )
    expect(screen.queryByText(/an administrator adds them/)).not.toBeInTheDocument()
  })

  it('tells a non-admin that an administrator adds folders', () => {
    renderBanner('no-folders', false)
    expect(screen.getByRole('status')).toHaveTextContent('No watched folders yet — an administrator adds them.')
    expect(screen.queryByText(/add one to start indexing/)).not.toBeInTheDocument()
  })

  it('renders nothing without a state', () => {
    renderBanner(null, false)
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })
})

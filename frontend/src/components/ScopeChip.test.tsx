import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { ScopeChip } from './ScopeChip'

const folders = [
  {
    folder_id: 'a',
    display_name: 'Contracts',
    path: '/c',
    unavailable_reason: null,
    enabled: true,
    auto_discovered: false,
    skipped_count: 0,
    skipped_extensions: [],
  },
  {
    folder_id: 'b',
    display_name: 'Legal',
    path: '/l',
    unavailable_reason: null,
    enabled: true,
    auto_discovered: false,
    skipped_count: 0,
    skipped_extensions: [],
  },
]

describe('ScopeChip', () => {
  it('renders "Folders: All" when scope is empty', () => {
    render(<ScopeChip scope={{}} folders={folders} />)
    expect(screen.getByText(/Folders: All/i)).toBeInTheDocument()
  })

  it('renders "Folders: All" when scope is null', () => {
    render(<ScopeChip scope={null} folders={folders} />)
    expect(screen.getByText(/Folders: All/i)).toBeInTheDocument()
  })

  it('renders display_name when scope has one folder_id', () => {
    render(<ScopeChip scope={{ folder_ids: ['a'] }} folders={folders} />)
    expect(screen.getByText(/Contracts/i)).toBeInTheDocument()
    // No count suffix for a single folder
    expect(screen.queryByText(/\(/)).not.toBeInTheDocument()
  })

  it('renders names and count when multiple', () => {
    render(<ScopeChip scope={{ folder_ids: ['a', 'b'] }} folders={folders} />)
    expect(screen.getByText(/Contracts.*Legal.*\(2\)/)).toBeInTheDocument()
  })

  it('handles unknown folder_ids gracefully with ?', () => {
    render(<ScopeChip scope={{ folder_ids: ['unknown'] }} folders={folders} />)
    expect(screen.getByText(/Folders: \?/i)).toBeInTheDocument()
  })
})

import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { FolderPicker } from './FolderPicker'

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

describe('FolderPicker', () => {
  it('renders "Folders: All" when value is empty', () => {
    render(<FolderPicker value={[]} onChange={() => {}} folders={folders} />)
    expect(screen.getByRole('button')).toHaveTextContent('Folders: All')
  })

  it('renders display_name when one selected', () => {
    render(<FolderPicker value={['a']} onChange={() => {}} folders={folders} />)
    expect(screen.getByRole('button')).toHaveTextContent('Folders: Contracts')
  })

  it('renders count when multiple selected', () => {
    render(<FolderPicker value={['a', 'b']} onChange={() => {}} folders={folders} />)
    expect(screen.getByRole('button')).toHaveTextContent(/Folders: Contracts, Legal \(2\)/)
  })

  it('disables when there are no folders', () => {
    render(<FolderPicker value={[]} onChange={() => {}} folders={[]} />)
    expect(screen.getByRole('button')).toBeDisabled()
    expect(screen.getByRole('button')).toHaveTextContent('No folders to scope to')
  })

  it('toggling a folder calls onChange', () => {
    const onChange = vi.fn()
    render(<FolderPicker value={[]} onChange={onChange} folders={folders} />)
    // Open the popover via the trigger button (first button in the DOM)
    fireEvent.click(screen.getAllByRole('button')[0])
    fireEvent.click(screen.getByLabelText('Contracts'))
    expect(onChange).toHaveBeenCalledWith(['a'])
  })

  it('"Select all" selects every folder', () => {
    const onChange = vi.fn()
    render(<FolderPicker value={[]} onChange={onChange} folders={folders} />)
    fireEvent.click(screen.getAllByRole('button')[0])
    fireEvent.click(screen.getByText('Select all'))
    expect(onChange).toHaveBeenCalledWith(['a', 'b'])
  })

  it('"Clear" resets selection', () => {
    const onChange = vi.fn()
    render(<FolderPicker value={['a', 'b']} onChange={onChange} folders={folders} />)
    fireEvent.click(screen.getAllByRole('button')[0])
    fireEvent.click(screen.getByText('Clear'))
    expect(onChange).toHaveBeenCalledWith([])
  })

  it('search input filters the list', () => {
    render(<FolderPicker value={[]} onChange={() => {}} folders={folders} />)
    fireEvent.click(screen.getAllByRole('button')[0])
    fireEvent.change(screen.getByPlaceholderText('Search folders…'), { target: { value: 'leg' } })
    expect(screen.queryByText('Contracts')).not.toBeInTheDocument()
    expect(screen.getByText('Legal')).toBeInTheDocument()
  })

  it('disambiguates labels when two folders share display_name', () => {
    const colliding = [
      {
        folder_id: 'a',
        path: '/work/cuad/ingest',
        display_name: 'ingest',
        unavailable_reason: null,
        enabled: true,
        auto_discovered: false,
        skipped_count: 0,
        skipped_extensions: [],
      },
      {
        folder_id: 'b',
        path: '/work/qwen3-20b/cuad/ingest',
        display_name: 'ingest',
        unavailable_reason: null,
        enabled: true,
        auto_discovered: false,
        skipped_count: 0,
        skipped_extensions: [],
      },
    ]
    render(<FolderPicker value={[]} onChange={() => {}} folders={colliding} />)
    fireEvent.click(screen.getAllByRole('button')[0])
    expect(screen.getByText('cuad/ingest')).toBeInTheDocument()
    expect(screen.getByText('qwen3-20b/cuad/ingest')).toBeInTheDocument()
  })

  it('renders a title attribute with the full path on each row', () => {
    render(<FolderPicker value={[]} onChange={() => {}} folders={folders} />)
    fireEvent.click(screen.getAllByRole('button')[0])
    const row = screen.getByText('Contracts').closest('label')
    expect(row).toHaveAttribute('title', '/c')
  })

  it('uses disambiguated labels in the trigger button summary', () => {
    const colliding = [
      {
        folder_id: 'a',
        path: '/work/cuad/ingest',
        display_name: 'ingest',
        unavailable_reason: null,
        enabled: true,
        auto_discovered: false,
        skipped_count: 0,
        skipped_extensions: [],
      },
      {
        folder_id: 'b',
        path: '/work/qwen3-20b/cuad/ingest',
        display_name: 'ingest',
        unavailable_reason: null,
        enabled: true,
        auto_discovered: false,
        skipped_count: 0,
        skipped_extensions: [],
      },
    ]
    render(<FolderPicker value={['a', 'b']} onChange={() => {}} folders={colliding} />)
    expect(screen.getByRole('button')).toHaveTextContent('Folders: cuad/ingest, qwen3-20b/cuad/ingest (2)')
  })
})

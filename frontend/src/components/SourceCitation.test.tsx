import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { SourceCitation } from './SourceCitation'

describe('SourceCitation', () => {
  it('prefers SourceRef citation over the compatibility citation string', () => {
    render(
      <SourceCitation
        citation="Old citation"
        source={{
          doc_id: 'doc-1',
          doc_title: 'Contract',
          source_kind: 'document',
          source_label: 'Contract',
          citation: 'Contract, p. 4',
        }}
      />,
    )

    screen.getByText('Contract, p. 4')
    expect(screen.queryByText('Old citation')).not.toBeInTheDocument()
  })

  it('renders citation plus safe source identity', () => {
    render(
      <SourceCitation
        source={{
          doc_id: 'doc-1',
          doc_title: 'Contract',
          source_kind: 'document',
          source_label: 'Contract',
          folder_label: 'Contracts',
          relative_path: 'vendors/contract.pdf',
          citation: 'Contract, p. 4',
        }}
      />,
    )

    expect(screen.getByText('Contract, p. 4')).toBeInTheDocument()
    expect(screen.getByText('Contracts')).toBeInTheDocument()
    expect(screen.getByText('vendors/contract.pdf')).toBeInTheDocument()
  })
})

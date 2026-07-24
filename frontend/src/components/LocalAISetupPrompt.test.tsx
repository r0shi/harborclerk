import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { describe, expect, it } from 'vitest'
import { LocalAISetupPrompt } from './LocalAISetupPrompt'

function renderPrompt(variant: 'ask' | 'research') {
  render(
    <MemoryRouter>
      <LocalAISetupPrompt variant={variant} />
    </MemoryRouter>,
  )
}

describe('LocalAISetupPrompt', () => {
  it('explains the Ask setup path and preserves search as the fallback', () => {
    renderPrompt('ask')

    expect(screen.getByRole('heading', { name: 'Set up local AI for Ask' })).toBeInTheDocument()
    expect(screen.getByText(/Ask needs one downloaded and active local AI model/)).toBeInTheDocument()
    expect(screen.getByText(/Search, Documents, and Folders still work/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Choose a model' })).toHaveAttribute('href', '/settings/models')
    expect(screen.getByRole('link', { name: 'Use Search instead' })).toHaveAttribute('href', '/search')
  })

  it('uses research-specific guidance for Research', () => {
    renderPrompt('research')

    expect(screen.getByRole('heading', { name: 'Set up local AI for Research' })).toBeInTheDocument()
    expect(screen.getByText(/Research drives a local AI model through repeated searches/)).toBeInTheDocument()
    expect(screen.getByText(/Larger models are better for long synthesis/)).toBeInTheDocument()
  })
})

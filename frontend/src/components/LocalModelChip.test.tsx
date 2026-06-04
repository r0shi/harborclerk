import { render, screen } from '@testing-library/react'
import type { ComponentProps } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import { LocalModelChip } from './LocalModelChip'

function renderChip(model: ComponentProps<typeof LocalModelChip>['model']) {
  render(
    <MemoryRouter>
      <LocalModelChip model={model} />
    </MemoryRouter>,
  )
}

describe('LocalModelChip', () => {
  it('shows the active model and qualitative guidance', () => {
    renderChip({ id: 'qwen36-35b-a3b', name: 'Qwen 35B', size_bytes: 22_000_000_000, supports_research: true })

    const link = screen.getByRole('link', { name: /Local AI.*Qwen 35B.*Best local research/ })
    expect(link).toHaveAttribute('href', '/settings/models')
    expect(link).toHaveAttribute('title', expect.stringContaining('Strongest choice'))
  })

  it('marks smaller models with a warning', () => {
    renderChip({ id: 'qwen3-4b', name: 'Qwen 4B', size_bytes: 2_500_000_000, supports_research: true })

    expect(screen.getByLabelText('May miss details in long, messy, or multi-step questions.')).toBeInTheDocument()
  })
})

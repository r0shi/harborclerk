import { describe, expect, it } from 'vitest'
import { resolveSummaryState } from './SummaryChip'

describe('resolveSummaryState', () => {
  it('prefers a persisted summary over stale summarize job status', () => {
    expect(resolveSummaryState('A valid summary.', 'qwen3', 'queued')).toBe('summarized')
    expect(resolveSummaryState('A valid summary.', 'qwen3', 'running')).toBe('summarized')
    expect(resolveSummaryState('A valid summary.', 'qwen3', 'error')).toBe('summarized')
  })

  it('preserves the extractive label for persisted fallback summaries', () => {
    expect(resolveSummaryState('A fallback summary.', 'extractive', 'queued')).toBe('extractive')
  })

  it('uses summarize job status when no summary has been saved', () => {
    expect(resolveSummaryState(null, null, 'running')).toBe('generating')
    expect(resolveSummaryState('', null, 'queued')).toBe('pending')
    expect(resolveSummaryState(undefined, undefined, 'error')).toBe('failed')
  })
})

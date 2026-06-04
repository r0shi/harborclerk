export interface ModelGuidance {
  label: string
  note: string
  warning?: string
}

const MODEL_GUIDANCE: Record<string, ModelGuidance> = {
  'qwen36-35b-a3b': {
    label: 'Best local research',
    note: 'Strongest choice for longer synthesis, timelines, and multi-document questions.',
  },
  'gemma4-26b-a4b': {
    label: 'Balanced local research',
    note: 'A steady larger model for cited answers and research when memory allows.',
  },
  'gpt-oss-20b': {
    label: 'Strong tables and comparisons',
    note: 'Good fit for structured answers, comparisons, and table-heavy work.',
  },
  'qwen3-8b': {
    label: 'Lightweight lookup',
    note: 'Good starting point for quick cited answers and everyday document lookup.',
    warning: 'For longer research tasks, verify cited answers carefully.',
  },
  'qwen3-4b': {
    label: 'Smallest usable model',
    note: 'Best for compact Macs and quick lookup, not deep synthesis.',
    warning: 'May miss details in long, messy, or multi-step questions.',
  },
}

export function modelGuidance(model: { id: string; size_bytes: number; supports_research: boolean }): ModelGuidance {
  const guidance = MODEL_GUIDANCE[model.id]
  if (guidance) return guidance
  if (!model.supports_research || model.size_bytes < 4_000_000_000) {
    return {
      label: 'Quick lookup',
      note: 'Use for short cited answers; verify anything important from the sources.',
      warning: 'Not recommended for deep research.',
    }
  }
  if (model.size_bytes >= 12_000_000_000) {
    return {
      label: 'Research capable',
      note: 'A larger local model suitable for cited answers and research workflows.',
    }
  }
  return {
    label: 'General purpose',
    note: 'A mid-size local model for everyday cited answers.',
  }
}

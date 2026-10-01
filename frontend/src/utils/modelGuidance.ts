export interface ModelGuidance {
  label: string
  note: string
  warning?: string
}

// Measured results are from the three benchmark reports of 2026-09-28 and 2026-09-30 (docs/reports/
// 2026-09-28-benchmark-ix-bench-20260928-1050-keyed.md, 2026-09-30-benchmark-ix-bench-20260929-1943-synthetic.md,
// 2026-09-30-benchmark-ix-bench-20260928-2156-enron.md): all registered models, one build, on a 32 GB Mac.
const MODEL_GUIDANCE: Record<string, ModelGuidance> = {
  'qwen36-35b-a3b': {
    label: 'Best local research',
    note: 'Strongest choice for longer synthesis, timelines, and multi-document questions.',
  },
  'gemma4-12b': {
    label: 'Newer mid-size',
    note: 'A dense 12B that sits between the lightweight and the large models. In evaluation it got every fact and date right on the keyed questions (0.82 over all 20; four judged failures, all on lists) at 12 tokens a second, and did not displace GPT-OSS 20B on any corpus. Whether it stays registered is still open.',
  },
  'qwen38-27b': {
    label: 'Exact but slow',
    note: 'A dense 27B, the successor to Qwen3.6 35B-A3B. Every fact and date it was asked in evaluation was right, but every token reads all 16.5 GB of weights: on a 32 GB Mac it answers in minutes where the mixture-of-experts models take seconds. Made for a Mac with more memory bandwidth.',
    warning: 'Slow on this class of Mac: expect several minutes per cited answer.',
  },
  'gemma4-26b-a4b': {
    label: 'Balanced local research',
    note: 'A steady larger model for cited answers and research when memory allows.',
  },
  'gpt-oss-20b': {
    label: 'Strong tables and comparisons',
    note: 'Good fit for structured answers, comparisons, and table-heavy work.',
  },
  'qwen35-9b': {
    label: 'Lightweight tier',
    note: 'Replaces Qwen3 8B. In evaluation it had the best record of any model on direct questions over the synthetic corpus, and got every fact right on the keyed questions; about a third slower than Qwen3.5 4B. Its native window is longer than most Macs have memory for, so the app runs it with as much as fits.',
  },
  'qwen35-4b': {
    label: 'Pick on a 16 GB Mac',
    note: 'Replaces Qwen3 4B. In evaluation it got every fact and date right on the keyed questions, level with Gemma 4 26B-A4B at 2.7 GB and a sixth of the size: the model to pick on a 16 GB Mac. Its native window is longer than most Macs have memory for, so the app runs it with as much as fits.',
  },
  'qwen3-8b': {
    label: 'Superseded',
    note: 'Replaced by Qwen3.5 9B, which scored 0.81 to this model’s 0.43 on the keyed questions: every fact right where this model had half of them wrong. Kept so an install that has it keeps working.',
    warning: 'Superseded by Qwen3.5 9B: switch when convenient. Until then this model works as it did.',
  },
  'qwen3-4b': {
    label: 'Superseded',
    note: 'Replaced by Qwen3.5 4B, which got every fact and date right on the keyed questions; this model was not run there, and its 8B sibling of the same generation scored 0.43. Kept so an install that has it keeps working.',
    warning: 'Superseded by Qwen3.5 4B: switch when convenient. Until then this model works as it did.',
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

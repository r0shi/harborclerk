// frontend/src/utils/documentFailure.ts
//
// Failure families for a failed document (#555), as classified server-side by
// src/harbor_clerk/document_failure.py, and how each one is styled.

import type { PillState } from '../components/StatusPill'

export type ErrorFamily = 'temporary' | 'unreadable' | 'other'

export interface ErrorInfo {
  family: ErrorFamily
  message: string
  action: string
}

interface FamilyStyle {
  title: string
  pillLabel: string
  pillState: PillState
  pillGlyph?: string
  box: string
  accent: string
}

export const FAMILY_STYLES: Record<ErrorFamily, FamilyStyle> = {
  temporary: {
    title: 'Temporary problem',
    pillLabel: 'temporary error',
    // The amber "running" tint with a clock glyph: calmer than red, and not a spinner.
    pillState: 'running',
    pillGlyph: '◷',
    box: 'border-amber-200 bg-amber-50 text-amber-900 dark:border-amber-500/30 dark:bg-amber-900/15 dark:text-amber-200',
    accent: 'text-amber-700 dark:text-amber-300',
  },
  unreadable: {
    title: "This file can't be read",
    pillLabel: "can't read file",
    pillState: 'error',
    box: 'border-red-200 bg-red-50 text-red-900 dark:border-red-500/30 dark:bg-red-900/20 dark:text-red-200',
    accent: 'text-red-700 dark:text-red-300',
  },
  other: {
    title: 'Processing failed',
    pillLabel: 'failed',
    pillState: 'error',
    box: 'border-(--color-border) bg-(--color-bg-secondary) text-(--color-text-primary)',
    accent: 'text-(--color-text-secondary)',
  },
}

// Shown when a server predates error_info, or a live event delivered only the raw text.
export const UNCLASSIFIED: ErrorInfo = {
  family: 'other',
  message: 'Processing failed.',
  action: 'See the technical details below.',
}

// The compact (list-row) form shows no details, so it points to where they are.
export const UNCLASSIFIED_COMPACT: ErrorInfo = {
  ...UNCLASSIFIED,
  action: 'Open the document for the technical details.',
}

/** Status pill props for a failed document, by failure family. */
export function failurePill(info: ErrorInfo | null | undefined): {
  state: PillState
  label: string
  glyph?: string
} {
  const style = FAMILY_STYLES[info?.family ?? 'other']
  return { state: style.pillState, label: style.pillLabel, glyph: style.pillGlyph }
}

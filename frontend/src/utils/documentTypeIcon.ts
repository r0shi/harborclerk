import type { AreaHue } from '../hooks/useAreaAccent'

export interface DocLike {
  doc_type?: string | null
  mime_type?: string | null
  canonical_filename?: string | null
}

export interface DocumentIconResult {
  glyph: string
  hue: AreaHue
}

/** Pattern → icon. First match wins, so order matters (most specific first). */
const DOC_TYPE_PATTERNS: { pattern: RegExp; glyph: string; hue: AreaHue }[] = [
  // The travel-vs-guide tie: 'travel' must come before generic 'guide'.
  { pattern: /\btravel(ogue|\s+(writing|journal|guide))?\b/i, glyph: '🧭', hue: 'explore' },
  { pattern: /\b(restaurant\s+menu|menu)\b/i, glyph: '🍽', hue: 'observatory' },
  { pattern: /\b(recipe|cookbook|cooking\s+guide)\b/i, glyph: '🍳', hue: 'observatory' },
  { pattern: /\breview\b/i, glyph: '⭐', hue: 'research' },
  // Research/journal articles must be checked BEFORE the generic article pattern,
  // otherwise "Journal Article" matches `article` first and resolves to 📰 instead of 🔬.
  { pattern: /\b(research\s+paper|journal\s+article|paper)\b/i, glyph: '🔬', hue: 'docs' },
  { pattern: /\b(magazine\s+article|article|blog)\b/i, glyph: '📰', hue: 'docs' },
  { pattern: /\bmagazine\b/i, glyph: '🗞', hue: 'explore' },
  { pattern: /\b(essay|philosophical\s+dialogue|dialogue)\b/i, glyph: '🎓', hue: 'search' },
  { pattern: /\bguide\b/i, glyph: '🗺', hue: 'explore' },
  { pattern: /\b(memoir|personal\s+narrative|narrative)\b/i, glyph: '📓', hue: 'ask' },
  { pattern: /\b(contract|agreement|license|legal)\b/i, glyph: '⚖', hue: 'settings' },
  { pattern: /\b(personal\s+letter|letter)\b/i, glyph: '💌', hue: 'ask' },
  { pattern: /\b(invoice|receipt)\b/i, glyph: '🧾', hue: 'observatory' },
  // Plain "novel" / "book" — but not "cookbook" / "recipe book" (caught above).
  { pattern: /\b(novel|book)\b/i, glyph: '📕', hue: 'docs' },
  { pattern: /\bdescription\b/i, glyph: '📋', hue: 'settings' },
]

/** MIME-type → icon (used when doc_type doesn't match anything). */
const MIME_PATTERNS: { test: (mime: string) => boolean; glyph: string; hue: AreaHue }[] = [
  { test: (m) => m === 'application/pdf', glyph: '📄', hue: 'ask' },
  { test: (m) => m.includes('spreadsheet') || m === 'text/csv', glyph: '📊', hue: 'observatory' },
  { test: (m) => m === 'message/rfc822', glyph: '✉', hue: 'research' },
  { test: (m) => m.includes('presentation'), glyph: '🎞', hue: 'explore' },
  { test: (m) => m.startsWith('audio/'), glyph: '🎙', hue: 'ask' },
  { test: (m) => m.startsWith('image/'), glyph: '🖼', hue: 'search' },
  { test: (m) => m === 'text/html', glyph: '🌐', hue: 'explore' },
  { test: (m) => m.startsWith('text/x-') || m === 'application/json', glyph: '💻', hue: 'explore' },
  { test: (m) => m.startsWith('text/'), glyph: '📜', hue: 'settings' },
]

/** Filename-extension → icon (final fallback when both above miss). */
const EXT_PATTERNS: { exts: string[]; glyph: string; hue: AreaHue }[] = [
  { exts: ['pdf'], glyph: '📄', hue: 'ask' },
  { exts: ['xlsx', 'xls', 'csv', 'numbers'], glyph: '📊', hue: 'observatory' },
  { exts: ['eml', 'msg', 'mbox'], glyph: '✉', hue: 'research' },
  { exts: ['pptx', 'ppt', 'key'], glyph: '🎞', hue: 'explore' },
  { exts: ['mp3', 'wav', 'm4a', 'flac', 'aac', 'ogg'], glyph: '🎙', hue: 'ask' },
  { exts: ['png', 'jpg', 'jpeg', 'gif', 'webp', 'tiff', 'bmp', 'heic'], glyph: '🖼', hue: 'search' },
  { exts: ['html', 'htm'], glyph: '🌐', hue: 'explore' },
  {
    exts: ['py', 'js', 'ts', 'tsx', 'jsx', 'rb', 'go', 'rs', 'c', 'h', 'cpp', 'java', 'json'],
    glyph: '💻',
    hue: 'explore',
  },
  { exts: ['txt', 'md', 'log', 'rtf'], glyph: '📜', hue: 'settings' },
]

const FALLBACK: DocumentIconResult = { glyph: '📦', hue: 'settings' }

/**
 * Resolve a document to an icon glyph + area hue.
 *
 * Resolution order:
 *   1. doc_type (LLM-derived, free-form — case-insensitive substring match).
 *   2. mime_type.
 *   3. canonical_filename extension.
 *   4. Fallback: 📦 (slate).
 *
 * The doc_type list and coverage rationale are documented in the
 * 2026-05-03 spec ("Coverage audit" section).
 */
export function documentTypeIcon(doc: DocLike): DocumentIconResult {
  if (doc.doc_type) {
    const dt = doc.doc_type
    for (const { pattern, glyph, hue } of DOC_TYPE_PATTERNS) {
      if (pattern.test(dt)) return { glyph, hue }
    }
  }

  if (doc.mime_type) {
    const mime = doc.mime_type.toLowerCase()
    for (const { test, glyph, hue } of MIME_PATTERNS) {
      if (test(mime)) return { glyph, hue }
    }
  }

  if (doc.canonical_filename) {
    const dot = doc.canonical_filename.lastIndexOf('.')
    const ext = dot >= 0 ? doc.canonical_filename.slice(dot + 1).toLowerCase() : ''
    if (ext) {
      for (const { exts, glyph, hue } of EXT_PATTERNS) {
        if (exts.includes(ext)) return { glyph, hue }
      }
    }
  }

  return FALLBACK
}

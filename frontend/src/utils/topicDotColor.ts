import type { AreaHue } from '../hooks/useAreaAccent'

const HUES: AreaHue[] = ['ask', 'research', 'folders', 'docs', 'explore', 'search', 'observatory', 'settings']

/**
 * Deterministic-hash-of-id → one of the eight area-accent hues.
 *
 * Used by the conversation list (Ask + Research sidebars) so each
 * conversation gets a stable colored dot. v1 derivation per the
 * 2026-05-03 spec: hash of conversation_id mod 8. Smarter "color by
 * dominant topic / top-cited entity" is a deferred follow-up.
 */
export function topicDotColor(id: string): AreaHue {
  // FNV-1a, 32-bit. Deterministic, low collision, no deps.
  let hash = 0x811c9dc5
  for (let i = 0; i < id.length; i++) {
    hash ^= id.charCodeAt(i)
    hash = Math.imul(hash, 0x01000193) >>> 0
  }
  return HUES[hash % HUES.length]
}

/** Convenience: returns the CSS var() reference for the hue's accent value. */
export function topicDotVar(id: string): string {
  return `var(--area-${topicDotColor(id)}-accent)`
}

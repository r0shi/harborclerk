// An API key's snippet cap (max_snippet_chars): null = no cap, N >= 0 = the first N
// characters of the passage and page text the key receives, none at 0. The server
// rejects a negative (src/harbor_clerk/api/schemas/api_keys.py, #823).

export type SnippetCapParse = { ok: true; value: number | null } | { ok: false; error: string }

export const SNIPPET_CAP_ERROR = 'Max snippet chars must be a whole number of 0 or more (empty = no limit).'

/** The input's raw string as a cap: '' is no cap (null), "0" is 0, a negative or a fraction is an error. */
export function parseSnippetCap(raw: string): SnippetCapParse {
  const trimmed = raw.trim()
  if (trimmed === '') return { ok: true, value: null }
  const n = Number(trimmed)
  if (!Number.isInteger(n) || n < 0) return { ok: false, error: SNIPPET_CAP_ERROR }
  return { ok: true, value: n }
}

/** The form's initial string for a stored cap. A legacy negative is shown as 0, the cap the server applies. */
export function snippetCapInput(stored: number | null): string {
  return stored == null ? '' : String(Math.max(0, stored))
}

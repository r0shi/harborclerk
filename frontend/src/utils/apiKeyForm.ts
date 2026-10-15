// Parsing for the API key form's numeric fields. Each takes the input's raw string: the
// inputs are type="text" (inputMode="numeric"), because a type="number" input reports any
// text it cannot parse ("5e", "50-", or a typo such as "5OO" in Safari/WKWebView) as "",
// which would read as "no limit" and silently remove a cap.
//
// max_snippet_chars: null = no cap, N >= 0 = the first N characters of the passage and page
// text the key receives, none at 0 (#823). Rate limits: null = the system default, 0 =
// unlimited, N >= 1 requests. The server rejects anything outside 0..2^31-1, the range of
// its integer columns (src/harbor_clerk/api/schemas/api_keys.py).

export const INT32_MAX = 2 ** 31 - 1

export type NumberFieldParse = { ok: true; value: number | null } | { ok: false; error: string }

export const SNIPPET_CAP_ERROR = `Max snippet chars must be a whole number from 0 to ${INT32_MAX} (empty = no limit).`

function parseWholeNumber(raw: string, error: string): NumberFieldParse {
  const trimmed = raw.trim()
  if (trimmed === '') return { ok: true, value: null }
  if (!/^\d+$/.test(trimmed)) return { ok: false, error }
  const n = Number(trimmed)
  if (n > INT32_MAX) return { ok: false, error }
  return { ok: true, value: n }
}

/** '' is no cap (null), "0" is 0; a negative, a fraction, junk or a value past 2^31-1 is an error. */
export function parseSnippetCap(raw: string): NumberFieldParse {
  return parseWholeNumber(raw, SNIPPET_CAP_ERROR)
}

/** '' is the system default (null), "0" is unlimited; otherwise a whole number of requests. */
export function parseRateLimit(raw: string, label: string): NumberFieldParse {
  return parseWholeNumber(raw, `${label} must be a whole number from 0 to ${INT32_MAX} (empty = system default).`)
}

/** The form's initial string for a stored cap. A legacy negative is shown as 0, the cap the server applies. */
export function snippetCapInput(stored: number | null): string {
  return stored == null ? '' : String(Math.max(0, stored))
}

"""SQL escape helpers.

Centralized so all sites that build pattern strings use the same
implementation. Adding a new ILIKE pattern site? Import escape_ilike here.
"""

from __future__ import annotations

import re

# Characters with special meaning in PostgreSQL ILIKE patterns:
#   %  - matches any sequence of zero or more characters
#   _  - matches any single character
#   \  - escape character itself
# The default escape character is backslash; we escape each with a backslash
# so the resulting string is safe to embed inside `%...%` (or any pattern).
_ILIKE_METACHAR_RE = re.compile(r"([%_\\])")


def escape_ilike(value: str) -> str:
    """Return `value` with ILIKE metacharacters (%, _, \\) escaped.

    Use whenever building an ILIKE pattern from user-supplied input:

        escaped = escape_ilike(query)
        stmt = stmt.where(Document.title.op("ILIKE")(f"%{escaped}%"))

    Idempotent on inputs without metacharacters (returns the input unchanged).
    """
    return _ILIKE_METACHAR_RE.sub(r"\\\1", value)

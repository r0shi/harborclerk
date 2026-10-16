"""The per-key snippet cap: one rule for every passage of corpus text a key receives (#823).

An API key's ``max_snippet_chars`` bounds passage and page text on every surface
(MCP tools, the CLI that speaks MCP, and the REST routes in
``api.scope.REST_ROUTE_TOOLS``): a passage is the chunk's (or page's) first
``cap`` characters, never a window positioned within it (on a search term or an
offset), and a whole-document response is the first ``cap`` characters of the
requested range. The cap is per passage, not per document: a caller chooses the
chunks and pages it reads, so it can receive the first ``cap`` characters of each. A cap of 0 —
or a negative value stored before create/update validated it — returns no
passage text (None). Humans, OAuth agents and keys with no cap are unaffected.

Titles, summaries, metadata, entity strings and headings are not passage text
and are not capped; nor is the fact that a ``text_contains`` filter matched,
which tells a caller whether a literal occurs in a chunk.

This module sits below the API layer so that ``search.py`` can use it.
"""

from typing import Protocol


class _CapScope(Protocol):
    max_snippet_chars: int | None


class _CappedPrincipal(Protocol):
    @property
    def key_scope(self) -> _CapScope | None: ...


def snippet_cap(principal: _CappedPrincipal) -> int | None:
    """The calling key's snippet cap, clamped at 0; None when no cap applies."""
    scope = principal.key_scope
    if scope is None or scope.max_snippet_chars is None:
        return None
    return max(0, scope.max_snippet_chars)


def cap_text(text: str, cap: int | None) -> str | None:
    """``text`` under a snippet cap: unchanged when ``cap`` is None, else its
    first ``cap`` characters, or None (no text) when ``cap`` is 0 or less."""
    if cap is None:
        return text
    return text[:cap] if cap > 0 else None


def cap_passage_text(text: str, principal: _CappedPrincipal) -> str | None:
    """A passage of corpus text as ``principal`` may receive it (see ``cap_text``)."""
    return cap_text(text, snippet_cap(principal))

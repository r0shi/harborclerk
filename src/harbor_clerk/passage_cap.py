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

The cap bounds the document's text, not only passages (#831): summaries and
metadata string values take the same prefix rule (``payload_bounds``), a
capped key's ``text_contains`` matches only within each chunk's first ``cap``
characters (``text_contains.text_contains_clause``), and entity strings come
without their ``start_char``/``end_char``. Titles, headings and entity strings
themselves are not capped.

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


class SnippetCapRefusal(ValueError):
    """A filter a capped key may not send: it would test text the key cannot read (#831)."""


def _filter_strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _filter_strings(v)]
    if isinstance(value, list | tuple):
        return [s for v in value for s in _filter_strings(v)]
    return []


def refuse_filter_past_cap(metadata_filter: dict | None, cap: int | None) -> None:
    """Raise ``SnippetCapRefusal`` for a capped key's ``metadata_filter`` string longer than its cap.

    A filter is an exact match on the whole stored value, so a value of at most ``cap`` characters
    can only match a stored value the key already sees whole (its first ``cap`` characters are all
    of it); a longer one would confirm a guessed value the key was never shown. At a cap of 0 the
    key sees no metadata text, and every string filter is refused. Numbers and booleans pass.
    """
    if cap is None or not metadata_filter:
        return
    for path, value in metadata_filter.items():
        for text in _filter_strings(value):
            if cap <= 0:
                raise SnippetCapRefusal(
                    f"metadata_filter {path!r} is refused: this API key's snippet cap is 0, so it receives no "
                    "metadata text and may not filter on it."
                )
            if len(text) > cap:
                raise SnippetCapRefusal(
                    f"metadata_filter {path!r} is refused: its value is {len(text)} characters and this API key's "
                    f"snippet cap is {cap}; a filter value may not be longer than the text the key can read."
                )

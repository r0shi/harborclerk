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

The cap bounds the text a key receives, and the yes/no filters answer only about
what it can see (#831): summaries and metadata string values take the same prefix
rule (``payload_bounds``; ISO dates in the date fields excepted, as structured
data), a capped key's ``text_contains`` matches only within each chunk's first
``cap`` characters (``text_contains.text_contains_clause``), a ``metadata_filter``
string longer than the cap is refused (``refuse_filter_past_cap``: ISO dates pass;
of ``email.*`` only the cited sender name and subject pass), and entity strings come without their ``start_char``/``end_char``. Ranked
search still matches its query against the whole document, so a phrase query can
indicate that words occur past the cap. Titles, headings and entity strings are not
capped.

This module sits below the API layer so that ``search.py`` can use it.
"""

import re
from datetime import datetime
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


# The metadata date fields kb_documents_by_date sorts, filters and reports on (``metadata_dates``).
# A value sitting directly on one (the scalar, or an element of a list there; never a dict child) that
# is a strict ISO 8601 date or datetime is structured data, not document text: the snippet cap leaves
# it whole in a payload (``payload_bounds``), lets a filter on it through (``refuse_filter_past_cap``),
# and is the only value the date tool uses under a cap (``mcp_lookup_tools``). Anything else stored
# under these keys is text and is capped like any other string.
DATE_FIELD_PATHS = frozenset({"tika.created_at", "frontmatter.date", "sidecar.date"})

# The strict form, one pattern for Python (``re.fullmatch``) and Postgres (``~ '^(?:...)$'``, with
# ``pg_input_is_valid`` for the calendar): YYYY-MM-DD, optionally a time after "T" or a space
# (HH:MM, :SS, up to six fractional digits) and a "Z" or ±HH:MM offset. Hours, minutes, seconds and
# offsets are bounded where Python and Postgres would otherwise disagree (24:00, a leap second 60,
# offsets past ±15:59). ``tests/test_key_snippet_cap.py`` holds the two sides in agreement.
ISO_DATE_PATTERN = (
    r"\d{4}-\d{2}-\d{2}"
    r"(?:[T ](?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d(?:\.\d{1,6})?)?(?:Z|[+-](?:0\d|1[0-5]):[0-5]\d)?)?"
)
_ISO_DATE_RE = re.compile(ISO_DATE_PATTERN)


def date_path_of(segments: tuple[str | int, ...]) -> str | None:
    """The date field a value sits directly on, or None.

    ``segments`` is the value's path: dict keys and list indices, outermost first, a key possibly a
    dotted ``ns.key`` itself (``("tika", "created_at")``, ``("tika.created_at",)``, or with trailing list
    indices for a list element). A dict child (``("tika", "created_at", "k")``) is not on the path."""
    keys = list(segments)
    while keys and isinstance(keys[-1], int):
        keys.pop()
    if not keys or not all(isinstance(k, str) for k in keys):
        return None
    path = ".".join(keys)  # type: ignore[arg-type]
    return path if path in DATE_FIELD_PATHS else None


def is_structured_date(path: str | None, value: object) -> bool:
    """True when ``value`` on the date field ``path`` (from ``date_path_of``) is a strict ISO date."""
    if path is None or path not in DATE_FIELD_PATHS or not isinstance(value, str):
        return False
    if _ISO_DATE_RE.fullmatch(value) is None:
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


# The email.* filters on header fields every mail citation carries whole (``source_ref``: the display
# sender and the subject). The other email.* filters test fields a citation does not always show (to,
# cc, message id, thread id; the address only when there is no display name), To and Cc also opening
# chunk 0 as passage text, so a capped key may not send them at all.
CITED_EMAIL_FILTERS = frozenset(
    {"email.from_name", "email.from_name_contains", "email.subject", "email.subject_contains"}
)


class SnippetCapRefusal(ValueError):
    """A filter a capped key may not send: it would test text the key cannot read (#831)."""


def _filter_leaves(value: object, position: tuple[str | int, ...] = ()) -> list[tuple[tuple[str | int, ...], object]]:
    """Every scalar in a filter value (None included), with its position under the filter's key."""
    if isinstance(value, dict):
        return [leaf for k, v in value.items() for leaf in _filter_leaves(v, (*position, str(k)))]
    if isinstance(value, list | tuple):
        return [leaf for i, v in enumerate(value) for leaf in _filter_leaves(v, (*position, i))]
    return [(position, value)]


def refuse_summary_state_at_cap_zero(summary_state: str | None, cap: int | None) -> None:
    """Raise ``SnippetCapRefusal`` for any ``summary_state`` filter from a key capped at 0.

    At a cap of 0 every summary shows as null, so filtering on whether one exists (or on its job's
    state) would tell the key which nulls hide a real summary. Above 0 the summary's prefix is shown,
    so the filter tells it nothing new."""
    if summary_state and cap is not None and cap <= 0:
        raise SnippetCapRefusal(
            "summary_state is refused: this API key's snippet cap is 0, so every summary shows as null to it, "
            "and filtering on whether one exists would tell a real summary from a hidden one."
        )


def refuse_filter_past_cap(metadata_filter: dict | None, cap: int | None) -> None:
    """Raise ``SnippetCapRefusal`` for a capped key's ``metadata_filter`` string longer than its cap.

    A filter is an exact match on the whole stored value, so a value of at most ``cap`` characters
    can only match a stored value the key already sees whole (its first ``cap`` characters are all
    of it); a longer one would confirm a guessed value the key was never shown. At a cap of 0 the
    key sees no metadata text, and every string filter is refused, and so is a null: every string
    shows as null at 0, so matching a real null would tell it apart from a hidden string. Numbers and
    booleans pass.

    Two exceptions follow what the key already receives whole. A strict ISO date directly on a date
    field (``date_path_of``, ``is_structured_date``) passes, as structured data. Of the ``email.*``
    keys, which filter the ``Document.email_*`` header columns, only ``CITED_EMAIL_FILTERS`` (display
    sender, subject) pass, since every mail citation carries them; every other ``email.*`` key (to,
    cc, message id, thread id, from address) is refused at any cap, whatever its value.
    """
    if cap is None or not metadata_filter:
        return
    for path, value in metadata_filter.items():
        if path in CITED_EMAIL_FILTERS:
            continue
        if path.startswith("email."):
            raise SnippetCapRefusal(
                f"metadata_filter {path!r} is refused: this API key has a snippet cap, and it may not filter on a "
                "mail header that citations do not always show. Filter mail by email.from_name or email.subject."
            )
        for position, leaf in _filter_leaves(value):
            if leaf is None:
                if cap <= 0:
                    raise SnippetCapRefusal(
                        f"metadata_filter {path!r} is refused: this API key's snippet cap is 0, so every metadata "
                        "string shows as null to it, and a null filter would tell a real null from hidden text."
                    )
                continue
            if not isinstance(leaf, str):
                continue
            if is_structured_date(date_path_of((path, *position)), leaf):
                continue
            if cap <= 0:
                raise SnippetCapRefusal(
                    f"metadata_filter {path!r} is refused: this API key's snippet cap is 0, so it receives no "
                    "metadata text and may not filter on it."
                )
            if len(leaf) > cap:
                raise SnippetCapRefusal(
                    f"metadata_filter {path!r} is refused: its value is {len(leaf)} characters and this API key's "
                    f"snippet cap is {cap}; a filter value may not be longer than the text the key can read."
                )

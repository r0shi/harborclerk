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
a null at cap 0 is refused, since whether a field holds hidden text is content),
and entity strings
come without their ``start_char``/``end_char``. Ranked search still matches its
query against the whole document, so a phrase query can indicate that words occur
past the cap. Titles, headings and entity strings are not capped. Pipeline state is
not text: under any cap a key may learn that a summary exists (``summary_state``,
``summary_model``, the summarize job's status), never its text past the cap. Mail
headers (sender, recipients, cc, subject, message and thread ids, date) are envelope
fields, like a title, outside the cap: every ``email.*`` filter passes. The header
block at the start of a message's first passage is passage text, and is capped.

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
# it whole in a payload (``payload_bounds``) and lets a filter on it through (``refuse_filter_past_cap``),
# list elements included. The date tool (``mcp_lookup_tools``) reads only a scalar date at the path,
# never a list, and under a cap only a strict one. Anything else stored under these keys is text and is
# capped like any other string.
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


# Tika's mail-header metadata (``ingest/metadata_extractors/tika_metadata.py``), which citations fall
# back to for messages without the ``Document.email_*`` columns (``source_ref._email_fields_for_doc``).
# Mail headers are envelope fields outside the snippet cap (owner's decision on #837): a value directly
# on one of these is never cut and never refused as a filter, like the ``email.*`` columns.
MAIL_HEADER_FIELD_PATHS = frozenset(
    {"tika.email_from", "tika.email_to", "tika.email_cc", "tika.email_subject", "tika.email_date"}
)


def field_path_of(segments: tuple[str | int, ...]) -> str | None:
    """The dotted metadata field a value sits directly on (the scalar, or an element of a list there),
    or None for a dict child or a path through a list.

    ``segments`` is the value's path: dict keys and list indices, outermost first, a key possibly a
    dotted ``ns.key`` itself (``("tika", "created_at")``, ``("tika.created_at",)``, or with trailing list
    indices for a list element). A dict child (``("tika", "created_at", "k")``) is ``tika.created_at.k``."""
    keys = list(segments)
    while keys and isinstance(keys[-1], int):
        keys.pop()
    if not keys or not all(isinstance(k, str) for k in keys):
        return None
    return ".".join(keys)  # type: ignore[arg-type]


def date_path_of(segments: tuple[str | int, ...]) -> str | None:
    """The date field a value sits directly on (``field_path_of`` in ``DATE_FIELD_PATHS``), or None."""
    path = field_path_of(segments)
    return path if path in DATE_FIELD_PATHS else None


def is_mail_header(segments: tuple[str | int, ...]) -> bool:
    """True when a value sits directly on one of ``MAIL_HEADER_FIELD_PATHS``."""
    return field_path_of(segments) in MAIL_HEADER_FIELD_PATHS


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


class SnippetCapRefusal(ValueError):
    """A filter a capped key may not send: it would test text the key cannot read (#831)."""


def _filter_leaves(value: object, position: tuple[str | int, ...] = ()) -> list[tuple[tuple[str | int, ...], object]]:
    """Every scalar in a filter value (None included), with its position under the filter's key."""
    if isinstance(value, dict):
        return [leaf for k, v in value.items() for leaf in _filter_leaves(v, (*position, str(k)))]
    if isinstance(value, list | tuple):
        return [leaf for i, v in enumerate(value) for leaf in _filter_leaves(v, (*position, i))]
    return [(position, value)]


def refuse_filter_past_cap(metadata_filter: dict | None, cap: int | None) -> None:
    """Raise ``SnippetCapRefusal`` for a capped key's ``metadata_filter`` string longer than its cap.

    A filter is an exact match on the whole stored value, so a value of at most ``cap`` characters
    can only match a stored value the key already sees whole (its first ``cap`` characters are all
    of it); a longer one would confirm a guessed value the key was never shown. At a cap of 0 the
    key sees no metadata text, and every string filter is refused, and so is a null: every string
    shows as null at 0, so matching a real null would tell it apart from a hidden string. Numbers and
    booleans pass.

    Two exceptions. A strict ISO date directly on a date field (``date_path_of``,
    ``is_structured_date``) passes, as structured data. Every ``email.*`` key passes, at any cap and
    length: mail headers (sender, recipients, cc, subject, message and thread ids, date) are envelope
    fields in the ``Document.email_*`` columns, like a title, not document text (owner's decision).
    """
    if cap is None or not metadata_filter:
        return
    for path, value in metadata_filter.items():
        if path.startswith("email."):
            continue  # a mail header: an envelope field, outside the cap
        for position, leaf in _filter_leaves(value):
            if is_mail_header((path, *position)):
                continue  # a Tika mail header: an envelope field, outside the cap
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

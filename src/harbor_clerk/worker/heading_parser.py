"""Parse heading hierarchy from Tika XHTML output."""

import re
from html.parser import HTMLParser
from typing import NamedTuple

_HEADING_RE = re.compile(r"^(?:\w+:)?h([1-6])$", re.IGNORECASE)


class Heading(NamedTuple):
    level: int
    title: str
    position: int  # char offset for ordering


class _HeadingExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.headings: list[Heading] = []
        self._char_offset = 0
        self._in_heading = False
        self._heading_level = 0
        self._heading_start = 0
        self._heading_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        m = _HEADING_RE.match(tag)
        if m:
            self._in_heading = True
            self._heading_level = int(m.group(1))
            self._heading_start = self._char_offset
            self._heading_parts = []

    def handle_endtag(self, tag: str) -> None:
        if self._in_heading and _HEADING_RE.match(tag):
            title = " ".join("".join(self._heading_parts).split())
            if title:
                self.headings.append(
                    Heading(
                        level=self._heading_level,
                        title=title,
                        position=self._heading_start,
                    )
                )
            self._in_heading = False
            self._heading_level = 0
            self._heading_parts = []

    def handle_data(self, data: str) -> None:
        if self._in_heading:
            self._heading_parts.append(data)
        self._char_offset += len(data)


def parse_headings_from_xhtml(xhtml: str) -> list[Heading]:
    """Extract headings from Tika XHTML output.

    Handles Tika's namespace-prefixed tags (e.g. ``<html:h1>``).
    Returns headings ordered by their position in the document.
    """
    if not xhtml:
        return []
    parser = _HeadingExtractor()
    parser.feed(xhtml)
    return parser.headings

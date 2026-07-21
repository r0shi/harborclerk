"""Marker-block machinery for generated documentation.

Generated content lives inside named marker blocks so that hand-written prose
can wrap it in place:

    <!-- BEGIN GENERATED: mcp-tools -->
    ...generated...
    <!-- END GENERATED: mcp-tools -->

Everything outside the markers is owned by a human and never touched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

BEGIN = "<!-- BEGIN GENERATED: {name} -->"
END = "<!-- END GENERATED: {name} -->"

_BEGIN_RE = re.compile(r"^[ \t]*<!--[ \t]*BEGIN GENERATED:[ \t]*(?P<name>[\w.-]+)[ \t]*-->[ \t]*$", re.M)
_END_RE = re.compile(r"^[ \t]*<!--[ \t]*END GENERATED:[ \t]*(?P<name>[\w.-]+)[ \t]*-->[ \t]*$", re.M)

DO_NOT_EDIT = "<!-- Do not edit by hand. Regenerate: uv run python -m scripts.gen_docs -->"


class BlockError(ValueError):
    """A marker block is malformed, duplicated, or unterminated."""


@dataclass(frozen=True)
class Block:
    """A located marker block. Offsets are into the source text."""

    name: str
    body_start: int  # first char after the BEGIN marker line
    body_end: int  # first char of the END marker line
    outer_start: int  # first char of the BEGIN marker line
    outer_end: int  # first char after the END marker line

    def body(self, text: str) -> str:
        return text[self.body_start : self.body_end]


def find_blocks(text: str) -> list[Block]:
    """Locate every marker block, in document order.

    Raises BlockError on unterminated, mismatched, overlapping, or duplicate
    blocks — silent misbehaviour here would mean silently ungenerated docs.
    """
    begins = list(_BEGIN_RE.finditer(text))
    ends = list(_END_RE.finditer(text))

    if len(begins) != len(ends):
        raise BlockError(f"{len(begins)} BEGIN marker(s) but {len(ends)} END marker(s)")

    blocks: list[Block] = []
    for begin, end in zip(begins, ends, strict=True):
        if begin.group("name") != end.group("name"):
            raise BlockError(f"marker mismatch: BEGIN {begin.group('name')!r} closed by END {end.group('name')!r}")
        if end.start() < begin.end():
            raise BlockError(f"END marker precedes BEGIN marker for block {begin.group('name')!r}")
        blocks.append(
            Block(
                name=begin.group("name"),
                body_start=begin.end() + 1 if text[begin.end() : begin.end() + 1] == "\n" else begin.end(),
                body_end=end.start(),
                outer_start=begin.start(),
                outer_end=end.end(),
            )
        )

    seen = [b.name for b in blocks]
    dupes = {n for n in seen if seen.count(n) > 1}
    if dupes:
        raise BlockError(f"duplicate block name(s) in one file: {sorted(dupes)}")

    for earlier, later in zip(blocks, blocks[1:], strict=False):
        if later.outer_start < earlier.outer_end:
            raise BlockError(f"blocks {earlier.name!r} and {later.name!r} overlap or nest")

    return blocks


def render_body(content: str) -> str:
    """Wrap generated content in the standard body form.

    Always exactly one blank line between the do-not-edit notice, the content,
    and the closing marker, so regeneration is byte-stable.
    """
    return f"{DO_NOT_EDIT}\n\n{content.strip()}\n"


def replace_block(text: str, name: str, content: str) -> str:
    """Return `text` with block `name`'s body replaced by rendered `content`."""
    for block in find_blocks(text):
        if block.name == name:
            return text[: block.body_start] + render_body(content) + text[block.body_end :]
    raise BlockError(f"no block named {name!r} in this document")

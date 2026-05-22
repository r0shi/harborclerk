"""Markdown-aware extraction: frontmatter, structure (headings + code fences),
light normalization, and the ``extract_markdown`` orchestrator that ``run_extract``
calls for files whose extension is in ``MARKDOWN_EXTENSIONS``.

Each helper is a pure function so it can be tested without database state.
``extract_markdown`` composes them.
"""

import re

import yaml
from markdown_it import MarkdownIt

# Frontmatter must be at the very top of the document. It opens on a `---` line
# and closes on the next `---` line. Captures the YAML block in group 1 and the
# body (everything after the closing fence) in group 2.
# The pattern uses a lazy `(.*?)` so the YAML content can be empty (``---\n---``)
# or non-empty; the closing ``---`` is matched without requiring a preceding ``\n``
# so that an empty block (zero YAML lines) is handled correctly.
_FRONTMATTER_RE = re.compile(
    r"\A---\r?\n(.*?)---\r?\n?(.*)\Z",
    flags=re.DOTALL,
)


def extract_frontmatter(text: str) -> tuple[dict, str]:
    """Split off a leading YAML frontmatter block.

    Returns ``(frontmatter_dict, body)``. If the text has no leading
    ``---``-delimited block, or if the block is not valid YAML, returns
    ``({}, text)`` — the original text unchanged.
    """
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text

    raw_block, body = match.group(1), match.group(2)
    try:
        parsed = yaml.safe_load(raw_block)
    except yaml.YAMLError:
        return {}, text

    # Empty block (``---\n---``) parses as ``None``. Treat as empty dict but
    # still strip the block from the body.
    if parsed is None:
        return {}, body
    if not isinstance(parsed, dict):
        return {}, text  # arrays-at-top etc. are not Obsidian frontmatter
    return parsed, body


def _is_scalar(value) -> bool:
    return isinstance(value, (str, int, float, bool))


def flatten_frontmatter(fm: dict) -> str:
    """Render frontmatter fields as a readable single-paragraph preamble.

    Scalar values become ``Key: value.`` Lists of scalars become
    ``Key: v1, v2, v3.`` Nested structures, ``None``, and empty lists are
    skipped. The ``title`` key is skipped (the orchestrator uses it to
    override ``documents.title``).

    Returns an empty string when nothing emits.
    """
    parts: list[str] = []
    for key, value in fm.items():
        if key == "title":
            continue
        if value is None:
            continue
        if isinstance(value, list):
            if not value or not all(_is_scalar(v) for v in value):
                continue
            rendered = ", ".join(str(v) for v in value)
        elif _is_scalar(value):
            rendered = str(value)
        else:
            continue  # nested dict / other non-flat structure
        label = key[:1].upper() + key[1:]
        parts.append(f"{label}: {rendered}.")
    return " ".join(parts)


def _line_start_offsets(text: str) -> list[int]:
    """Char offset where each line begins. ``offsets[i]`` is the start of
    line ``i`` (0-indexed); ``offsets[-1]`` equals ``len(text)``.
    """
    offsets = [0]
    pos = 0
    for line in text.splitlines(keepends=True):
        pos += len(line)
        offsets.append(pos)
    return offsets


def parse_markdown_structure(text: str) -> tuple[list[dict], list[tuple[int, int]]]:
    """Parse Markdown once and return ``(headings, fence_line_ranges)``.

    ``headings`` is a list of dicts ``{"level": int, "title": str, "position": int}``
    where ``position`` is the character offset of the heading's first character in
    ``text`` (the same shape ``_extract_headings_via_tika`` produces, minus the
    page_num field which the orchestrator fills in later).

    ``fence_line_ranges`` is a list of inclusive ``(start_line, end_line)`` tuples
    (0-indexed) covering each fenced code block's opening fence, content, and
    closing fence.
    """
    if not text:
        return [], []

    md = MarkdownIt()
    tokens = md.parse(text)
    line_starts = _line_start_offsets(text)

    headings: list[dict] = []
    fences: list[tuple[int, int]] = []

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.type == "heading_open" and tok.tag and tok.tag[0] == "h":
            level = int(tok.tag[1:])
            start_line = tok.map[0] if tok.map else 0
            position = line_starts[start_line] if start_line < len(line_starts) else 0
            title = ""
            if i + 1 < len(tokens) and tokens[i + 1].type == "inline":
                inline = tokens[i + 1]
                if inline.children:
                    # Concatenate only text-node children, so e.g. `# **Bold** Title`
                    # produces "Bold Title" (formatting markers are stripped).
                    title = "".join(c.content for c in inline.children if c.type == "text").strip()
                else:
                    title = inline.content.strip()
            if title:
                headings.append({"level": level, "title": title, "position": position})
        elif tok.type == "fence" and tok.map:
            # markdown-it's token.map = [start_line, end_line_exclusive].
            # Convert to inclusive end line.
            start_line, end_line_excl = tok.map
            fences.append((start_line, end_line_excl - 1))
        i += 1

    return headings, fences

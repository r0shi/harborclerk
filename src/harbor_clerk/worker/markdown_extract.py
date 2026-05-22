"""Markdown-aware extraction: frontmatter, structure (headings + code fences),
light normalization, and the ``extract_markdown`` orchestrator that ``run_extract``
calls for files whose extension is in ``MARKDOWN_EXTENSIONS``.

Each helper is a pure function so it can be tested without database state.
``extract_markdown`` composes them.
"""

import re

import yaml

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

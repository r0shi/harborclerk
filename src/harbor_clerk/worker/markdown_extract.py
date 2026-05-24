"""Markdown-aware extraction: frontmatter, structure (headings + code fences),
light normalization, and the ``extract_markdown`` orchestrator that ``run_extract``
calls for files whose extension is in ``MARKDOWN_EXTENSIONS``.

Each helper is a pure function so it can be tested without database state.
``extract_markdown`` composes them.
"""

import re
from dataclasses import dataclass

import yaml
from markdown_it import MarkdownIt

from harbor_clerk.config import get_settings
from harbor_clerk.worker.text_pagination import paginate_text

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


def strip_frontmatter(text: str) -> str:
    """Remove the leading YAML frontmatter block from markdown text.

    If the text doesn't start with ``---\\n`` or the frontmatter YAML is
    malformed, return the text unchanged — don't risk losing content on a
    parse error.  The FrontmatterExtractor in
    ``src/harbor_clerk/ingest/metadata_extractors/frontmatter.py`` already
    captures those fields into ``Document.doc_metadata.frontmatter``, so
    stripping the block from the body text is pure noise reduction: raw
    ``tags: [foo, bar]`` lines muddying FTS and semantic similarity.

    The leading blank line that the regex leaves between the closing ``---``
    and the first body content is also stripped so callers get clean text.
    """
    fm, body = extract_frontmatter(text)
    # extract_frontmatter returns the literal `text` argument on no-match /
    # malformed / not-a-dict paths, and a fresh stripped slice on the
    # delimiter-stripped paths. Identity comparison distinguishes them: if
    # `body is text`, nothing was stripped (pass through unchanged); else
    # the helper already stripped the YAML block (lstrip leading newlines).
    if body is text:
        return text
    return body.lstrip("\n")


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


# Wikilink: [[Target]], [[Target#Anchor]], [[Target|Alias]], [[Target#Anchor|Alias]].
# Group 1 = target, Group 2 = alias (optional).
_WIKILINK_RE = re.compile(r"\[\[([^\[\]|#]+?)(?:#[^\[\]|]+)?(?:\|([^\[\]]+))?\]\]")
# Inline link: [text](url). Group 1 = text. URLs without spaces.
_INLINE_LINK_RE = re.compile(r"\[([^\[\]]+?)\]\(([^()\s]+)\)")
# ATX heading at line start: 1-6 `#` then a space then the title.
_ATX_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
# Bold (**...** or __...__). Run BEFORE italic so longer markers go first.
_BOLD_RE = re.compile(r"(\*\*|__)(.+?)\1")
# Setext-style heading underline: a line of all `=` or all `-` (optional
# leading/trailing whitespace). Only treated as a setext underline when the
# previous line is non-blank and non-code-fence — see normalize_markdown.
_SETEXT_UNDERLINE_RE = re.compile(r"^\s{0,3}(=+|-+)\s*$")
# Italic (*...* or _..._). Lookarounds keep us from biting into adjacent words
# or leftover bold markers.
_ITALIC_RE = re.compile(r"(?<![\w*_])([*_])(?!\s)(.+?)(?<!\s)\1(?![\w*_])")
# Inline tag: `#word` not preceded by a word char, `/`, or another `#`.
# Captures the word; the leading `#` is dropped.
_INLINE_TAG_RE = re.compile(r"(?<![\w/#])#([A-Za-z][\w\-/]+)")


def _normalize_line(line: str) -> str:
    """Apply per-line normalization to a single (non-code-fence) line."""
    # Wikilinks first (they use `[[ ]]` which would otherwise interact with
    # inline-link / emphasis matching).
    line = _WIKILINK_RE.sub(lambda m: m.group(2) if m.group(2) else m.group(1), line)
    # Inline links: keep the link text, drop the URL.
    line = _INLINE_LINK_RE.sub(lambda m: m.group(1), line)
    # ATX heading: strip leading `#` markers, keep the title text.
    m = _ATX_HEADING_RE.match(line)
    if m:
        line = m.group(2)
    # Emphasis: bold first (longer markers), then italic.
    line = _BOLD_RE.sub(lambda m: m.group(2), line)
    line = _ITALIC_RE.sub(lambda m: m.group(2), line)
    # Inline #tag → tag.
    line = _INLINE_TAG_RE.sub(lambda m: m.group(1), line)
    return line


def normalize_markdown(text: str, code_fence_line_ranges: list[tuple[int, int]]) -> str:
    """Strip Markdown syntax in place. Code-fence content is left verbatim.

    Setext heading underlines (``=====`` / ``-----``) are replaced with an
    empty line — preserving line count (the orchestrator's heading-position
    relocation depends on input/output line-count parity) while removing the
    underline content from the indexed text.

    ``code_fence_line_ranges`` are inclusive 0-indexed line spans. Any line
    whose index falls inside one of these ranges is emitted unchanged.
    """
    if not text:
        return text

    def in_fence(line_idx: int) -> bool:
        return any(start <= line_idx <= end for start, end in code_fence_line_ranges)

    lines = text.splitlines(keepends=True)

    # Identify setext underline lines (must follow a non-blank, non-fence line
    # and not themselves be in a fence).
    setext_indices: set[int] = set()
    for i in range(1, len(lines)):
        if in_fence(i) or in_fence(i - 1):
            continue
        cur_no_nl = lines[i].rstrip("\r\n")
        prev_no_nl_stripped = lines[i - 1].rstrip("\r\n").strip()
        if not prev_no_nl_stripped:
            continue
        if _SETEXT_UNDERLINE_RE.match(cur_no_nl):
            setext_indices.add(i)

    out: list[str] = []
    for i, raw_line in enumerate(lines):
        if i in setext_indices:
            # Replace with an empty line (preserve line count + line ending).
            if raw_line.endswith("\r\n"):
                out.append("\r\n")
            elif raw_line.endswith("\n"):
                out.append("\n")
            else:
                out.append("")
            continue
        if in_fence(i):
            out.append(raw_line)
            continue
        # Preserve any trailing newline through the transform.
        newline = ""
        body = raw_line
        if body.endswith("\r\n"):
            newline = "\r\n"
            body = body[:-2]
        elif body.endswith("\n"):
            newline = "\n"
            body = body[:-1]
        out.append(_normalize_line(body) + newline)
    return "".join(out)


# Wikilink capture pattern: matches [[Target]], [[Target#Anchor]],
# [[Target|Alias]], and [[Target#Anchor|Alias]].
# Group 1 = target (before # or |), Group 2 = anchor (optional),
# Group 3 = alias (optional).
_WIKILINK_CAPTURE_RE = re.compile(
    r"\[\["
    r"([^\[\]|#]+?)"  # group 1: target
    r"(?:#([^\[\]|]+))?"  # group 2: anchor (optional)
    r"(?:\|([^\[\]]+))?"  # group 3: alias (optional)
    r"\]\]"
)


def parse_wikilinks(text: str) -> list[dict]:
    """Extract all ``[[…]]`` wikilinks from ``text``.

    Returns a list of dicts with these keys (one per match):

    - ``link_text``: the raw inner text exactly as it appeared (preserves
      whitespace, original casing, anchor + alias separators).
    - ``target_title``: the parsed target name (text before ``#`` or ``|``),
      lowercased and stripped, for case-insensitive matching at resolve time.
    - ``anchor``: the ``#Anchor`` portion as it appeared, or ``None``.
    - ``alias``: the ``|Alias`` portion as it appeared, or ``None``.

    Wikilinks with empty or whitespace-only targets are skipped.
    """
    if not text:
        return []

    results: list[dict] = []
    for match in _WIKILINK_CAPTURE_RE.finditer(text):
        target_raw = match.group(1)
        target_title = target_raw.strip().lower()
        if not target_title:
            continue
        # Reconstruct link_text from the raw groups (matches what was between [[ and ]])
        anchor = match.group(2)
        alias = match.group(3)
        link_text = target_raw
        if anchor is not None:
            link_text += "#" + anchor
        if alias is not None:
            link_text += "|" + alias
        results.append(
            {
                "link_text": link_text,
                "target_title": target_title,
                "anchor": anchor,
                "alias": alias,
            }
        )
    return results


@dataclass
class MarkdownExtractResult:
    """Output of :func:`extract_markdown` — what ``run_extract`` consumes.

    - ``title``: the frontmatter ``title`` field, if present and a non-empty
      string. The caller updates ``documents.title`` from this.
    - ``pages``: ``[(page_num, text)]`` in the same shape as ``_extract_txt`` /
      ``_extract_via_tika`` return.
    - ``headings``: a list of dicts ``{"level", "title", "position", "page_num"}``
      in the same shape as the Tika heading flow's output.
    - ``wikilinks``: a list of dicts ``{"link_text", "target_title", "anchor",
      "alias"}`` for each ``[[…]]`` found in the BODY (before normalization).
    """

    title: str | None
    pages: list[tuple[int, str]]
    headings: list[dict]
    wikilinks: list[dict]


def extract_markdown(data: bytes) -> MarkdownExtractResult:
    """Decode and structure-parse Markdown bytes for ingestion.

    Pipeline: decode → frontmatter (with title override) → preamble →
    parse structure on body → normalize body → relocate heading positions →
    compose preamble + body → paginate → map heading positions to pages.
    """
    text = data.decode("utf-8", errors="replace")

    frontmatter, _ = extract_frontmatter(text)
    body = strip_frontmatter(text)
    title = frontmatter.get("title")
    if not isinstance(title, str) or not title.strip():
        title = None

    preamble = flatten_frontmatter(frontmatter)
    raw_headings, fence_ranges = parse_markdown_structure(body)
    wikilinks = parse_wikilinks(body)
    normalized_body = normalize_markdown(body, fence_ranges)

    # Compose the final text the rest of the pipeline will see.
    if preamble:
        full_text = preamble + "\n\n" + normalized_body
        body_offset_in_full = len(preamble) + 2
    else:
        full_text = normalized_body
        body_offset_in_full = 0

    # Relocate each heading by line index. ``normalize_markdown`` preserves
    # line count (one input line → one output line), so the heading on line N
    # of the body is on line N of the normalized body. This avoids latching
    # onto a prose occurrence of the title that precedes the heading.
    body_line_starts = _line_start_offsets(body)
    norm_line_starts = _line_start_offsets(normalized_body)
    headings: list[dict] = []
    for h in raw_headings:
        # parse_markdown_structure returns positions that are always at a
        # line start. Find which line.
        raw_pos = h["position"]
        try:
            line_idx = body_line_starts.index(raw_pos)
        except ValueError:
            continue  # position did not align with a line start (shouldn't happen)
        if line_idx >= len(norm_line_starts):
            continue
        pos_in_normalized = norm_line_starts[line_idx]
        headings.append(
            {
                "level": h["level"],
                "title": h["title"],
                "position": pos_in_normalized + body_offset_in_full,
                "page_num": None,  # filled in below
            }
        )

    settings = get_settings()
    pages = paginate_text(full_text, settings.synthetic_page_chars)

    # Map each heading's position to a page number.
    cum_offsets: list[tuple[int, int, int]] = []  # (start, end, page_num)
    offset = 0
    for page_num, page_text in pages:
        end = offset + len(page_text)
        cum_offsets.append((offset, end, page_num))
        offset = end
    for h in headings:
        for _start, end, pnum in cum_offsets:
            if h["position"] < end:
                h["page_num"] = pnum
                break

    return MarkdownExtractResult(title=title, pages=pages, headings=headings, wikilinks=wikilinks)

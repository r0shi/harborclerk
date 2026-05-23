"""Tests for the Markdown extraction helpers in worker/markdown_extract.py."""

from harbor_clerk.worker.markdown_extract import (
    MarkdownExtractResult,
    extract_frontmatter,
    extract_markdown,
    flatten_frontmatter,
    normalize_markdown,
    parse_markdown_structure,
)

# --- extract_frontmatter ---


def test_extract_frontmatter_none():
    text = "# Heading\n\nBody text.\n"
    fm, body = extract_frontmatter(text)
    assert fm == {}
    assert body == text


def test_extract_frontmatter_basic():
    text = "---\ntitle: Q3 Report\ntags: [finance, draft]\n---\nBody text.\n"
    fm, body = extract_frontmatter(text)
    assert fm == {"title": "Q3 Report", "tags": ["finance", "draft"]}
    assert body == "Body text.\n"


def test_extract_frontmatter_empty_block():
    text = "---\n---\nBody.\n"
    fm, body = extract_frontmatter(text)
    assert fm == {}
    assert body == "Body.\n"


def test_extract_frontmatter_malformed_yaml():
    """Malformed YAML inside --- delimiters → treat as no frontmatter."""
    text = "---\ntitle: : : invalid\n---\nBody.\n"
    fm, body = extract_frontmatter(text)
    assert fm == {}
    assert body == text


def test_extract_frontmatter_only_no_body():
    text = "---\ntitle: X\n---\n"
    fm, body = extract_frontmatter(text)
    assert fm == {"title": "X"}
    assert body == ""


def test_extract_frontmatter_does_not_match_mid_document():
    """A --- in the middle of the document is not frontmatter."""
    text = "Intro paragraph.\n\n---\ntitle: not frontmatter\n---\nMore body.\n"
    fm, body = extract_frontmatter(text)
    assert fm == {}
    assert body == text


# --- flatten_frontmatter ---


def test_flatten_empty():
    assert flatten_frontmatter({}) == ""


def test_flatten_skips_title():
    """title is handled by the orchestrator (updates doc.title); not in the preamble."""
    assert flatten_frontmatter({"title": "X"}) == ""


def test_flatten_scalar_values():
    out = flatten_frontmatter({"author": "Alex", "status": "draft"})
    assert "Author: Alex." in out
    assert "Status: draft." in out


def test_flatten_list_values():
    out = flatten_frontmatter({"tags": ["finance", "draft"]})
    assert "Tags: finance, draft." in out


def test_flatten_skips_none_and_empty_list():
    out = flatten_frontmatter({"deleted_at": None, "reviewers": []})
    assert out == ""


def test_flatten_capitalises_key_first_letter():
    out = flatten_frontmatter({"aliases": ["Q3"]})
    assert out.startswith("Aliases: ")


def test_flatten_ignores_nested_structures():
    """Nested dicts and lists-of-non-scalars are skipped (not useful as flat text)."""
    out = flatten_frontmatter({"nested": {"k": "v"}, "list_of_lists": [[1, 2]]})
    assert out == ""


def test_flatten_combines_multiple_fields():
    out = flatten_frontmatter({"tags": ["a", "b"], "author": "X"})
    assert "Tags: a, b." in out
    assert "Author: X." in out


# --- parse_markdown_structure ---


def test_parse_structure_empty():
    headings, fences = parse_markdown_structure("")
    assert headings == []
    assert fences == []


def test_parse_structure_atx_headings():
    text = "# Heading One\n\nSome body.\n\n## Heading Two\n"
    headings, fences = parse_markdown_structure(text)
    assert fences == []
    assert len(headings) == 2
    assert headings[0]["level"] == 1
    assert headings[0]["title"] == "Heading One"
    assert headings[1]["level"] == 2
    assert headings[1]["title"] == "Heading Two"
    assert headings[0]["position"] < headings[1]["position"]
    assert headings[0]["position"] == 0


def test_parse_structure_setext_headings():
    """Setext (underline-style) headings are parsed."""
    text = "Heading One\n===========\n\nBody.\n\nHeading Two\n-----------\n"
    headings, fences = parse_markdown_structure(text)
    assert [h["level"] for h in headings] == [1, 2]
    assert [h["title"] for h in headings] == ["Heading One", "Heading Two"]


def test_parse_structure_code_fence_excluded_from_headings():
    """A `#`-prefixed line inside a fenced code block must NOT be parsed as a heading."""
    text = "Intro.\n\n```python\n# This is a Python comment, not a heading\n```\n\n# Real heading after the fence\n"
    headings, fences = parse_markdown_structure(text)
    titles = [h["title"] for h in headings]
    assert "Real heading after the fence" in titles
    assert "This is a Python comment, not a heading" not in titles
    assert len(fences) == 1


def test_parse_structure_fence_line_ranges():
    """Code-fence spans are inclusive line ranges (0-indexed)."""
    text = "Intro.\n\n```\nline1\nline2\n```\n\nOutro.\n"
    _, fences = parse_markdown_structure(text)
    assert fences == [(2, 5)]


def test_parse_structure_heading_position_in_chars():
    """Position is a character offset in the original text, not a line number."""
    text = "Line one.\nLine two.\n# Heading on line 3\n"
    headings, _ = parse_markdown_structure(text)
    assert len(headings) == 1
    assert headings[0]["position"] == 20


# --- normalize_markdown ---


def test_normalize_strips_atx_heading_marker():
    assert normalize_markdown("# Heading One\n", []) == "Heading One\n"


def test_normalize_strips_multi_level_atx():
    assert normalize_markdown("### Sub-section\n", []) == "Sub-section\n"


def test_normalize_strips_bold_emphasis():
    out = normalize_markdown("This is **bold** text.\n", [])
    assert out == "This is bold text.\n"


def test_normalize_strips_italic_emphasis():
    out = normalize_markdown("This is *italic* text.\n", [])
    assert out == "This is italic text.\n"


def test_normalize_strips_underscore_emphasis():
    out = normalize_markdown("__bold__ and _italic_ work too.\n", [])
    assert out == "bold and italic work too.\n"


def test_normalize_unwraps_inline_link():
    out = normalize_markdown("See [the report](https://example.com/r.pdf) here.\n", [])
    assert out == "See the report here.\n"


def test_normalize_unwraps_wikilink_plain():
    out = normalize_markdown("Refers to [[Project Plan]] for details.\n", [])
    assert out == "Refers to Project Plan for details.\n"


def test_normalize_unwraps_wikilink_with_alias():
    out = normalize_markdown("See [[ProjectPlan|the plan]] for details.\n", [])
    assert out == "See the plan for details.\n"


def test_normalize_unwraps_wikilink_with_heading_anchor():
    out = normalize_markdown("See [[ProjectPlan#Budget]].\n", [])
    assert out == "See ProjectPlan.\n"


def test_normalize_inline_tag_loses_hash():
    out = normalize_markdown("Logged with #project-q3 today.\n", [])
    assert out == "Logged with project-q3 today.\n"


def test_normalize_preserves_code_fence_content():
    """Text inside a fenced code block is left verbatim, including # and ** syntax."""
    text = "Intro.\n\n```python\n# This **stays** bold-looking\nprint('[x](y)')\n```\n\nOutro **bold**.\n"
    out = normalize_markdown(text, [(2, 5)])
    assert "# This **stays** bold-looking" in out
    assert "print('[x](y)')" in out
    assert "Outro bold." in out


def test_normalize_idempotent_on_clean_prose():
    text = "Just plain prose with no markup.\n"
    assert normalize_markdown(text, []) == text


# --- extract_markdown orchestrator ---


def test_extract_markdown_minimal():
    """A plain Markdown doc with one heading."""
    data = b"# Hello\n\nWorld.\n"
    result = extract_markdown(data)
    assert isinstance(result, MarkdownExtractResult)
    assert result.title is None
    assert len(result.pages) == 1
    page_num, page_text = result.pages[0]
    assert page_num == 1
    assert "Hello" in page_text
    assert "World." in page_text
    assert len(result.headings) == 1
    assert result.headings[0]["level"] == 1
    assert result.headings[0]["title"] == "Hello"


def test_extract_markdown_frontmatter_title_returned():
    """Frontmatter `title` is returned so the caller can apply it to doc.title."""
    data = b"---\ntitle: Project Plan\n---\n\nBody.\n"
    result = extract_markdown(data)
    assert result.title == "Project Plan"


def test_extract_markdown_frontmatter_preamble_prepended():
    """Non-title frontmatter fields become a searchable preamble in the page text."""
    data = b"---\ntitle: X\ntags: [finance, draft]\n---\n# Body Heading\n\nBody text.\n"
    result = extract_markdown(data)
    page_text = result.pages[0][1]
    assert "Tags: finance, draft." in page_text
    assert "---" not in page_text


def test_extract_markdown_normalises_body():
    """Markdown syntax is stripped from the body before pagination."""
    data = b"# Heading\n\nThis is **bold** with [a link](https://example.com).\n"
    result = extract_markdown(data)
    page_text = result.pages[0][1]
    assert "**bold**" not in page_text
    assert "bold" in page_text
    assert "https://example.com" not in page_text
    assert "a link" in page_text


def test_extract_markdown_heading_positions_match_normalised_text():
    """Heading positions are character offsets into the page text."""
    data = b"# Section A\n\nProse.\n\n## Section B\n\nMore prose.\n"
    result = extract_markdown(data)
    page_text = result.pages[0][1]
    for h in result.headings:
        assert page_text[h["position"] :].startswith(h["title"])


def test_extract_markdown_invalid_utf8_replaced():
    """Invalid UTF-8 bytes are replaced rather than raising."""
    data = b"# Heading\n\nBad byte: \xff bytes.\n"
    result = extract_markdown(data)
    assert "Heading" in result.pages[0][1]


def test_extract_markdown_code_fence_survives():
    data = b"# Heading\n\n```python\nx = '**not bold**'\n```\n\nAfter.\n"
    result = extract_markdown(data)
    page_text = result.pages[0][1]
    assert "x = '**not bold**'" in page_text


def test_extract_markdown_no_frontmatter_no_title():
    data = b"# Body Heading\n\nProse.\n"
    result = extract_markdown(data)
    assert result.title is None


def test_extract_markdown_frontmatter_only_title_no_preamble():
    data = b"---\ntitle: Just A Title\n---\n# Body.\n\nText.\n"
    result = extract_markdown(data)
    assert result.title == "Just A Title"
    page_text = result.pages[0][1]
    assert page_text.startswith("Body.") or page_text.startswith("Body")


def test_extract_markdown_heading_title_appearing_earlier_in_prose():
    """Regression: a heading whose title text appears verbatim earlier in
    prose must NOT have its position bind to the prose occurrence — the
    position must point at the actual heading line."""
    data = b"See the Budget section for details.\n\n## Budget\n\nActual budget info.\n"
    result = extract_markdown(data)
    assert len(result.headings) == 1
    h = result.headings[0]
    assert h["title"] == "Budget"
    page_text = result.pages[0][1]
    # The character before the heading position must be a newline (or it must
    # be the very start of the page text). Otherwise the position is pointing
    # inside a prose line, not at a heading line.
    if h["position"] > 0:
        assert page_text[h["position"] - 1] == "\n", (
            f"Heading position {h['position']} points inside a line; "
            f"char before is {page_text[h['position'] - 1]!r}, expected '\\n'. "
            f"Page text: {page_text!r}"
        )


# --- setext underline stripping ---


def test_normalize_strips_setext_h1_underline():
    """Setext h1: title line preserved; the `=====` underline becomes an empty line."""
    out = normalize_markdown("Heading One\n===========\nBody.\n", [])
    assert out == "Heading One\n\nBody.\n"


def test_normalize_strips_setext_h2_underline():
    out = normalize_markdown("Heading Two\n-----------\nBody.\n", [])
    assert out == "Heading Two\n\nBody.\n"


def test_normalize_thematic_break_after_blank_is_preserved():
    """An `---` line preceded by a blank line is a thematic break, not a setext underline.
    Leave it alone (setext requires a non-blank line above)."""
    text = "Some prose.\n\n---\nMore prose.\n"
    out = normalize_markdown(text, [])
    # The `---` should still be in the output (it's a thematic break, not setext).
    assert "---\n" in out


def test_normalize_setext_inside_code_fence_is_preserved():
    """A `=====` line inside a fenced code block must NOT be treated as setext."""
    text = "Intro.\n\n```\nHeading\n=======\n```\n\nAfter.\n"
    # Fence on lines 2..5 inclusive (0-indexed):
    #   0 Intro.
    #   1 (blank)
    #   2 ```
    #   3 Heading
    #   4 =======
    #   5 ```
    out = normalize_markdown(text, [(2, 5)])
    assert "Heading\n=======\n" in out


def test_normalize_setext_preserves_line_count():
    """Replacing the underline (not removing it) keeps the line count constant,
    which the orchestrator's heading-position relocation depends on."""
    text = "Heading One\n===========\nBody.\nMore body.\n"
    out = normalize_markdown(text, [])
    assert out.count("\n") == text.count("\n")


def test_extract_markdown_setext_heading_clean_output():
    """End-to-end: a setext heading's title is in document_headings; the `===`
    underline does not appear in the page text."""
    data = b"Heading One\n===========\n\nBody text.\n"
    result = extract_markdown(data)
    assert len(result.headings) == 1
    assert result.headings[0]["level"] == 1
    assert result.headings[0]["title"] == "Heading One"
    page_text = result.pages[0][1]
    assert "===" not in page_text
    assert "Heading One" in page_text
    assert "Body text." in page_text

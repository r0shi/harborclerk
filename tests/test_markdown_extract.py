"""Tests for the Markdown extraction helpers in worker/markdown_extract.py."""

from harbor_clerk.worker.markdown_extract import (
    extract_frontmatter,
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

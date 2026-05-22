"""Tests for the Markdown extraction helpers in worker/markdown_extract.py."""

from harbor_clerk.worker.markdown_extract import extract_frontmatter, flatten_frontmatter

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

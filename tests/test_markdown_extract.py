"""Tests for the Markdown extraction helpers in worker/markdown_extract.py."""

from harbor_clerk.worker.markdown_extract import extract_frontmatter

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

"""Markdown extract path strips YAML frontmatter from body text before
chunking, so the chunks/embedding/FTS don't include raw YAML noise."""

from harbor_clerk.worker.markdown_extract import strip_frontmatter


def test_strip_frontmatter_removes_yaml_block():
    body = """---
title: Notes
tags: [a, b]
---

# Heading

Body content.
"""
    assert strip_frontmatter(body) == "# Heading\n\nBody content.\n"


def test_strip_frontmatter_passes_through_unchanged_without_frontmatter():
    body = "# Just a heading\n\nNo frontmatter.\n"
    assert strip_frontmatter(body) == body


def test_strip_frontmatter_passes_through_unchanged_when_yaml_is_malformed():
    """Malformed YAML → don't risk losing body content; pass through unchanged.
    Metadata extraction logs the parse failure separately."""
    body = """---
title: Meeting
unclosed_list: [a, b
---

content
"""
    assert strip_frontmatter(body) == body

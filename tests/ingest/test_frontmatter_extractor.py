"""FrontmatterExtractor — parses YAML frontmatter from markdown."""

import uuid
from dataclasses import dataclass

from harbor_clerk.ingest.metadata_extractors.frontmatter import FrontmatterExtractor, _jsonify


@dataclass
class _FakeDoc:
    doc_id: uuid.UUID
    title: str
    canonical_filename: str | None


def test_frontmatter_extractor_parses_yaml_block():
    body = b"""---
title: Meeting Notes
date: 2024-09-15
tags: [team, planning]
status: draft
---

# Meeting Notes

We discussed roadmap items.
"""
    extractor = FrontmatterExtractor()
    out = extractor.extract(
        doc=_FakeDoc(doc_id=uuid.uuid4(), title="meeting", canonical_filename="notes.md"),
        raw_bytes=body,
        source_path=None,
    )
    assert out == {
        "title": "Meeting Notes",
        "date": "2024-09-15",  # YAML date → ISO string at the boundary
        "tags": ["team", "planning"],
        "status": "draft",
    }


def test_frontmatter_extractor_returns_none_for_non_markdown():
    """A .pdf or .docx file is never parsed for frontmatter."""
    extractor = FrontmatterExtractor()
    out = extractor.extract(
        doc=_FakeDoc(doc_id=uuid.uuid4(), title="r", canonical_filename="report.pdf"),
        raw_bytes=b"%PDF-1.7\n---\ntitle: nope\n---\n...",
        source_path=None,
    )
    assert out is None


def test_frontmatter_extractor_returns_none_for_markdown_without_frontmatter():
    extractor = FrontmatterExtractor()
    out = extractor.extract(
        doc=_FakeDoc(doc_id=uuid.uuid4(), title="r", canonical_filename="note.md"),
        raw_bytes=b"# Just a heading\n\nNo frontmatter here.\n",
        source_path=None,
    )
    assert out is None


def test_frontmatter_extractor_handles_malformed_yaml_without_raising():
    """A markdown file with broken YAML in the frontmatter block → returns
    None + logs a warning. Does NOT propagate the parser exception (the
    framework would catch it anyway but the extractor itself is defensive)."""
    body = b"""---
title: Meeting
date: 2024-09-15
unclosed_list: [a, b
---

content
"""
    extractor = FrontmatterExtractor()
    out = extractor.extract(
        doc=_FakeDoc(doc_id=uuid.uuid4(), title="m", canonical_filename="m.md"),
        raw_bytes=body,
        source_path=None,
    )
    assert out is None


def test_frontmatter_extractor_recognises_markdown_extension_variants():
    """Both .md and .markdown trigger parsing."""
    body = b"---\nfoo: bar\n---\n\ncontent\n"
    extractor = FrontmatterExtractor()
    for ext in (".md", ".markdown"):
        out = extractor.extract(
            doc=_FakeDoc(doc_id=uuid.uuid4(), title="t", canonical_filename=f"file{ext}"),
            raw_bytes=body,
            source_path=None,
        )
        assert out == {"foo": "bar"}, f"failed for extension {ext}"


def test_frontmatter_extractor_falls_back_to_source_path_when_no_filename():
    """If canonical_filename is None, use source_path to detect the extension."""
    body = b"---\nfoo: bar\n---\n\ncontent\n"
    extractor = FrontmatterExtractor()
    out = extractor.extract(
        doc=_FakeDoc(doc_id=uuid.uuid4(), title="t", canonical_filename=None),
        raw_bytes=body,
        source_path="/tmp/note.md",
    )
    assert out == {"foo": "bar"}


def test_frontmatter_extractor_strips_utf8_bom():
    """UTF-8 BOM (U+FEFF) before --- delimiter shouldn't hide the frontmatter."""
    body = b"\xef\xbb\xbf---\nfoo: bar\n---\n\ncontent\n"
    extractor = FrontmatterExtractor()
    out = extractor.extract(
        doc=_FakeDoc(doc_id=uuid.uuid4(), title="t", canonical_filename="note.md"),
        raw_bytes=body,
        source_path=None,
    )
    assert out == {"foo": "bar"}


def test_jsonify_converts_yaml_binary_to_string():
    """!!binary YAML tag produces bytes; _jsonify decodes to a JSON-serializable string."""
    assert _jsonify({"data": b"hello"}) == {"data": "hello"}
    assert _jsonify(b"raw") == "raw"

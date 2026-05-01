"""Tests for watched folder pipeline integration."""

from unittest.mock import MagicMock


def test_doc_filename_with_source_path():
    from harbor_clerk.worker.pipeline import _doc_filename

    doc = MagicMock()
    doc.original_object_key = None
    doc.source_path = "/Users/test/Documents/report.pdf"
    assert _doc_filename(doc) == "report.pdf"


def test_doc_filename_with_object_key():
    from harbor_clerk.worker.pipeline import _doc_filename

    doc = MagicMock()
    doc.original_object_key = "versions/abc/report.pdf"
    doc.source_path = "/Users/test/Documents/report.pdf"
    assert _doc_filename(doc) == "report.pdf"


def test_doc_filename_neither():
    from harbor_clerk.worker.pipeline import _doc_filename

    doc = MagicMock()
    doc.original_object_key = None
    doc.source_path = None
    assert _doc_filename(doc) == ""

"""Tests for watched folder pipeline integration."""

from unittest.mock import MagicMock


def test_version_filename_with_source_path():
    from harbor_clerk.worker.pipeline import _version_filename

    version = MagicMock()
    version.original_object_key = None
    version.source_path = "/Users/test/Documents/report.pdf"
    assert _version_filename(version) == "report.pdf"


def test_version_filename_with_object_key():
    from harbor_clerk.worker.pipeline import _version_filename

    version = MagicMock()
    version.original_object_key = "versions/abc/report.pdf"
    version.source_path = "/Users/test/Documents/report.pdf"
    assert _version_filename(version) == "report.pdf"


def test_version_filename_neither():
    from harbor_clerk.worker.pipeline import _version_filename

    version = MagicMock()
    version.original_object_key = None
    version.source_path = None
    assert _version_filename(version) == "unknown"

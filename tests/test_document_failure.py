"""explain_failure: stored pipeline errors sorted into plain-language families (#555).

The strings are the shapes the pipeline actually stores: ``describe_error(exc)``
("TypeName: message") from worker/entry.py, the embed give-up from
``_record_embed_unavailable_job``, the deliberate-stop texts, and the rows in
the #555 data comment from the dogfood corpus.
"""

from __future__ import annotations

import ast
import types
from pathlib import Path

import pytest

from harbor_clerk.document_failure import DELIBERATE_STOP_ERRORS, FailureFamily, explain_failure
from harbor_clerk.error_text import describe_error
from harbor_clerk.models.enums import PipelineStatus
from harbor_clerk.watched_source import NoReadableSource, WatchedFileMissing, failed_for_a_missing_source

_SRC = Path(__file__).resolve().parent.parent / "src" / "harbor_clerk"

TEMP, UNREAD, OTHER = FailureFamily.temporary, FailureFamily.unreadable, FailureFamily.other

CASES: list[tuple[str, FailureFamily]] = [
    # --- the #555 data comment (2026-09 dogfood corpus) ---
    ("ConnectError: [Errno 61] Connection refused", TEMP),
    (
        "RuntimeError: Tika rejected file (422): org.apache.poi.ooxml.POIXMLException: "
        "java.lang.IndexOutOfBoundsException",
        UNREAD,
    ),
    ("RuntimeError: Tika rejected file (422): java.lang.IndexOutOfBoundsException: Index 3 out of bounds", UNREAD),
    ("ReadError: [Errno 54] Connection reset by peer", TEMP),
    ("OSError: broken data stream when reading image file", UNREAD),
    ("TimeoutError: Stage entities timed out after 1800s", TEMP),
    (
        "HTTPStatusError: Server error '500 Internal Server Error' for url 'http://tika:9998/tika'\n"
        "For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/500",
        TEMP,
    ),
    # --- temporary: transport, timeouts, 5xx, retry-later ---
    ("ConnectError: [Errno 111] Connection refused", TEMP),
    ("ConnectTimeout", TEMP),
    ("ReadTimeout", TEMP),
    ("TimeoutError", TEMP),
    ("RemoteProtocolError: Server disconnected without sending a response.", TEMP),
    ("HTTPStatusError: Server error '503 Service Unavailable' for url 'http://tika:9998/tika'", TEMP),
    ("HTTPStatusError: Server error '502 Bad Gateway' for url 'http://llm:8102/v1'", TEMP),
    ("HTTPStatusError: Client error '429 Too Many Requests' for url 'http://llm/v1'", TEMP),
    ("RuntimeError: LLM server unavailable, try again later", TEMP),
    # The terminal embed failure since #862: retry-later attempts exhausted.
    (
        "Embedder unavailable: gave up after 6 attempts over about 62 minutes with no progress. Last error: "
        "EmbedderUnavailableError: embedder call failed after 3/7 attempts: ConnectError: [Errno 61] "
        "Connection refused",
        TEMP,
    ),
    ("EmbedderUnavailableError: embedder call failed after 7/7 attempts: HTTP 503", TEMP),
    ("AppleIntelligenceUnavailableError: Apple Intelligence summaries are enabled but unavailable: busy", TEMP),
    # --- unreadable: this file ---
    (
        "RuntimeError: Tika rejected file (422): org.apache.poi.EncryptedDocumentException: "
        "Export Restrictions in place - please install JCE Unlimited Strength Jurisdiction Policy files",
        UNREAD,
    ),
    ("PdfiumError: Failed to load document (PDFium: Incorrect password error).", UNREAD),
    ("PdfiumError: Failed to load document (PDFium: Data format error).", UNREAD),
    ("UnidentifiedImageError: cannot identify image file <_io.BytesIO object at 0x1047>", UNREAD),
    ("OSError: image file is truncated (12 bytes not processed)", UNREAD),
    ("DecompressionBombError: Image size (300000000 pixels) exceeds limit", UNREAD),
    ("UnicodeDecodeError: 'utf-8' codec can't decode byte 0xff in position 0: invalid start byte", UNREAD),
    ("HTTPStatusError: Client error '415 Unsupported Media Type' for url 'http://tika:9998/tika'", UNREAD),
    (describe_error(WatchedFileMissing("the watched file of doc X cannot be read in place ([Errno 2])")), UNREAD),
    (describe_error(NoReadableSource("doc X is not a watched document and has no stored original")), UNREAD),
    ("RuntimeError: No source for doc 1234: missing", UNREAD),
    ("FileNotFoundError: [Errno 2] No such file or directory: '/data/originals/docs/x/a.pdf'", UNREAD),
    ("PermissionError: [Errno 13] Permission denied: '/Users/a/b.pdf'", UNREAD),
    # A 422 whose diagnostic refetch then timed out is still the file's fault.
    ("RuntimeError: Tika rejected file (422): rmeta refetch failed: ReadTimeout", UNREAD),
    # --- other ---
    ("Cancelled by user", OTHER),
    ("Queue cleared before pipeline completed", OTHER),
    ("Cancelled by admin (queue clear)", OTHER),
    # A bare status-like number is not an HTTP status (review of #920).
    ("IndexError: index 429 is out of bounds for axis 0 with size 12", OTHER),
    ("KeyError: 429", OTHER),
    ("IndexError: list index 503 out of range", OTHER),
    ("ValueError: expected 500 rows, got 12", OTHER),
    ("EmbedderUnavailableError: embedder call failed after 7/7 attempts: HTTP 429", TEMP),
    # A path or file name never decides the family (review of #920).
    ("KeyError: '/Users/a/unavailable docs/x.pdf'", OTHER),
    ("KeyError: '/Users/a/timed out/Connection refused.pdf'", OTHER),
    ('ValueError: bad page in "/Volumes/Archive/Service Unavailable/a.pdf"', OTHER),
    # "unavailable" alone is not an outage.
    ("RuntimeError: OCR language pack fra unavailable", OTHER),
    ("RuntimeError: page image unavailable for page 3", OTHER),
    (
        "DocumentTooLargeError: Document too large to index: it splits into 57,180 chunks, over the "
        "per-document limit of 20,000",
        OTHER,
    ),
    (
        "RuntimeError: Tika is required for extraction (TIKA_URL not set). Only plain text and images work "
        "without Tika.",
        OTHER,
    ),
    ("EmbedderError: embedder rejected request: HTTP 422: body.texts: field required", OTHER),
    ("AppleIntelligenceContentRejectedError: the model declined the content", OTHER),
    ("KeyError: 'pages'", OTHER),
    ("RuntimeError", OTHER),
]


@pytest.mark.parametrize(("error", "family"), CASES, ids=[c[0][:60] for c in CASES])
def test_family(error: str, family: FailureFamily) -> None:
    explanation = explain_failure(error)
    assert explanation is not None
    assert explanation.family is family


@pytest.mark.parametrize("error", [None, "", "   \n"])
def test_no_error_text_has_no_explanation(error: str | None) -> None:
    assert explain_failure(error) is None


@pytest.mark.parametrize(("error", "_family"), CASES, ids=[c[0][:60] for c in CASES])
def test_message_and_action_never_quote_the_raw_error(error: str, _family: FailureFamily) -> None:
    """The plain-language text is fixed per rule: no path or raw detail leaks
    through it, so it needs no redaction for an API-key caller."""
    explanation = explain_failure(error)
    assert explanation is not None
    for text in (explanation.message, explanation.action):
        assert text.strip()
        assert "/" not in text
        assert "Errno" not in text and "Error:" not in text


# Modules that write a pipeline error into ingestion_jobs.error / documents.error.
_ERROR_WRITERS = [
    *sorted((_SRC / "worker").rglob("*.py")),
    *sorted((_SRC / "watcher").rglob("*.py")),
    _SRC / "api" / "routes" / "system.py",
    _SRC / "api" / "routes" / "documents.py",
]


def _literal_error_writes() -> set[str]:
    """Every string literal assigned to an ``.error`` attribute or passed as
    ``error=`` in the modules above (AST, so comments and docstrings never match)."""
    found: set[str] = set()
    for path in _ERROR_WRITERS:
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            value = getattr(node, "value", None)
            if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
                continue
            assigns_error = isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Attribute) and t.attr == "error" for t in node.targets
            )
            passes_error = isinstance(node, ast.keyword) and node.arg == "error"
            if assigns_error or passes_error:
                found.add(value.value)
    return found


def test_every_literal_stop_text_written_by_the_pipeline_is_a_known_deliberate_stop() -> None:
    """Job- and document-level: a new cancel or stop string written anywhere in the
    pipeline must be added to DELIBERATE_STOP_ERRORS, or it reads as "unexpected"."""
    written = _literal_error_writes()
    assert written == set(DELIBERATE_STOP_ERRORS)


def test_deliberate_stops_read_as_a_stop() -> None:
    """Every stop text, including reprocess-failed's document-level list, reads as a
    stop, not as a failure family."""
    from harbor_clerk.api.routes.documents import DELIBERATELY_STOPPED_ERRORS

    assert set(DELIBERATELY_STOPPED_ERRORS) <= set(DELIBERATE_STOP_ERRORS)
    for text in DELIBERATE_STOP_ERRORS:
        explanation = explain_failure(text)
        assert explanation is not None
        assert explanation.family is FailureFamily.other
        assert "stopped" in explanation.message


@pytest.mark.parametrize(("error", "_family"), CASES, ids=[c[0][:60] for c in CASES])
def test_missing_source_agrees_with_the_watcher_requeue_check(error: str, _family: FailureFamily) -> None:
    """watched_source decides which errors the watcher requeues as "source
    missing"; every one of those must read as unreadable here, never temporary."""
    doc = types.SimpleNamespace(pipeline_status=PipelineStatus.error, error=error)
    if failed_for_a_missing_source(doc):
        explanation = explain_failure(error)
        assert explanation is not None
        assert explanation.family is FailureFamily.unreadable


def test_a_stage_alarm_timeout_is_temporary_but_honest_about_repeats() -> None:
    stage = explain_failure("TimeoutError: Stage entities timed out after 1800s")
    assert stage is not None
    assert stage.family is FailureFamily.temporary
    assert "If it times out again" in stage.action
    assert "too large" in stage.action
    # An HTTP timeout keeps the plain retry advice.
    http = explain_failure("ReadTimeout")
    assert http is not None
    assert http.family is FailureFamily.temporary
    assert "times out again" not in http.action

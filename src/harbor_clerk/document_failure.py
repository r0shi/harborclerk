"""Plain-language explanations of a document's stored pipeline error (#555).

A failed stage records ``describe_error(exc)`` ("TypeName: message") in
``ingestion_jobs.error`` and ``documents.error``. That string is the right
diagnostic and the wrong thing to lead with: on the dogfood corpus 92% of
failed documents were transient infrastructure blips (a refused connection to
the embedder, a reset, a timeout, a 5xx) that rendered identically to the few
genuinely unreadable files.

``explain_failure`` sorts a stored error into one of three families:

- ``temporary``: the service, not the file, was at fault; reprocessing later
  should succeed.
- ``unreadable``: this file cannot be read (Tika 422, decode errors, broken
  image or PDF data, an unsupported type, encryption, a watched file that is
  gone or unreadable).
- ``other``: everything else, including a deliberate stop.

It is a pure function over the stored text so that rows written before this
module existed are explained too, and so that one table decides every surface.
The returned message and action are fixed strings from the table below: they
never quote the raw error, so they carry no path and need no redaction. The
raw error stays available beside them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from harbor_clerk.api.scope import redact_path_tokens


class FailureFamily(StrEnum):
    temporary = "temporary"
    unreadable = "unreadable"
    other = "other"


@dataclass(frozen=True)
class FailureExplanation:
    family: FailureFamily
    message: str
    action: str


_REPROCESS = "Reprocess the document to try again."

# Every text the pipeline writes into ``ingestion_jobs.error`` or
# ``documents.error`` when work is stopped on purpose rather than failing:
# a user cancel (worker/pipeline.py, job and document) and the admin queue clear
# (api/routes/system.py, job and document). tests/test_document_failure.py
# collects every literal error written in those modules and fails on one
# missing here, so a new cancel string cannot fall through to "unexpected".
DELIBERATE_STOP_ERRORS: tuple[str, ...] = (
    "Cancelled by user",
    "Cancelled by admin (queue clear)",
    "Queue cleared before pipeline completed",
)

# Ordered: the first matching rule wins. Specific causes come before the
# generic connection/timeout patterns because their messages often quote one
# (a Tika 422 whose rmeta refetch then timed out, a watched file read that
# failed with an errno, the embed give-up quoting its last ConnectError).
_RULES: tuple[tuple[re.Pattern[str], FailureExplanation], ...] = tuple(
    (re.compile(pattern, flags), explanation)
    for pattern, flags, explanation in (
        # Deliberate stops, job- and document-level.
        (
            r"^\s*(" + "|".join(re.escape(text) for text in DELIBERATE_STOP_ERRORS) + r")\s*$",
            0,
            FailureExplanation(
                FailureFamily.other,
                "Processing was stopped before it finished.",
                "Reprocess the document when you want it indexed.",
            ),
        ),
        # watched_source.read_document_bytes, plus the pre-upgrade wording.
        (
            r"\bWatchedFileMissing\b|^RuntimeError: No source for doc ",
            0,
            FailureExplanation(
                FailureFamily.unreadable,
                "The source file could not be read. It may have been moved, renamed or deleted, "
                "or it is no longer accessible.",
                "Check that the file is still in the watched folder and can be opened. "
                "It is picked up again automatically when it changes.",
            ),
        ),
        (
            r"\bNoReadableSource\b",
            0,
            FailureExplanation(
                FailureFamily.unreadable,
                "The original file for this document is no longer available.",
                "Add the file again.",
            ),
        ),
        (
            r"\b(FileNotFoundError|PermissionError|IsADirectoryError|NotADirectoryError)\b",
            0,
            FailureExplanation(
                FailureFamily.unreadable,
                "The file could not be opened. It may have been moved or deleted, or its permissions "
                "do not allow reading it.",
                "Check the file, then reprocess the document.",
            ),
        ),
        # worker/stages/chunk.py
        (
            r"\bDocumentTooLargeError\b",
            0,
            FailureExplanation(
                FailureFamily.other,
                "This document is too large to index under the current per-document limit.",
                "Raise the max_chunks_per_document setting and reprocess, or leave files like this "
                "(dataset or database dumps) out of the library.",
            ),
        ),
        # worker/stages/extract.py: Tika is not configured.
        (
            r"TIKA_URL not set",
            0,
            FailureExplanation(
                FailureFamily.other,
                "Text extraction for this file type is not set up (Tika is not configured).",
                "Configure Tika, then reprocess the document.",
            ),
        ),
        # Encrypted or password-protected: a Tika 422 (POI/PDFBox
        # EncryptedDocumentException) or pypdfium2 refusing a password.
        (
            r"EncryptedDocumentException|Incorrect password|password[- ]protected|\bis encrypted\b"
            r"|\bencrypted (document|file|PDF)\b",
            re.IGNORECASE,
            FailureExplanation(
                FailureFamily.unreadable,
                "This file is encrypted or password-protected, so its text cannot be read.",
                "Remove the password, then add the file again.",
            ),
        ),
        # worker/stages/extract.py: Tika refused to parse the file.
        (
            r"Tika rejected file \(422\)",
            0,
            FailureExplanation(
                FailureFamily.unreadable,
                "This file could not be read. It may be damaged, or saved in a format version the "
                "text extractor does not understand.",
                "Check that the file opens on your computer. Re-saving or exporting it in a standard "
                "format usually fixes this.",
            ),
        ),
        (
            r"Unsupported Media Type|\bunsupported (file|mime|media) type\b",
            re.IGNORECASE,
            FailureExplanation(
                FailureFamily.unreadable,
                "This file type is not supported.",
                "Convert the file to a supported format, such as PDF, Word or plain text, and add it again.",
            ),
        ),
        # Broken image or PDF data (Pillow, pypdfium2) during OCR.
        (
            r"\bUnidentifiedImageError\b|cannot identify image file|broken data stream|image file is truncated"
            r"|\bDecompressionBombError\b|\bPdfiumError\b|Failed to load (document|page)",
            0,
            FailureExplanation(
                FailureFamily.unreadable,
                "The file's image or PDF data is damaged and could not be decoded.",
                "Check that the file opens on your computer, then add a repaired copy.",
            ),
        ),
        (
            r"\bUnicodeDecodeError\b|codec can't decode",
            0,
            FailureExplanation(
                FailureFamily.unreadable,
                "The file's text could not be decoded.",
                "Re-save the file with UTF-8 encoding, then add it again.",
            ),
        ),
        # Apple Intelligence (summaries only): the document, not the service.
        (
            r"\bAppleIntelligence(ContentRejected|ContextWindow|Document)Error\b",
            0,
            FailureExplanation(
                FailureFamily.other,
                "Apple Intelligence could not summarize this document. It is still indexed and searchable.",
                "Retry the summary, or use a local model for summaries.",
            ),
        ),
        # The terminal embed failure since #862: the embedder stayed unavailable
        # through every retry-later attempt (worker/entry.py).
        (
            r"^Embedder unavailable: gave up|\bEmbedderUnavailableError\b",
            0,
            FailureExplanation(
                FailureFamily.temporary,
                "The embedding service was unavailable, so this document could not be indexed. "
                "The file itself is probably fine.",
                "Reprocess the document once the embedding service is running again.",
            ),
        ),
        (
            r"\bAppleIntelligenceUnavailableError\b",
            0,
            FailureExplanation(
                FailureFamily.temporary,
                "Apple Intelligence was unavailable. The document is still indexed and searchable.",
                "Retry the summary later.",
            ),
        ),
        # The worker's stage alarm (worker/entry.py). Still temporary, but a file that is
        # simply too big to finish in time times out every run, so the action says so.
        (
            r"^TimeoutError: Stage \w+ timed out\b",
            0,
            FailureExplanation(
                FailureFamily.temporary,
                "A processing step took too long and was stopped, usually because the machine or a service was busy.",
                "Reprocess to try again. If it times out again, the file may be too large to process in time.",
            ),
        ),
        # Other timeouts: an HTTP or socket timeout.
        (
            r"\b(TimeoutError|ReadTimeout|ConnectTimeout|WriteTimeout|PoolTimeout|TimeoutException)\b|timed out",
            0,
            FailureExplanation(
                FailureFamily.temporary,
                "A processing step took too long and was stopped, usually because the machine or a "
                "service was busy. The file itself is probably fine.",
                _REPROCESS,
            ),
        ),
        # A service that could not be reached or dropped the connection.
        (
            r"\b(ConnectError|ReadError|WriteError|RemoteProtocolError|ConnectionError|ConnectionRefusedError"
            r"|ConnectionResetError|BrokenPipeError)\b|Connection refused|Connection reset|Server disconnected"
            r"|could not connect to server|Temporary failure in name resolution",
            re.IGNORECASE,
            FailureExplanation(
                FailureFamily.temporary,
                "A background service could not be reached while this file was processed. "
                "The file itself is probably fine.",
                _REPROCESS,
            ),
        ),
        # 5xx, 429 and explicit retry-later answers. "Unavailable" counts only in the shapes a
        # service writes ("503 Service Unavailable", "temporarily unavailable", and the
        # embedder and Apple Intelligence types above), never bare: "language pack fra
        # unavailable" is not an outage. Status codes match only in an HTTP
        # context (httpx's "Server error '503 …'", the embedder's "HTTP 503"), never a
        # bare number: "IndexError: index 429 is out of bounds" is not a rate limit.
        (
            r"Server error '5\d\d|\bHTTP 5\d\d\b|Internal Server Error|Bad Gateway|Service Unavailable"
            r"|Gateway Timeout|\bHTTP 429\b|'429 |Too Many Requests|try again later|retry later|temporarily unavailable",
            re.IGNORECASE,
            FailureExplanation(
                FailureFamily.temporary,
                "A background service reported a temporary error while this file was processed. "
                "The file itself is probably fine.",
                _REPROCESS,
            ),
        ),
    )
)

_FALLBACK = FailureExplanation(
    FailureFamily.other,
    "Processing failed for an unexpected reason.",
    "Reprocess the document. If it fails again, the technical details can help diagnose it.",
)


def explain_failure(error: str | None) -> FailureExplanation | None:
    """Classify a stored pipeline error; None when there is no error text."""
    if error is None or not error.strip():
        return None
    # Match on the text with path tokens replaced, so a folder or file name ("/Users/a/
    # unavailable docs/x.pdf") never decides the family.
    text = redact_path_tokens(error)
    for pattern, explanation in _RULES:
        if pattern.search(text):
            return explanation
    return _FALLBACK

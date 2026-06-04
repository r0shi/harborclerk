from datetime import UTC, datetime
from uuid import uuid4

from harbor_clerk.models.document import Document
from harbor_clerk.models.enums import PipelineStatus
from harbor_clerk.models.watched import WatchedFile, WatchedFolder
from harbor_clerk.source_ref import (
    build_source_ref,
    format_document_citation,
    format_email_citation,
    format_pages,
)


def _doc(**kwargs) -> Document:
    defaults = {
        "doc_id": uuid4(),
        "title": "Contract A",
        "canonical_filename": "contract-a.pdf",
        "status": "active",
        "sha256": b"x" * 32,
        "pipeline_status": PipelineStatus.ready,
        "doc_metadata": {},
    }
    defaults.update(kwargs)
    return Document(**defaults)


def test_format_pages() -> None:
    assert format_pages(None) is None
    assert format_pages(4) == "4"
    assert format_pages(4, 4) == "4"
    assert format_pages(4, 5) == "4-5"


def test_format_document_citation_single_page_and_range() -> None:
    assert format_document_citation("Contract A", "4") == "Contract A, p. 4"
    assert format_document_citation("Contract A", "4-5") == "Contract A, pp. 4-5"
    assert format_document_citation("Contract A") == "Contract A"


def test_document_source_ref() -> None:
    doc = _doc(title="Contract A")
    ref = build_source_ref(doc=doc, chunk_id=str(uuid4()), pages="4-5", section="Termination")

    payload = ref.to_dict()
    assert payload["doc_id"] == str(doc.doc_id)
    assert payload["doc_title"] == "Contract A"
    assert payload["source_kind"] == "document"
    assert payload["source_label"] == "Contract A"
    assert payload["citation"] == "Contract A, pp. 4-5"
    assert payload["pages"] == "4-5"
    assert payload["section"] == "Termination"


def test_document_title_falls_back_to_filename_then_untitled() -> None:
    with_filename = _doc(title="", canonical_filename="fallback.pdf")
    assert build_source_ref(doc=with_filename).citation == "fallback.pdf"

    untitled = _doc(title="", canonical_filename=None)
    assert build_source_ref(doc=untitled).citation == "Untitled document"


def test_email_citation_uses_native_metadata() -> None:
    sent = datetime(2025, 3, 7, 14, 30, tzinfo=UTC)
    doc = _doc(
        title="Budget follow-up",
        canonical_filename="budget-follow-up.eml",
        mime_type="message/rfc822",
        email_from_name="Jane Doe",
        email_from_address="jane@example.com",
        email_subject="Budget follow-up",
        email_date_sent=sent,
    )

    ref = build_source_ref(doc=doc)

    assert ref.source_kind == "email"
    assert ref.citation == 'Email from Jane Doe, "Budget follow-up", Mar 7, 2025'
    assert ref.source_label == ref.citation


def test_email_citation_handles_missing_sender() -> None:
    assert (
        format_email_citation(subject="Budget follow-up", date_sent="2025-03-07T00:00:00+00:00")
        == 'Email, "Budget follow-up", Mar 7, 2025'
    )


def test_email_source_ref_falls_back_to_tika_metadata() -> None:
    doc = _doc(
        title="legacy",
        canonical_filename="legacy.eml",
        mime_type="message/rfc822",
        email_subject=None,
        doc_metadata={
            "tika": {
                "email_from": "alice@example.com",
                "email_subject": "Legacy email",
                "email_date": "Wed, 28 May 2026 12:00:00 +0000",
            }
        },
    )

    ref = build_source_ref(doc=doc)

    assert ref.source_kind == "email"
    assert ref.citation == 'Email from alice@example.com, "Legacy email", May 28, 2026'


def test_eml_with_unparsed_metadata_falls_back_to_file_metadata() -> None:
    doc = _doc(
        title="legacy",
        canonical_filename="legacy.eml",
        mime_type="message/rfc822",
        email_subject=None,
        doc_metadata={},
    )

    ref = build_source_ref(doc=doc)

    assert ref.source_kind == "email"
    assert ref.citation == "legacy"


def test_attachment_citation_includes_parent_email() -> None:
    parent = _doc(
        title="Budget follow-up",
        mime_type="message/rfc822",
        email_from_name="Jane Doe",
        email_subject="Budget follow-up",
        email_date_sent=datetime(2025, 3, 7, tzinfo=UTC),
    )
    child = _doc(
        title="invoice.pdf",
        canonical_filename="invoice.pdf",
        mime_type="application/pdf",
        email_parent_doc_id=parent.doc_id,
    )

    ref = build_source_ref(doc=child, pages="2", parent_email_doc=parent)

    assert ref.source_kind == "attachment"
    assert ref.source_label == "invoice.pdf"
    assert ref.citation == 'Attachment "invoice.pdf", p. 2, to Email from Jane Doe, "Budget follow-up", Mar 7, 2025'


def test_folder_label_and_relative_path_are_included_without_absolute_path() -> None:
    doc = _doc(title="NDA")
    folder = WatchedFolder(path="/Users/alex/private/contracts", display_name="Contracts")
    watched_file = WatchedFile(
        folder_id=uuid4(),
        relative_path="client-a/nda.pdf",
        bookmark_data=b"",
        sha256=b"x" * 32,
        doc_id=doc.doc_id,
    )

    payload = build_source_ref(doc=doc, watched_file=watched_file, watched_folder=folder).to_dict()

    assert payload["folder_label"] == "Contracts"
    assert payload["relative_path"] == "client-a/nda.pdf"
    assert "/Users/alex" not in str(payload)


def test_absolute_relative_path_degrades_to_filename() -> None:
    doc = _doc(title="")
    watched_file = WatchedFile(
        folder_id=uuid4(),
        relative_path="/Users/alex/private/secret.pdf",
        bookmark_data=b"",
        sha256=b"x" * 32,
        doc_id=doc.doc_id,
    )

    payload = build_source_ref(doc=doc, watched_file=watched_file).to_dict()

    assert payload["relative_path"] == "secret.pdf"
    assert "/Users/alex" not in str(payload)


def test_windows_absolute_relative_path_degrades_to_filename() -> None:
    doc = _doc(title="")
    watched_file = WatchedFile(
        folder_id=uuid4(),
        relative_path=r"C:\Users\alex\private\secret.pdf",
        bookmark_data=b"",
        sha256=b"x" * 32,
        doc_id=doc.doc_id,
    )

    payload = build_source_ref(doc=doc, watched_file=watched_file).to_dict()

    assert payload["relative_path"] == "secret.pdf"
    assert "C:" not in str(payload)
    assert "Users" not in str(payload)


def test_to_dict_omits_empty_optional_fields() -> None:
    payload = build_source_ref(doc=_doc()).to_dict()

    assert "chunk_id" not in payload
    assert "pages" not in payload
    assert "section" not in payload
    assert "None" not in str(payload)
    assert "null" not in str(payload)

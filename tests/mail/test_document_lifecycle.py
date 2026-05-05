"""Lifecycle: watched_message unlabeled → Documents soft-deleted."""

from datetime import UTC, datetime

from harbor_clerk.mail.document_lifecycle import (
    restore_documents_for_relabeled,
    soft_delete_documents_for_unlabeled,
)
from harbor_clerk.models import Document, WatchedMessage
from harbor_clerk.models.enums import PipelineStatus


async def test_soft_delete_marks_email_and_attachments_as_deleted(db_session, watched_label):
    # Set up: 1 email Doc + 2 attachment Docs + a watched_message linking them, all 'active'
    email_doc = Document(
        title="Email",
        canonical_filename="email.eml",
        sha256=b"\x00" * 32,
        pipeline_status=PipelineStatus.ready,
        mime_type="message/rfc822",
        status="active",
        email_message_id="<unlabel@example.com>",
    )
    db_session.add(email_doc)
    await db_session.flush()

    attach1 = Document(
        title="a.pdf",
        canonical_filename="a.pdf",
        sha256=b"\x11" * 32,
        pipeline_status=PipelineStatus.ready,
        mime_type="application/pdf",
        status="active",
        email_parent_doc_id=email_doc.doc_id,
    )
    attach2 = Document(
        title="b.pdf",
        canonical_filename="b.pdf",
        sha256=b"\x22" * 32,
        pipeline_status=PipelineStatus.ready,
        mime_type="application/pdf",
        status="active",
        email_parent_doc_id=email_doc.doc_id,
    )
    db_session.add_all([attach1, attach2])
    await db_session.flush()

    msg = WatchedMessage(
        label_id=watched_label.label_id,
        message_id="<unlabel@example.com>",
        imap_uid=1,
        eml_sha256=b"\x00" * 32,
        email_doc_id=email_doc.doc_id,
        status="unlabeled",  # already transitioned by Stage 2's lifecycle.detect
        unlabeled_at=datetime.now(UTC),
    )
    db_session.add(msg)
    await db_session.flush()

    deleted = await soft_delete_documents_for_unlabeled(db_session, watched_label)
    await db_session.commit()

    assert deleted == 3  # 1 email + 2 attachments

    await db_session.refresh(email_doc)
    await db_session.refresh(attach1)
    await db_session.refresh(attach2)
    assert email_doc.status == "deleted"
    assert attach1.status == "deleted"
    assert attach2.status == "deleted"


async def test_soft_delete_skips_docs_still_referenced_by_other_labels(db_session, mail_account):
    """If an email is in two labels and only one un-labels, the Documents
    must NOT be deleted — the other label still references them."""
    from harbor_clerk.models import WatchedLabel

    label_a = WatchedLabel(account_id=mail_account.account_id, label_path="A", display_name="A")
    label_b = WatchedLabel(account_id=mail_account.account_id, label_path="B", display_name="B")
    db_session.add_all([label_a, label_b])
    await db_session.flush()

    email_doc = Document(
        title="shared",
        canonical_filename="shared.eml",
        sha256=b"\xff" * 32,
        pipeline_status=PipelineStatus.ready,
        mime_type="message/rfc822",
        status="active",
        email_message_id="<shared@example.com>",
    )
    db_session.add(email_doc)
    await db_session.flush()

    # Both labels reference the same email_doc
    msg_a = WatchedMessage(
        label_id=label_a.label_id,
        message_id="<shared@example.com>",
        imap_uid=1,
        eml_sha256=b"\xff" * 32,
        email_doc_id=email_doc.doc_id,
        status="unlabeled",
        unlabeled_at=datetime.now(UTC),
    )
    msg_b = WatchedMessage(
        label_id=label_b.label_id,
        message_id="<shared@example.com>",
        imap_uid=99,
        eml_sha256=b"\xff" * 32,
        email_doc_id=email_doc.doc_id,
        status="active",
    )
    db_session.add_all([msg_a, msg_b])
    await db_session.flush()

    deleted = await soft_delete_documents_for_unlabeled(db_session, label_a)
    await db_session.commit()

    assert deleted == 0  # email_doc still actively referenced by label_b

    await db_session.refresh(email_doc)
    assert email_doc.status == "active"


async def test_restore_documents_for_relabeled(db_session, watched_label):
    """If a previously-unlabeled message comes back, restore its Documents."""
    email_doc = Document(
        title="restored",
        canonical_filename="restored.eml",
        sha256=b"\x33" * 32,
        pipeline_status=PipelineStatus.ready,
        mime_type="message/rfc822",
        status="deleted",  # was soft-deleted earlier
        email_message_id="<restore@example.com>",
    )
    db_session.add(email_doc)
    await db_session.flush()

    msg = WatchedMessage(
        label_id=watched_label.label_id,
        message_id="<restore@example.com>",
        imap_uid=1,
        eml_sha256=b"\x33" * 32,
        email_doc_id=email_doc.doc_id,
        status="active",  # came back via re-label
        unlabeled_at=None,
    )
    db_session.add(msg)
    await db_session.flush()

    restored = await restore_documents_for_relabeled(db_session, watched_label)
    await db_session.commit()

    assert restored == 1
    await db_session.refresh(email_doc)
    assert email_doc.status == "active"

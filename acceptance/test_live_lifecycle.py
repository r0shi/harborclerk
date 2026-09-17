"""Live checks E and H: Ask with citations; deletion leaves every surface.
IDs match docs/superpowers/specs/2026-09-16-acceptance-suite-design.md.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from acceptance.access import populated_folder_session
from acceptance.conftest import Corpus, KeyFactory
from acceptance.fixtures.render import source_text
from acceptance.hc_client import HarborClerk, tool_error, tool_json

pytestmark = pytest.mark.acceptance

DELETION_TARGET = "deletion-target.txt"


# ── E. Ask ──────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def active_model(cfg, admin: HarborClerk) -> str:
    """The model Ask will answer with. An already-active, ready model is used as
    found: nothing to swap, nothing to restore. Otherwise the smallest
    downloaded model is activated only when the run may change the instance
    (HC_ACCEPTANCE_DISPOSABLE or HC_ACCEPTANCE_ALLOW_MODEL_SWAP), and it stays
    active: deactivating is on the suite's never-called list."""
    status = admin.model_status()
    if status.get("state") == "ready" and status.get("model_id"):
        return status["model_id"]
    downloaded = [m for m in admin.list_models() if m.get("downloaded")]
    if not downloaded:
        pytest.skip("no local model is downloaded on this instance; the Ask checks need one")
    if not cfg.allow_model_swap:
        pytest.skip(
            "no model is active; activating one changes the instance, allowed only with "
            "HC_ACCEPTANCE_DISPOSABLE=1 or HC_ACCEPTANCE_ALLOW_MODEL_SWAP=1"
        )
    smallest = min(downloaded, key=lambda m: m["size_bytes"])
    admin.activate_model(smallest["id"])
    admin.wait_for_model_ready(smallest["id"], timeout_s=cfg.ask_timeout_s)
    return smallest["id"]


def test_e1_active_model_reports_ready_repeatedly(admin: HarborClerk, active_model: str, cfg) -> None:
    admin.wait_for_model_ready(active_model, timeout_s=60, consecutive=3, poll_s=1.0)
    listed = {m["id"]: m for m in admin.list_models()}
    assert listed[active_model]["active"] and listed[active_model]["downloaded"], listed[active_model]


def _ask(admin: HarborClerk, corpus: Corpus, question: str, timeout_s: float) -> tuple[dict, str]:
    """One scoped Ask; returns the `done` event and the concatenated answer text.
    The conversation is deleted afterwards so the run leaves none behind."""
    conv = admin.create_conversation("acceptance", scope=corpus.scope)
    try:
        events = admin.stream_ask(conv, question, timeout_s=timeout_s)
    finally:
        try:
            admin.delete_conversation(conv)
        except httpx.HTTPStatusError:
            pass
    done = next((e for e in events if e.get("type") == "done"), None)
    assert done is not None, f"no done event in {len(events)} events; last: {events[-1] if events else None}"
    text = "".join(e.get("content") or e.get("delta") or e.get("text") or "" for e in events if e.get("type") != "done")
    return done, text


def test_e2_ask_answers_with_citations_from_the_fixture_folder(
    admin: HarborClerk, corpus: Corpus, active_model: str, cfg
) -> None:
    done, _text = _ask(
        admin,
        corpus,
        "How much notice must the tenant give to renew the lease at 14 Harbourside Lane? Cite the document.",
        cfg.ask_timeout_s,
    )
    assert done.get("model_id") in (None, active_model), done.get("model_id")
    citations = (done.get("rag_context") or {}).get("citations") or []
    assert citations, f"the answer carried no citations: {done}"
    cited = {c["doc_id"] for c in citations}
    assert cited <= corpus.doc_ids, f"a scoped Ask cited documents outside the folder: {cited - corpus.doc_ids}"
    lease_ids = {corpus.doc_id("lease-agreement.pdf"), corpus.doc_id("lease-agreement-scan.pdf")}
    assert cited & lease_ids, {corpus.name_of(d) for d in cited}
    for c in citations:
        assert c.get("citation") and c.get("doc_title"), c


# ── H. Deletion ─────────────────────────────────────────────────────────────


def _ensure_deleted(admin: HarborClerk, corpus: Corpus) -> str:
    """Soft-delete the deletion-target fixture if it is still there; idempotent
    so H1 and the Ask follow-up do not depend on each other's order."""
    doc_id = corpus.doc_id(DELETION_TARGET)
    if admin.request("GET", f"/api/docs/{doc_id}").status_code == 200:
        admin.delete_document(doc_id)
    return doc_id


def test_h1_soft_deleted_document_leaves_every_retrieval_surface(
    admin: HarborClerk, corpus: Corpus, keys: KeyFactory, mcp
) -> None:
    """Regression for #621/#622: the chunks used to stay retrievable."""
    phrase = corpus.groundtruth["fixtures"][DELETION_TARGET]["unique_phrase"]
    doc_id = corpus.doc_id(DELETION_TARGET)
    key = keys.create("h1-full", scope_folder_ids=[corpus.folder_id])["raw_key"]
    session = mcp.bearer(key)
    before = admin.search(phrase, scope=corpus.scope, text_contains=phrase, k=5)["hits"]
    assert before and {h["doc_id"] for h in before} == {doc_id}, "the target must be retrievable before deletion"
    chunk_id = before[0]["chunk_id"]
    mcp_before = tool_json(session.call_tool("kb_search", {"query": phrase, "k": 10}))["hits"]
    assert doc_id in {h["doc_id"] for h in mcp_before}, "MCP must retrieve the target before deletion"

    _ensure_deleted(admin, corpus)

    # Hybrid search still returns the nearest *other* chunks for any query, so
    # the contract is "the deleted document is absent", not "no hits".
    detail = admin.request("GET", f"/api/docs/{doc_id}")
    assert detail.status_code == 404 or detail.json().get("status") == "deleted", detail.text[:200]
    assert not admin.search(phrase, scope=corpus.scope, text_contains=phrase, k=5)["hits"], "REST search"
    rest_hits = admin.search(phrase, scope=corpus.scope, k=20)["hits"]
    assert doc_id not in {h["doc_id"] for h in rest_hits}, "REST search without a text filter"
    assert not admin.find_all(phrase, scope=corpus.scope, text_contains=phrase)["results"], "REST Find All"
    assert not admin.list_documents(doc_ids=doc_id, limit=5)["items"], "document list"
    mcp_hits = tool_json(session.call_tool("kb_search", {"query": phrase, "k": 20}))["hits"]
    assert doc_id not in {h["doc_id"] for h in mcp_hits}, "kb_search still returns the deleted document"
    passages = session.call_tool("kb_read_passages", {"chunk_ids": [chunk_id]})
    err = tool_error(passages)
    assert err or not tool_json(passages)["passages"], "kb_read_passages still returned the deleted chunk"
    rest_passages = admin.request("POST", "/api/passages/read", json={"chunk_ids": [chunk_id]})
    assert rest_passages.status_code >= 400 or not rest_passages.json()["passages"], "REST passages/read"


def test_h1b_ask_does_not_cite_a_deleted_document(admin: HarborClerk, corpus: Corpus, active_model: str, cfg) -> None:
    phrase = corpus.groundtruth["fixtures"][DELETION_TARGET]["unique_phrase"]
    doc_id = _ensure_deleted(admin, corpus)
    done, _ = _ask(admin, corpus, f"What does the note about the {phrase} say? Cite the document.", cfg.ask_timeout_s)
    cited = {c["doc_id"] for c in (done.get("rag_context") or {}).get("citations") or []}
    assert doc_id not in cited, "Ask cited a soft-deleted document"


def test_h2_deleting_a_folder_removes_its_documents_and_keeps_the_audit_trail(
    cfg, admin: HarborClerk, keys: KeyFactory, mcp
) -> None:
    name = f"{cfg.folder_name}-h2"
    with populated_folder_session(
        admin,
        cfg.folder_root / name,
        str(Path(cfg.folder_path_in_instance).parent / name),
        source_text=source_text("second-folder-note.txt"),
        timeout_s=cfg.ingest_timeout_s,
    ) as folder:
        key = keys.create("h2-scoped", scope_folder_ids=[folder.folder_id])
        hits = tool_json(mcp.bearer(key["raw_key"]).call_tool("kb_search", {"query": folder.phrase, "k": 5}))["hits"]
        assert {h["doc_id"] for h in hits} == {folder.doc_id}, hits
        rows_before = admin.key_requests(key["key_id"], page_size=50)["items"]
        assert rows_before, "the search was not logged"

        admin.folder_delete(folder.folder_id)  # the session's own teardown tolerates the 404 that follows

        detail = admin.request("GET", f"/api/docs/{folder.doc_id}")
        assert detail.status_code == 404 or detail.json().get("status") == "deleted", detail.text[:200]
        assert not admin.list_documents(doc_ids=folder.doc_id, limit=5)["items"]
        assert folder.folder_id not in {f["folder_id"] for f in admin.folder_list()}
        rows_after = admin.key_requests(key["key_id"], page_size=50)["items"]
        assert len(rows_after) >= len(rows_before), "deleting the folder removed request-log rows"

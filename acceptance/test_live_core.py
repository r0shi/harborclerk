"""Live checks A–D: setup and auth, ingest, search and Find All, documents and
citations. IDs match docs/superpowers/specs/2026-09-16-acceptance-suite-design.md.

Every search carries the fixture folder's scope unless the check is about
scope, so a run on an instance with other documents asserts only on its own.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import time

import pytest

from acceptance.conftest import Corpus, KeyFactory, documents_under
from acceptance.hc_client import FOREGROUND_STAGES, HarborClerk, tool_text

pytestmark = pytest.mark.acceptance

LEASE_DOCS = ("lease-agreement.pdf", "lease-agreement-scan.pdf")
# What /api/system/health itself counts as healthy (system.py health_check).
HEALTHY_CHECK_STATES = {"ok", "disabled", "not_probed"}


def _doc_ids(hits: list[dict]) -> set[str]:
    return {h["doc_id"] for h in hits}


# ── A. Setup and auth ───────────────────────────────────────────────────────


def test_a1_health_and_setup_status(admin: HarborClerk) -> None:
    health = admin.health()
    assert health["status"] == "healthy", health
    assert set(health["checks"].values()) <= HEALTHY_CHECK_STATES, health["checks"]
    assert admin.setup_status()["needs_setup"] is False


def test_a2_login_sets_refresh_cookie_and_refresh_rotates(cfg, admin: HarborClerk) -> None:
    fresh = HarborClerk(cfg.api_base, verify=not cfg.insecure)
    try:
        first = fresh.login(cfg.username, cfg.password)
        assert first["token_type"] == "bearer" and first["user"]["role"] == "admin"
        assert fresh.cookie("refresh_token"), "login must set the httponly refresh cookie"
        # Tokens carry second-granularity iat/exp and no jti, so a refresh in
        # the same second as the login is byte-identical. Cross the boundary.
        time.sleep(1.1)
        second = fresh.refresh()
        assert second["access_token"] and second["access_token"] != first["access_token"]
        assert fresh.me()["email"].lower() == cfg.username.lower()
    finally:
        fresh.close()


def test_a3_api_key_is_refused_on_human_only_routes(admin: HarborClerk, keys: KeyFactory, cfg) -> None:
    key = admin.with_key(keys.create("a3-full")["raw_key"])
    try:
        conv = key.request("POST", "/api/chat/conversations", json={"title": "acceptance"})
        assert conv.status_code == 403, conv.text
        probe = str(cfg.folder_root / "does-not-exist-for-a3")  # a path that could never be registered
        folder = key.request("POST", "/api/watch/folders", json={"path": probe})
        assert folder.status_code == 403, folder.text
        assert key.request("GET", "/api/me").status_code == 400  # identity is a human concept
    finally:
        key.close()


# ── B. Ingest and status ────────────────────────────────────────────────────


def test_b1_folder_ingested_completely(admin: HarborClerk, corpus: Corpus) -> None:
    expected = sum(1 for f in corpus.fixtures.values() if not f.unsupported)
    progress = admin.folder_progress(corpus.folder_id)
    assert progress["scan_status"] == "idle"
    assert progress["total_files"] == expected, progress
    assert progress["completed_files"] == expected, progress
    for stage, counts in progress["by_stage"].items():
        if stage != "summarize":
            assert counts["error"] == 0 and counts["pending"] == 0 and counts["running"] == 0, (stage, counts)


def test_b2_every_fixture_reached_ready(admin: HarborClerk, corpus: Corpus) -> None:
    missing = [n for n, f in corpus.fixtures.items() if not f.unsupported and n not in corpus.docs]
    assert not missing, f"no document row for {missing}; documents under folder: {sorted(corpus.docs)}"
    for name, fixture in corpus.fixtures.items():
        if fixture.unsupported:
            continue
        doc = admin.get_document(corpus.doc_id(name))
        assert doc["pipeline_status"] == "ready", (name, doc["pipeline_status"], doc.get("error"))
    text_pdf = admin.get_document(corpus.doc_id("lease-agreement.pdf"))
    scan_pdf = admin.get_document(corpus.doc_id("lease-agreement-scan.pdf"))
    assert text_pdf["has_text_layer"] is True and not text_pdf["needs_ocr"]
    assert scan_pdf["needs_ocr"] is True, "the image-only PDF must have gone through OCR"
    ocr_text = " ".join(p["text"] for p in _pages(admin.document_content(corpus.doc_id("lease-agreement-scan.pdf"))))
    assert "Harbourside" in ocr_text and "Marlowe" in ocr_text, ocr_text[:300]


def _pages(content: dict) -> list[dict]:
    return content["pages"]  # DocumentContentResponse


def test_b3_no_fixture_stage_errored(admin: HarborClerk, corpus: Corpus) -> None:
    """`status-summary` is instance-wide with no folder attribution, so the
    folder's own progress is the source of truth here."""
    progress = admin.folder_progress(corpus.folder_id)
    errored = {stage: c["error"] for stage, c in progress["by_stage"].items() if stage != "summarize" and c["error"]}
    assert not errored, errored
    status = {n: admin.get_document(d["doc_id"])["pipeline_status"] for n, d in corpus.docs.items()}
    assert all(v == "ready" for v in status.values()), {n: v for n, v in status.items() if v != "ready"}


def test_b4_reprocess_returns_to_ready(admin: HarborClerk, corpus: Corpus, cfg) -> None:
    doc_id = corpus.doc_id("vendor-notes.md")
    admin.reprocess_document(doc_id)
    doc = admin.wait_for_document_ready(doc_id, timeout_s=cfg.ingest_timeout_s)
    by_stage = {j["stage"]: j["status"] for j in doc["jobs"]}
    for stage in FOREGROUND_STAGES:
        assert by_stage.get(stage) == "done", (stage, by_stage)


def test_b5_unsupported_extension_is_skipped_not_failed(admin: HarborClerk, corpus: Corpus) -> None:
    folder = next(f for f in admin.folder_list() if f["folder_id"] == corpus.folder_id)
    assert folder["skipped_count"] >= 1, folder
    assert any(ext.lstrip(".") == "xyz" for ext in folder["skipped_extensions"]), folder["skipped_extensions"]
    assert "notes.xyz" not in corpus.docs


def test_b6_source_files_are_byte_identical_after_ingest(corpus: Corpus) -> None:
    """The product reads files in place and never modifies a source."""
    now = {name: hashlib.sha256(f.path.read_bytes()).hexdigest() for name, f in corpus.fixtures.items()}
    changed = {
        name: (corpus.source_digests[name], now[name]) for name in now if now[name] != corpus.source_digests[name]
    }
    assert not changed, f"ingest modified source files: {changed}"
    on_disk = {p.name for p in corpus.fixtures["notes.xyz"].path.parent.iterdir()}
    assert set(corpus.fixtures) <= on_disk, (
        f"ingest removed files from the watched folder: {set(corpus.fixtures) - on_disk}"
    )


def test_b7_a_file_added_after_registration_is_detected_and_ingested(admin: HarborClerk, corpus: Corpus, cfg) -> None:
    """The watcher's live path, as distinct from the initial scan B1 exercised."""
    marker = f"late addition marker {cfg.run_id}"
    late = corpus.fixtures["notes.xyz"].path.parent / "late-addition.txt"
    late.write_text(f"LATE ADDITION\n\nWritten after the folder was registered. {marker}.\n", encoding="utf-8")
    deadline = time.monotonic() + cfg.ingest_timeout_s
    row = None
    while time.monotonic() < deadline and row is None:
        row = documents_under(admin, corpus.folder_path).get("late-addition.txt")
        if row is None:
            time.sleep(3)
    assert row is not None, "the watcher never picked up a file added after registration"
    corpus.docs["late-addition.txt"] = row
    admin.wait_for_document_ready(row["doc_id"], timeout_s=cfg.ingest_timeout_s)
    hits = admin.search(marker, scope=corpus.scope, text_contains=marker, k=5)["hits"]
    assert {h["doc_id"] for h in hits} == {row["doc_id"]}


# ── C. Search and Find All ──────────────────────────────────────────────────


def test_c1_known_answer_query_returns_cited_hits(admin: HarborClerk, corpus: Corpus) -> None:
    q = corpus.groundtruth["queries"]["known_answer"]
    resp = admin.search(q["query"], scope=corpus.scope, k=10)
    assert resp["hits"], resp
    lease_ids = {corpus.doc_id(n) for n in LEASE_DOCS}
    assert resp["hits"][0]["doc_id"] in lease_ids, corpus.name_of(resp["hits"][0]["doc_id"])
    for hit in resp["hits"]:
        assert hit["citation"], hit
        assert hit["source"] and hit["source"]["doc_title"], hit
        assert hit["source"]["pages"] or hit["source"]["section"] or hit["page_start"] is not None, hit["source"]
        assert isinstance(hit["score"], float)
        assert corpus.name_of(hit["doc_id"]) is not None, "scoped search returned a document outside the folder"


def test_c2_french_query_reaches_the_french_document(admin: HarborClerk, corpus: Corpus) -> None:
    q = corpus.groundtruth["queries"]["french"]
    resp = admin.search(q["query"], scope=corpus.scope, k=5)
    assert resp["hits"], resp
    top = resp["hits"][0]
    assert top["doc_id"] == corpus.doc_id(q["expected_top"]), corpus.name_of(top["doc_id"])
    assert top["language"] == "fr"


def test_c3_filters_narrow_correctly(admin: HarborClerk, corpus: Corpus) -> None:
    gt = corpus.groundtruth
    # scope: every hit stays inside the folder
    scoped = admin.search("Harbourside Lane", scope=corpus.scope, k=50)
    assert scoped["hits"] and _doc_ids(scoped["hits"]) <= corpus.doc_ids
    # language
    fr = admin.search("bail loyer", scope=corpus.scope, language="fr", k=20)
    assert fr["hits"] and all(h["language"] == "fr" for h in fr["hits"])
    en = admin.search("lease rent", scope=corpus.scope, language="en", k=20)
    assert corpus.doc_id("bail-commercial.txt") not in _doc_ids(en["hits"])
    # mime type (Find All carries mime_type per result)
    pdfs = admin.find_all("lease", scope=corpus.scope, mime_type="application/pdf", presentation="brief")
    assert pdfs["results"] and all(r["mime_type"] == "application/pdf" for r in pdfs["results"])
    # exact text
    phrase = gt["queries"]["known_answer"]["exact_phrase"]
    exact = admin.search("renewal", scope=corpus.scope, text_contains=phrase, k=20)
    assert exact["hits"] and _doc_ids(exact["hits"]) <= {corpus.doc_id(n) for n in LEASE_DOCS}
    assert not admin.search("renewal", scope=corpus.scope, text_contains="zzz-not-in-any-fixture", k=5)["hits"]
    # dates filter on Document.created_at (the Date header for emails, ingest time otherwise); nothing is dated tomorrow
    tomorrow = (dt.datetime.now(dt.UTC) + dt.timedelta(days=1)).isoformat()
    assert not admin.search("Harbourside", scope=corpus.scope, after=tomorrow, k=5)["hits"]
    assert admin.search("Harbourside", scope=corpus.scope, before=tomorrow, k=5)["hits"]
    # email sender
    email = gt["email"]
    by_sender = admin.search(
        "invoice", scope=corpus.scope, metadata_filter={"email.from_address": email["from_address"]}, k=10
    )
    assert by_sender["hits"], by_sender
    assert all(h["source"]["source_kind"] in ("email", "attachment") for h in by_sender["hits"]), by_sender["hits"]
    nobody = admin.search(
        "invoice", scope=corpus.scope, metadata_filter={"email.from_address": "nobody@example.invalid"}
    )
    assert not nobody["hits"]
    by_to = admin.search(
        "invoice", scope=corpus.scope, metadata_filter={"email.to_addresses": email["to_addresses"][0]}
    )
    assert by_to["hits"] and all(h["source"]["source_kind"] == "email" for h in by_to["hits"])
    by_subject = admin.search("invoice", scope=corpus.scope, metadata_filter={"email.subject_contains": "NP-2025-0471"})
    assert by_subject["hits"]
    assert not admin.search("invoice", scope=corpus.scope, metadata_filter={"email.subject_contains": "zzz-no-such"})[
        "hits"
    ]
    # when the reranker service is healthy, search must actually have used it
    if admin.health()["checks"].get("reranker") == "ok":
        assert scoped["reranker_status"] == "ok", scoped["reranker_status"]


def test_c4_close_scoring_documents_surface_as_a_possible_conflict(admin: HarborClerk, corpus: Corpus) -> None:
    """`possible_conflict` means the top hits within 10% of the best score come
    from more than one document (search.py); the near-duplicate pair is the
    reliable way to produce that."""
    q = corpus.groundtruth["queries"]["conflict"]
    resp = admin.search(q["query"], scope=corpus.scope, k=10)
    expected = {corpus.doc_id(n) for n in q["expected_docs"]}
    assert resp["possible_conflict"] is True, resp
    assert expected <= {c["doc_id"] for c in resp["conflict_sources"]}, resp["conflict_sources"]


def test_c5_find_all_enumerates_exactly_the_matching_documents(admin: HarborClerk, corpus: Corpus) -> None:
    q = corpus.groundtruth["queries"]["find_all"]
    resp = admin.find_all(q["phrase"], scope=corpus.scope, text_contains=q["phrase"], presentation="full")
    names = {corpus.name_of(r["doc_id"]) for r in resp["results"]}
    assert names == set(q["expected_docs"]), names
    for r in resp["results"]:
        assert r["citation"] and r["source"] and r["top_chunk"], r


def test_c6_faceted_search_groups_by_document(admin: HarborClerk, corpus: Corpus) -> None:
    resp = admin.search("Harbourside Lane", scope=corpus.scope, faceted=True, k=50)
    assert len(resp["documents"]) >= 2, resp
    for group in resp["documents"]:
        assert group["hits"] and all(h["doc_id"] == group["doc_id"] for h in group["hits"])


def test_c7_read_passages_returns_the_exact_chunk_text(admin: HarborClerk, corpus: Corpus) -> None:
    q = corpus.groundtruth["queries"]["known_answer"]
    hit = admin.search(q["query"], scope=corpus.scope, k=1)["hits"][0]
    passages = admin.read_passages({"chunk_ids": [hit["chunk_id"]]})["passages"]
    assert len(passages) == 1 and passages[0]["chunk_text"] == hit["chunk_text"]


def test_c8_unknown_email_filter_is_a_422_not_an_empty_result(admin: HarborClerk, corpus: Corpus) -> None:
    resp = admin.request("POST", "/api/search", json={"query": "x", "metadata_filter": {"email.bogus": "y"}})
    assert resp.status_code == 422, resp.text


# ── D. Documents and citations ──────────────────────────────────────────────


def test_d1_document_detail_carries_the_operator_fields(admin: HarborClerk, corpus: Corpus) -> None:
    doc = admin.get_document(corpus.doc_id("lease-agreement.pdf"))
    assert doc["title"] and doc["canonical_filename"].endswith("lease-agreement.pdf")
    assert doc["mime_type"] == "application/pdf" and doc["pipeline_status"] == "ready"
    stages = {j["stage"]: j["status"] for j in doc["jobs"]}
    assert all(stages.get(s) == "done" for s in FOREGROUND_STAGES), stages


def test_d2_document_content_matches_the_source(admin: HarborClerk, corpus: Corpus) -> None:
    v1 = " ".join(p["text"] for p in _pages(admin.document_content(corpus.doc_id("meeting-minutes.txt"))))
    v2 = " ".join(p["text"] for p in _pages(admin.document_content(corpus.doc_id("meeting-minutes-v2.txt"))))
    assert "Attendees" in v1 and "20 May" in v1 and "27 May" not in v1
    assert "27 May" in v2 and "20 May" not in v2


def test_d3_entities_include_the_planted_names(admin: HarborClerk, corpus: Corpus) -> None:
    gt = corpus.groundtruth
    doc = admin.get_document(corpus.doc_id("lease-agreement.pdf"))
    entities_job = {j["stage"]: j["status"] for j in doc["jobs"]}.get("entities")
    if entities_job == "skipped":
        pytest.skip("entities stage was skipped on this instance (spaCy model unavailable); not an NER regression")
    resp = admin.document_entities(corpus.doc_id("lease-agreement.pdf"))
    found = " | ".join(e["entity_text"] for e in resp["entities"]).lower()
    planted = [n for n in gt["people"] + gt["organisations"] if n.lower() in found]
    assert len(planted) >= 2, f"planted names in entities: {planted}; entities: {found[:400]}"


def test_d4_email_citation_uses_email_metadata(admin: HarborClerk, corpus: Corpus) -> None:
    """An on-disk .eml is one document; attachments become child documents
    only through IMAP ingest. So the email's citation must carry sender or
    subject, and the .eml must not have produced more than one document."""
    email = corpus.groundtruth["email"]
    hits = admin.search("invoice NP-2025-0471 archival board", scope=corpus.scope, k=20)["hits"]
    mail_hits = [h for h in hits if h["source"]["source_kind"] == "email"]
    assert mail_hits, {h["source"]["source_kind"] for h in hits}
    label = (mail_hits[0]["source"]["source_label"] or "") + " " + (mail_hits[0]["citation"] or "")
    assert "NP-2025-0471" in label or email["from_name"] in label, label
    assert {h["doc_id"] for h in mail_hits} == {corpus.doc_id("invoice-thread.eml")}


def test_d5_mcp_payloads_never_contain_an_absolute_path(corpus: Corpus, keys: KeyFactory, mcp) -> None:
    raw = keys.create("d5-full", scope_folder_ids=[corpus.folder_id])["raw_key"]
    session = mcp.bearer(raw)
    forbidden = (corpus.folder_path, "/Users/", "/private/", "/home/")
    for name, args in (
        ("kb_search", {"query": "Harbourside Lane", "k": 20}),
        ("kb_find_all", {"query": "standing order"}),
        ("kb_get_document", {"doc_id": corpus.doc_id("lease-agreement.pdf")}),
    ):
        result = session.call_tool(name, args)
        text = tool_text(result)
        assert text, name
        leaked = [f for f in forbidden if f in text]
        assert not leaked, f"{name} payload contains {leaked}"


def test_d6_rest_read_routes_admit_a_search_tier_key_and_expose_source_path(
    admin: HarborClerk, corpus: Corpus, keys: KeyFactory
) -> None:
    """Records current behaviour for open question 1 of the spec and #646:
    tier is enforced only in MCP, so a search-tier key can read a document's
    detail (with the absolute `source_path`) and its full content over REST.
    If this starts failing, the behaviour changed; resolve #646 deliberately."""
    doc_id = corpus.doc_id("lease-agreement.pdf")
    key = admin.with_key(keys.create("d6-search", permission_tier="search")["raw_key"])
    try:
        doc = key.get_document(doc_id)
        content = key.request("GET", f"/api/docs/{doc_id}/content")
    finally:
        key.close()
    assert doc.get("source_path", "").startswith(corpus.folder_path), json.dumps(doc)[:300]
    assert content.status_code == 200 and "one further term" in content.text


def test_d7_document_list_filters_narrow_the_list(admin: HarborClerk, corpus: Corpus) -> None:
    fr = {d["doc_id"] for d in admin.list_documents(language="fr", limit=200)["items"]}
    assert corpus.doc_id("bail-commercial.txt") in fr and corpus.doc_id("lease-agreement.pdf") not in fr
    pdfs = {d["doc_id"] for d in admin.list_documents(mime_type="application/pdf", limit=200)["items"]}
    assert {corpus.doc_id(n) for n in LEASE_DOCS} <= pdfs and corpus.doc_id("vendor-notes.md") not in pdfs
    titled = {d["doc_id"] for d in admin.list_documents(q="handbook", limit=200)["items"]}
    assert corpus.doc_id("handbook.docx") in titled

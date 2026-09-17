"""Live checks F and G: MCP and CLI parity; API-key scope, limits and audit.
IDs match docs/superpowers/specs/2026-09-16-acceptance-suite-design.md.

Keys are scoped to the fixture folder so the MCP and CLI results being compared
cover the same documents on any instance. The CLI gate is judged by CLI exit
codes, never by the health endpoint: the API re-reads `enable_cli_access` only
when a CLI request reaches the MCP auth middleware, so health reports the
setting as of the last CLI request it saw.
"""

from __future__ import annotations

import datetime as dt
import time

import pytest

from acceptance.access import (
    EXIT_AUTH,
    EXIT_CLI_DISABLED,
    EXIT_CONNECTION,
    EXIT_OK,
    TIER_TOOLS,
    mcp_probe,
    run_cli,
)
from acceptance.conftest import Corpus, KeyFactory
from acceptance.hc_client import HarborClerk, tool_error, tool_json

pytestmark = pytest.mark.acceptance


@pytest.fixture(scope="module")
def tier_keys(keys: KeyFactory, corpus: Corpus) -> dict[str, dict]:
    """One key per tier, all scoped to the fixture folder."""
    return {
        tier: keys.create(f"tier-{tier}", permission_tier=tier, scope_folder_ids=[corpus.folder_id])
        for tier in ("search", "read", "full")
    }


def _rows(admin: HarborClerk, key_id: str) -> list[dict]:
    return admin.key_requests(key_id, page_size=100)["items"]


# ── F. MCP and CLI parity ───────────────────────────────────────────────────


@pytest.mark.parametrize("tier", ["search", "read", "full"])
def test_f1_bearer_tool_list_is_exactly_the_tier(tier: str, tier_keys: dict, mcp) -> None:
    listed = set(mcp.bearer(tier_keys[tier]["raw_key"]).list_tool_names())
    assert listed == TIER_TOOLS[tier], {"missing": TIER_TOOLS[tier] - listed, "extra": listed - TIER_TOOLS[tier]}


def test_f2_url_token_lists_the_same_tools_as_bearer(tier_keys: dict, mcp) -> None:
    raw = tier_keys["read"]["raw_key"]
    assert mcp.url_token(raw).list_tool_names() == mcp.bearer(raw).list_tool_names()


def test_f3_kb_search_and_kb_find_all_carry_citations(tier_keys: dict, corpus: Corpus, mcp) -> None:
    session = mcp.bearer(tier_keys["full"]["raw_key"])
    hits = tool_json(session.call_tool("kb_search", {"query": corpus.groundtruth["queries"]["known_answer"]["query"]}))
    assert hits["hits"], hits
    for h in hits["hits"]:
        assert h["citation"] and h["source"] and h["chunk_id"], h
        assert corpus.name_of(h["doc_id"]) is not None, "a folder-scoped key returned a document outside the folder"
    q = corpus.groundtruth["queries"]["find_all"]
    found = tool_json(
        session.call_tool("kb_find_all", {"query": q["phrase"], "text_contains": q["phrase"], "presentation": "full"})
    )
    assert {corpus.name_of(r["doc_id"]) for r in found["results"]} == set(q["expected_docs"])


def test_f4_cli_disabled_exits_3_and_is_audited(cfg, admin: HarborClerk, tier_keys: dict, cli_access) -> None:
    """The healing request after the block runs whatever happened inside it, so
    a failed assertion cannot leave the in-memory gate behind the restored file."""
    key = tier_keys["full"]
    search = ("search", "Harbourside Lane", "-k", "1")
    if cli_access.enabled:
        try:
            with cli_access.toggled(False):
                result = run_cli(cfg.api_base, key["raw_key"], *search, insecure=cfg.insecure)
        finally:
            after = run_cli(cfg.api_base, key["raw_key"], *search, insecure=cfg.insecure)
        assert after.code == EXIT_OK, f"CLI access was not restored: exit {after.code} {after.stderr[:200]}"
    else:
        result = run_cli(cfg.api_base, key["raw_key"], *search, insecure=cfg.insecure)
    assert result.code == EXIT_CLI_DISABLED, (result.code, result.stderr[:300])
    assert result.error["error_kind"] == "cli_disabled", result.stderr
    denied = [r for r in _rows(admin, key["key_id"]) if r["status_detail"] == "cli_access_disabled"]
    assert denied, "the refusal was not audited"
    assert (denied[0]["request_type"], denied[0]["status"], denied[0]["endpoint"]) == ("cli_tool", "denied", "<gate>")


def test_f5_cli_search_matches_kb_search(cfg, tier_keys: dict, corpus: Corpus, mcp, cli_access) -> None:
    raw = tier_keys["full"]["raw_key"]
    query = "Harbourside Lane"
    with cli_access.ensured():
        via_mcp = tool_json(mcp.bearer(raw).call_tool("kb_search", {"query": query, "k": 20}))
        via_cli = run_cli(cfg.api_base, raw, "search", query, "-k", "20", insecure=cfg.insecure)
    assert via_cli.code == EXIT_OK, via_cli.stderr[:300]
    mcp_hits = {h["chunk_id"]: h["citation"] for h in via_mcp["hits"]}
    cli_hits = {h["chunk_id"]: h["citation"] for h in via_cli.json["hits"]}
    diagnostics = {"mcp_only": set(mcp_hits) - set(cli_hits), "cli_only": set(cli_hits) - set(mcp_hits)}
    assert mcp_hits and set(cli_hits) == set(mcp_hits), diagnostics
    assert cli_hits == mcp_hits, "citation strings differ between CLI and MCP for the same chunks"


def test_f6_cli_find_all_and_expand_context_match_mcp(
    cfg, admin: HarborClerk, tier_keys: dict, corpus: Corpus, mcp, cli_access
) -> None:
    raw = tier_keys["full"]["raw_key"]
    session = mcp.bearer(raw)
    q = corpus.groundtruth["queries"]["find_all"]
    args = {"query": q["phrase"], "text_contains": q["phrase"], "presentation": "full"}
    with cli_access.ensured():
        via_mcp = tool_json(session.call_tool("kb_find_all", args))
        via_cli = run_cli(
            cfg.api_base,
            raw,
            "find-all",
            q["phrase"],
            "--text-contains",
            q["phrase"],
            "--presentation",
            "full",
            insecure=cfg.insecure,
        )
        assert via_cli.code == EXIT_OK, via_cli.stderr[:300]
        assert via_mcp["results"], via_mcp
        assert {r["doc_id"] for r in via_cli.json["results"]} == {r["doc_id"] for r in via_mcp["results"]}
        # expand-context: CLI and tool return the same chunk window, and the window has neighbours
        first = via_mcp["results"][0]
        chunk_id = first["top_chunk"]["chunk_id"]
        mcp_ctx = tool_json(session.call_tool("kb_expand_context", {"chunk_id": chunk_id, "n": 1}))
        cli_ctx = run_cli(cfg.api_base, raw, "expand-context", chunk_id, "-n", "1", insecure=cfg.insecure)
    assert cli_ctx.code == EXIT_OK, cli_ctx.stderr[:300]
    cli_ids = [c["chunk_id"] for c in cli_ctx.json["chunks"]]
    assert cli_ids == [c["chunk_id"] for c in mcp_ctx["chunks"]], "expand-context windows differ between CLI and MCP"
    assert chunk_id in cli_ids, "the target chunk is missing from its own expansion"
    if admin.document_outline(first["doc_id"])["chunk_count"] > 1:
        assert len(cli_ids) >= 2, "a document with several chunks must expand to at least one neighbour"


def test_f7_cli_exit_codes_for_auth_and_connection_failures(cfg) -> None:
    bad_key = run_cli(cfg.api_base, "hc_" + "0" * 48, "search", "anything", insecure=cfg.insecure)
    assert bad_key.code == EXIT_AUTH and bad_key.error["error_kind"] == "auth", (bad_key.code, bad_key.stderr[:200])
    unreachable = run_cli("http://127.0.0.1:9", "hc_" + "0" * 48, "search", "anything", timeout_s=60)
    assert unreachable.code == EXIT_CONNECTION and unreachable.error["error_kind"] == "connection", unreachable.stderr[
        :200
    ]


# ── G. API key scope, limits and audit ──────────────────────────────────────


def test_g1_out_of_tier_tool_call_is_refused_and_audited(admin: HarborClerk, tier_keys: dict, mcp) -> None:
    key = tier_keys["search"]
    result = mcp.bearer(key["raw_key"]).call_tool(
        "kb_read_passages", {"chunk_ids": ["00000000-0000-0000-0000-000000000000"]}
    )
    err = tool_error(result)
    assert err and "Unknown tool" in err, err
    denied = [r for r in _rows(admin, key["key_id"]) if r["status"] == "denied"]
    assert denied and denied[0]["status_detail"] == "tool not in key scope", denied


def test_g2_read_tier_reads_passages_and_documents_over_mcp_search_tier_cannot(
    tier_keys: dict, corpus: Corpus, mcp
) -> None:
    doc_id = corpus.doc_id("lease-agreement.pdf")
    read = mcp.bearer(tier_keys["read"]["raw_key"])
    hits = tool_json(read.call_tool("kb_search", {"query": "renewal notice", "k": 1}))["hits"]
    assert hits, "the read-tier key found nothing to read"
    hit = hits[0]
    passages = tool_json(read.call_tool("kb_read_passages", {"chunk_ids": [hit["chunk_id"]]}))["passages"]
    assert passages and passages[0]["chunk_id"] == hit["chunk_id"]
    doc = tool_json(read.call_tool("kb_get_document", {"doc_id": doc_id}))
    assert doc.get("doc_id") == doc_id, doc
    search = mcp.bearer(tier_keys["search"]["raw_key"])
    assert "Unknown tool" in (tool_error(search.call_tool("kb_get_document", {"doc_id": doc_id})) or "")


def test_g3_key_scoped_to_an_empty_folder_sees_nothing_and_says_why(keys: KeyFactory, empty_folder: dict, mcp) -> None:
    raw = keys.create("g3-empty-scope", scope_folder_ids=[empty_folder["folder_id"]])["raw_key"]
    session = mcp.bearer(raw)
    resp = tool_json(session.call_tool("kb_search", {"query": "Harbourside Lane", "k": 10}))
    assert resp["hits"] == [], resp
    assert resp.get("would_match_unscoped", 0) > 0, "an empty scoped result must say the query would match unscoped"
    found = tool_json(session.call_tool("kb_find_all", {"query": "standing order", "text_contains": "standing order"}))
    assert found["results"] == [], found


def test_g4_rate_limit_refuses_the_third_call_and_recovers(
    admin: HarborClerk, keys: KeyFactory, corpus: Corpus, mcp
) -> None:
    key = keys.create("g4-rpm2", scope_folder_ids=[corpus.folder_id], rate_limit_rpm=2)
    session = mcp.bearer(key["raw_key"])
    for _ in range(2):
        assert tool_error(session.call_tool("kb_search", {"query": "lease", "k": 1})) is None
    third = tool_error(session.call_tool("kb_search", {"query": "lease", "k": 1}))
    assert third and "Rate limit exceeded" in third, third
    limited = [r for r in _rows(admin, key["key_id"]) if r["status"] == "rate_limited"]
    assert limited, "the refusal was not audited as rate_limited"
    deadline = time.monotonic() + 75  # sliding 60 s window
    while time.monotonic() < deadline:
        if tool_error(session.call_tool("kb_search", {"query": "lease", "k": 1})) is None:
            return
        time.sleep(5)
    pytest.fail("the rate limit did not reset within 75 s")


def test_g5_expired_key_is_refused_on_both_surfaces(cfg, keys: KeyFactory, corpus: Corpus) -> None:
    expires = (dt.datetime.now(dt.UTC) + dt.timedelta(seconds=10)).isoformat()
    raw = keys.create("g5-expiring", scope_folder_ids=[corpus.folder_id], expires_at=expires)["raw_key"]
    assert mcp_probe(cfg.api_base, raw, verify=not cfg.insecure) == 200
    time.sleep(11)
    assert mcp_probe(cfg.api_base, raw, verify=not cfg.insecure) == 401
    assert mcp_probe(cfg.api_base, raw, url_token=True, verify=not cfg.insecure) == 401


def test_g6_audit_distinguishes_mcp_and_cli_calls(
    cfg, admin: HarborClerk, keys: KeyFactory, corpus: Corpus, mcp, cli_access
) -> None:
    key = keys.create("g6-audit", scope_folder_ids=[corpus.folder_id])
    assert tool_error(mcp.bearer(key["raw_key"]).call_tool("kb_search", {"query": "lease", "k": 1})) is None
    types = {(r["request_type"], r["endpoint"], r["status"]) for r in _rows(admin, key["key_id"])}
    assert ("mcp_tool", "kb_search", "ok") in types, types
    with cli_access.ensured():
        assert (
            run_cli(cfg.api_base, key["raw_key"], "search", "lease", "-k", "1", insecure=cfg.insecure).code == EXIT_OK
        )
    types = {(r["request_type"], r["endpoint"], r["status"]) for r in _rows(admin, key["key_id"])}
    assert ("cli_tool", "kb_search", "ok") in types, types


def test_g7_deleted_key_is_refused_on_both_surfaces(cfg, admin: HarborClerk, keys: KeyFactory, corpus: Corpus) -> None:
    # Through the factory so a failed probe cannot leave an active key behind;
    # the teardown's second DELETE on an already-deleted key is a harmless 204.
    key = keys.create("g7-deleted", scope_folder_ids=[corpus.folder_id])
    raw = key["raw_key"]
    assert mcp_probe(cfg.api_base, raw, verify=not cfg.insecure) == 200
    assert mcp_probe(cfg.api_base, raw, url_token=True, verify=not cfg.insecure) == 200
    admin.delete_api_key(key["key_id"])
    assert mcp_probe(cfg.api_base, raw, verify=not cfg.insecure) == 401
    assert mcp_probe(cfg.api_base, raw, url_token=True, verify=not cfg.insecure) == 401

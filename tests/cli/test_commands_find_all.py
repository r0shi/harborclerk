"""CLI tests for harbor-clerk find-all."""

import json
from unittest.mock import MagicMock, patch

from harbor_clerk.cli import main as cli_main


def _run(args, mock_response):
    """Invoke CLI in-process with a mocked McpHttpClient."""
    with (
        patch("harbor_clerk.cli.commands.find_all.McpHttpClient") as MockClient,
        patch("harbor_clerk.cli.commands.find_all.resolve_config") as MockResolve,
    ):
        instance = MagicMock()
        instance.call_tool.return_value = mock_response
        instance.__enter__.return_value = instance
        instance.__exit__.return_value = False
        MockClient.return_value = instance
        MockResolve.return_value = MagicMock(url="https://test", api_key="hc_t", insecure=False)
        rc = cli_main.main(args)
        return rc, instance


_EMPTY = {
    "results": [],
    "total_matches": 0,
    "returned": 0,
    "offset": 0,
    "truncated": False,
    "sort_by": "relevance",
    "presentation": "brief",
}


def test_find_all_defaults_with_positional_query(capsys):
    rc, client = _run(["find-all", "termination", "--json"], _EMPTY)
    assert rc == 0
    client.call_tool.assert_called_once_with(
        "kb_find_all",
        {
            "query": "termination",
            "max_results": 100,
            "offset": 0,
            "presentation": "brief",
            "sort_by": "relevance",
        },
    )
    assert json.loads(capsys.readouterr().out) == _EMPTY


def test_find_all_query_flag_and_filters_forwarded():
    rc, client = _run(
        [
            "find-all",
            "--query",
            "invoice",
            "--text-contains",
            "total due",
            "--max-results",
            "25",
            "--offset",
            "50",
            "--presentation",
            "full",
            "--sort-by",
            "date_desc",
            "--doc-ids",
            "d1,d2",
            "--after",
            "2026-01-01",
            "--before",
            "2026-02-01",
            "--language",
            "fr",
            "--mime-type",
            "message/rfc822",
            "--metadata-filter-json",
            '{"email.from_address": "alice@example.com"}',
            "--json",
        ],
        _EMPTY,
    )
    assert rc == 0
    args = client.call_tool.call_args.args[1]
    assert args == {
        "query": "invoice",
        "text_contains": "total due",
        "max_results": 25,
        "offset": 50,
        "presentation": "full",
        "sort_by": "date_desc",
        "doc_ids": ["d1", "d2"],
        "after": "2026-01-01",
        "before": "2026-02-01",
        "language": "french",
        "mime_type": "message/rfc822",
        "metadata_filter": {"email.from_address": "alice@example.com"},
    }


def test_find_all_rejects_missing_query(capsys):
    rc, client = _run(["find-all", "--json"], _EMPTY)
    assert rc == 1
    client.call_tool.assert_not_called()
    assert "requires a query" in capsys.readouterr().err


def test_find_all_rejects_duplicate_query(capsys):
    rc, client = _run(["find-all", "foo", "--query", "bar", "--json"], _EMPTY)
    assert rc == 1
    client.call_tool.assert_not_called()
    assert "not both" in capsys.readouterr().err


def test_find_all_invalid_metadata_filter_exits_1(capsys):
    rc, client = _run(["find-all", "foo", "--metadata-filter-json", "not-json", "--json"], _EMPTY)
    assert rc == 1
    client.call_tool.assert_not_called()
    assert "--metadata-filter-json must be valid JSON" in capsys.readouterr().err


def test_find_all_text_mode_renders_citation_and_top_chunk(capsys):
    payload = {
        "results": [
            {
                "doc_id": "d1",
                "doc_title": "Contract A",
                "citation": "Contract A, p. 4",
                "score": 0.87,
                "page_range": "4",
                "top_chunk": {
                    "chunk_id": "c1",
                    "text": "shall terminate",
                    "page": 4,
                    "heading": "Termination",
                },
            }
        ],
        "total_matches": 1,
        "returned": 1,
        "offset": 0,
        "truncated": False,
        "sort_by": "relevance",
        "presentation": "full",
    }
    rc, _client = _run(["find-all", "terminate", "--format", "text"], payload)
    assert rc == 0
    out = capsys.readouterr().out
    assert "1 of 1 matches" in out
    assert "Contract A, p. 4" in out
    assert "chunk=c1" in out
    assert "shall terminate" in out

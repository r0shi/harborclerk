import json
from unittest.mock import MagicMock, patch

from harbor_clerk.cli import main as cli_main


def _run(args, mock_response):
    """Invoke CLI in-process with a mocked McpHttpClient."""
    with (
        patch("harbor_clerk.cli.commands.search.McpHttpClient") as MockClient,
        patch("harbor_clerk.cli.commands.search.resolve_config") as MockResolve,
    ):
        instance = MagicMock()
        instance.call_tool.return_value = mock_response
        instance.__enter__.return_value = instance
        instance.__exit__.return_value = False
        MockClient.return_value = instance
        MockResolve.return_value = MagicMock(url="https://test", api_key="hc_t", insecure=False)
        rc = cli_main.main(args)
        return rc, instance


def test_search_calls_kb_search_with_query_and_defaults(capsys):
    rc, client = _run(["search", "termination", "--json"], {"hits": []})
    assert rc == 0
    client.call_tool.assert_called_once_with(
        "kb_search",
        {"query": "termination", "k": 10, "offset": 0, "detail": "full"},
    )
    out = capsys.readouterr().out
    assert json.loads(out) == {"hits": []}


def test_search_forwards_optional_filters(capsys):
    rc, client = _run(
        [
            "search",
            "force majeure",
            "--k",
            "5",
            "--detail",
            "compact",
            "--language",
            "en",
            "--after",
            "2026-01-01",
            "--metadata-filter",
            '{"email.from": "alice@example.com"}',
            "--text-contains",
            "force majeure",
            "--json",
        ],
        {"hits": []},
    )
    assert rc == 0
    args = client.call_tool.call_args.args[1]
    assert args["k"] == 5
    assert args["detail"] == "compact"
    assert args["language"] == "english"
    assert args["after"] == "2026-01-01"
    assert args["metadata_filter"] == {"email.from": "alice@example.com"}
    assert args["text_contains"] == "force majeure"


def test_search_invalid_metadata_filter_exits_1(capsys):
    rc, client = _run(["search", "foo", "--metadata-filter", "not-json", "--json"], {"hits": []})
    assert rc == 1
    client.call_tool.assert_not_called()
    assert "--metadata-filter must be valid JSON" in capsys.readouterr().err


def test_search_text_mode_prints_human_readable(capsys):
    payload = {
        "hits": [
            {
                "chunk_id": "c1",
                "doc_id": "d1",
                "doc_title": "Contract A",
                "pages": "4",
                "text": "shall terminate",
                "score": 0.87,
                "language": "english",
            }
        ],
        "possible_conflict": False,
    }
    rc, _client = _run(["search", "terminate", "--format", "text"], payload)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Contract A" in out
    assert "shall terminate" in out

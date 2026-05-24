import json
from unittest.mock import patch, MagicMock

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
    rc, client = _run(["search", "termination", "--json"], {"results": []})
    assert rc == 0
    client.call_tool.assert_called_once_with(
        "kb_search",
        {"query": "termination", "k": 10, "offset": 0, "detail": "full"},
    )
    out = capsys.readouterr().out
    assert json.loads(out) == {"results": []}


def test_search_forwards_optional_filters(capsys):
    rc, client = _run(
        [
            "search",
            "force majeure",
            "--k",
            "5",
            "--detail",
            "brief",
            "--language",
            "en",
            "--after",
            "2026-01-01",
            "--json",
        ],
        {"results": []},
    )
    assert rc == 0
    args = client.call_tool.call_args.args[1]
    assert args["k"] == 5
    assert args["detail"] == "brief"
    assert args["language"] == "en"
    assert args["after"] == "2026-01-01"


def test_search_text_mode_prints_human_readable(capsys):
    payload = {
        "results": [
            {
                "chunk_id": "c1",
                "doc_id": "d1",
                "title": "Contract A",
                "page": 4,
                "snippet": "shall terminate",
                "score": 0.87,
                "language": "en",
                "citation": "Contract A, p.4",
            }
        ],
        "possible_conflict": False,
    }
    rc, _client = _run(["search", "terminate", "--format", "text"], payload)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Contract A" in out
    assert "shall terminate" in out

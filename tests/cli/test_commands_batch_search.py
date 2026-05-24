from unittest.mock import MagicMock, patch

from harbor_clerk.cli import main as cli_main


def _run(args, mock_response):
    """Invoke CLI in-process with a mocked McpHttpClient."""
    with (
        patch("harbor_clerk.cli.commands.batch_search.McpHttpClient") as MockClient,
        patch("harbor_clerk.cli.commands.batch_search.resolve_config") as MockResolve,
    ):
        instance = MagicMock()
        instance.call_tool.return_value = mock_response
        instance.__enter__.return_value = instance
        instance.__exit__.return_value = False
        MockClient.return_value = instance
        MockResolve.return_value = MagicMock(url="https://test", api_key="hc_t", insecure=False)
        rc = cli_main.main(args)
        return rc, instance


def test_batch_search_multiple_queries_forwarded():
    rc, client = _run(["batch-search", "q1", "q2", "q3", "--json"], {"results": []})
    assert rc == 0
    client.call_tool.assert_called_once_with(
        "kb_batch_search",
        {"queries": ["q1", "q2", "q3"], "k": 5, "detail": "brief"},
    )


def test_batch_search_defaults():
    rc, client = _run(["batch-search", "only-query", "--json"], {"results": []})
    assert rc == 0
    args = client.call_tool.call_args.args[1]
    assert args["k"] == 5
    assert args["detail"] == "brief"
    assert args["queries"] == ["only-query"]

from unittest.mock import MagicMock, patch

from harbor_clerk.cli import main as cli_main


def _run(args, mock_response):
    """Invoke CLI in-process with a mocked McpHttpClient."""
    with (
        patch("harbor_clerk.cli.commands.corpus_overview.McpHttpClient") as MockClient,
        patch("harbor_clerk.cli.commands.corpus_overview.resolve_config") as MockResolve,
    ):
        instance = MagicMock()
        instance.call_tool.return_value = mock_response
        instance.__enter__.return_value = instance
        instance.__exit__.return_value = False
        MockClient.return_value = instance
        MockResolve.return_value = MagicMock(url="https://test", api_key="hc_t", insecure=False)
        rc = cli_main.main(args)
        return rc, instance


def test_corpus_overview_default_limit_50():
    rc, client = _run(["corpus-overview", "--json"], {"topics": []})
    assert rc == 0
    client.call_tool.assert_called_once_with(
        "kb_corpus_overview",
        {"limit": 50},
    )


def test_corpus_overview_limit_forwarded():
    rc, client = _run(["corpus-overview", "--limit", "10", "--json"], {"topics": []})
    assert rc == 0
    args = client.call_tool.call_args.args[1]
    assert args["limit"] == 10

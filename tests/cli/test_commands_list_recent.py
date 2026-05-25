from unittest.mock import MagicMock, patch

from harbor_clerk.cli import main as cli_main


def _run(args, mock_response):
    """Invoke CLI in-process with a mocked McpHttpClient."""
    with (
        patch("harbor_clerk.cli.commands.list_recent.McpHttpClient") as MockClient,
        patch("harbor_clerk.cli.commands.list_recent.resolve_config") as MockResolve,
    ):
        instance = MagicMock()
        instance.call_tool.return_value = mock_response
        instance.__enter__.return_value = instance
        instance.__exit__.return_value = False
        MockClient.return_value = instance
        MockResolve.return_value = MagicMock(url="https://test", api_key="hc_t", insecure=False)
        rc = cli_main.main(args)
        return rc, instance


def test_list_recent_default_limit_20():
    rc, client = _run(["list-recent", "--json"], {"documents": []})
    assert rc == 0
    client.call_tool.assert_called_once_with(
        "kb_list_recent",
        {"limit": 20},
    )


def test_list_recent_limit_forwarded():
    rc, client = _run(["list-recent", "--limit", "5", "--json"], {"documents": []})
    assert rc == 0
    args = client.call_tool.call_args.args[1]
    assert args["limit"] == 5

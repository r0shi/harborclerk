from unittest.mock import MagicMock, patch

from harbor_clerk.cli import main as cli_main


def _run(args, mock_response):
    """Invoke CLI in-process with a mocked McpHttpClient."""
    with (
        patch("harbor_clerk.cli.commands.expand_context.McpHttpClient") as MockClient,
        patch("harbor_clerk.cli.commands.expand_context.resolve_config") as MockResolve,
    ):
        instance = MagicMock()
        instance.call_tool.return_value = mock_response
        instance.__enter__.return_value = instance
        instance.__exit__.return_value = False
        MockClient.return_value = instance
        MockResolve.return_value = MagicMock(url="https://test", api_key="hc_t", insecure=False)
        rc = cli_main.main(args)
        return rc, instance


def test_expand_context_defaults_n_to_2():
    rc, client = _run(["expand-context", "c1", "--json"], {"chunks": []})
    assert rc == 0
    client.call_tool.assert_called_once_with(
        "kb_expand_context",
        {"chunk_id": "c1", "n": 2},
    )


def test_expand_context_n_flag_forwarded():
    rc, client = _run(["expand-context", "c1", "-n", "5", "--json"], {"chunks": []})
    assert rc == 0
    args = client.call_tool.call_args.args[1]
    assert args["n"] == 5

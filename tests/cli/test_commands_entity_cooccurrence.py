from unittest.mock import MagicMock, patch

from harbor_clerk.cli import main as cli_main


def _run(args, mock_response):
    """Invoke CLI in-process with a mocked McpHttpClient."""
    with (
        patch("harbor_clerk.cli.commands.entity_cooccurrence.McpHttpClient") as MockClient,
        patch("harbor_clerk.cli.commands.entity_cooccurrence.resolve_config") as MockResolve,
    ):
        instance = MagicMock()
        instance.call_tool.return_value = mock_response
        instance.__enter__.return_value = instance
        instance.__exit__.return_value = False
        MockClient.return_value = instance
        MockResolve.return_value = MagicMock(url="https://test", api_key="hc_t", insecure=False)
        rc = cli_main.main(args)
        return rc, instance


def test_entity_cooccurrence_defaults():
    rc, client = _run(["entity-cooccurrence", "Acme Corp", "--json"], {"cooccurrences": []})
    assert rc == 0
    called_args = client.call_tool.call_args.args[1]
    assert called_args["entity"] == "Acme Corp"
    assert called_args["k"] == 20


def test_entity_cooccurrence_k_forwarded():
    rc, client = _run(["entity-cooccurrence", "Alice", "-k", "5", "--json"], {"cooccurrences": []})
    assert rc == 0
    called_args = client.call_tool.call_args.args[1]
    assert called_args["k"] == 5

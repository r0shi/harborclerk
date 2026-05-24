from unittest.mock import MagicMock, patch

from harbor_clerk.cli import main as cli_main


def _run(args, mock_response):
    """Invoke CLI in-process with a mocked McpHttpClient."""
    with (
        patch("harbor_clerk.cli.commands.entity_search.McpHttpClient") as MockClient,
        patch("harbor_clerk.cli.commands.entity_search.resolve_config") as MockResolve,
    ):
        instance = MagicMock()
        instance.call_tool.return_value = mock_response
        instance.__enter__.return_value = instance
        instance.__exit__.return_value = False
        MockClient.return_value = instance
        MockResolve.return_value = MagicMock(url="https://test", api_key="hc_t", insecure=False)
        rc = cli_main.main(args)
        return rc, instance


def test_entity_search_defaults():
    rc, client = _run(["entity-search", "Acme Corp", "--json"], {"entities": []})
    assert rc == 0
    called_args = client.call_tool.call_args.args[1]
    assert called_args["query"] == "Acme Corp"
    assert called_args["k"] == 20
    assert "type" not in called_args


def test_entity_search_type_forwarded():
    rc, client = _run(["entity-search", "Alice", "--type", "PERSON", "--json"], {"entities": []})
    assert rc == 0
    called_args = client.call_tool.call_args.args[1]
    assert called_args["type"] == "PERSON"

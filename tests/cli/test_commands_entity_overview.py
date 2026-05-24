from unittest.mock import MagicMock, patch

from harbor_clerk.cli import main as cli_main


def _run(args, mock_response):
    """Invoke CLI in-process with a mocked McpHttpClient."""
    with (
        patch("harbor_clerk.cli.commands.entity_overview.McpHttpClient") as MockClient,
        patch("harbor_clerk.cli.commands.entity_overview.resolve_config") as MockResolve,
    ):
        instance = MagicMock()
        instance.call_tool.return_value = mock_response
        instance.__enter__.return_value = instance
        instance.__exit__.return_value = False
        MockClient.return_value = instance
        MockResolve.return_value = MagicMock(url="https://test", api_key="hc_t", insecure=False)
        rc = cli_main.main(args)
        return rc, instance


def test_entity_overview_no_args():
    rc, client = _run(["entity-overview", "--json"], {"entities": []})
    assert rc == 0
    called_args = client.call_tool.call_args.args[1]
    assert called_args == {}


def test_entity_overview_doc_id_forwarded():
    rc, client = _run(["entity-overview", "--doc-id", "abc", "--json"], {"entities": []})
    assert rc == 0
    called_args = client.call_tool.call_args.args[1]
    assert called_args == {"doc_id": "abc"}

from unittest.mock import MagicMock, patch

from harbor_clerk.cli import main as cli_main


def _run(args, mock_response):
    """Invoke CLI in-process with a mocked McpHttpClient."""
    with (
        patch("harbor_clerk.cli.commands.ingest_status.McpHttpClient") as MockClient,
        patch("harbor_clerk.cli.commands.ingest_status.resolve_config") as MockResolve,
    ):
        instance = MagicMock()
        instance.call_tool.return_value = mock_response
        instance.__enter__.return_value = instance
        instance.__exit__.return_value = False
        MockClient.return_value = instance
        MockResolve.return_value = MagicMock(url="https://test", api_key="hc_t", insecure=False)
        rc = cli_main.main(args)
        return rc, instance


def test_ingest_status_calls_kb_ingest_status_with_doc_id():
    rc, client = _run(["ingest-status", "d1", "--json"], {"stages": []})
    assert rc == 0
    client.call_tool.assert_called_once_with(
        "kb_ingest_status",
        {"doc_id": "d1"},
    )

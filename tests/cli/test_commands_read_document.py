from unittest.mock import MagicMock, patch

from harbor_clerk.cli import main as cli_main


def _run(args, mock_response):
    """Invoke CLI in-process with a mocked McpHttpClient."""
    with (
        patch("harbor_clerk.cli.commands.read_document.McpHttpClient") as MockClient,
        patch("harbor_clerk.cli.commands.read_document.resolve_config") as MockResolve,
    ):
        instance = MagicMock()
        instance.call_tool.return_value = mock_response
        instance.__enter__.return_value = instance
        instance.__exit__.return_value = False
        MockClient.return_value = instance
        MockResolve.return_value = MagicMock(url="https://test", api_key="hc_t", insecure=False)
        rc = cli_main.main(args)
        return rc, instance


def test_read_document_defaults():
    rc, client = _run(["read-document", "d1", "--json"], {"chunks": []})
    assert rc == 0
    client.call_tool.assert_called_once_with(
        "kb_read_document",
        {"doc_id": "d1"},
    )


def test_read_document_page_range_forwarded():
    rc, client = _run(
        ["read-document", "d1", "--page-start", "3", "--page-end", "5", "--max-chars", "10000", "--json"],
        {"chunks": []},
    )
    assert rc == 0
    call_args = client.call_tool.call_args.args[1]
    assert call_args["page_start"] == 3
    assert call_args["page_end"] == 5
    assert call_args["max_chars"] == 10000
    assert "page" not in call_args
    assert "page_size" not in call_args

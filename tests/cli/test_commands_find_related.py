from unittest.mock import MagicMock, patch

from harbor_clerk.cli import main as cli_main


def _run(args, mock_response):
    """Invoke CLI in-process with a mocked McpHttpClient."""
    with (
        patch("harbor_clerk.cli.commands.find_related.McpHttpClient") as MockClient,
        patch("harbor_clerk.cli.commands.find_related.resolve_config") as MockResolve,
    ):
        instance = MagicMock()
        instance.call_tool.return_value = mock_response
        instance.__enter__.return_value = instance
        instance.__exit__.return_value = False
        MockClient.return_value = instance
        MockResolve.return_value = MagicMock(url="https://test", api_key="hc_t", insecure=False)
        rc = cli_main.main(args)
        return rc, instance


def test_find_related_defaults():
    rc, client = _run(["find-related", "doc-uuid-1", "--json"], {"related": []})
    assert rc == 0
    client.call_tool.assert_called_once_with(
        "kb_find_related",
        {"doc_id": "doc-uuid-1", "k": 5},
    )


def test_find_related_k_forwarded():
    rc, client = _run(["find-related", "doc-uuid-2", "-k", "10", "--json"], {"related": []})
    assert rc == 0
    args = client.call_tool.call_args.args[1]
    assert args["k"] == 10

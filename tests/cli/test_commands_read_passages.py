from unittest.mock import MagicMock, patch

from harbor_clerk.cli import main as cli_main


def _run(args, mock_response):
    """Invoke CLI in-process with a mocked McpHttpClient."""
    with (
        patch("harbor_clerk.cli.commands.read_passages.McpHttpClient") as MockClient,
        patch("harbor_clerk.cli.commands.read_passages.resolve_config") as MockResolve,
    ):
        instance = MagicMock()
        instance.call_tool.return_value = mock_response
        instance.__enter__.return_value = instance
        instance.__exit__.return_value = False
        MockClient.return_value = instance
        MockResolve.return_value = MagicMock(url="https://test", api_key="hc_t", insecure=False)
        rc = cli_main.main(args)
        return rc, instance


def test_read_passages_calls_kb_read_passages_with_chunk_ids(capsys):
    rc, client = _run(["read-passages", "c1", "c2", "c3", "--json"], {"passages": []})
    assert rc == 0
    client.call_tool.assert_called_once_with(
        "kb_read_passages",
        {"chunk_ids": ["c1", "c2", "c3"]},
    )


def test_read_passages_include_context_forwarded():
    rc, client = _run(["read-passages", "c1", "--include-context", "--json"], {"passages": []})
    assert rc == 0
    args = client.call_tool.call_args.args[1]
    assert args["include_context"] is True


def test_read_passages_no_chunk_ids_exits_1(capsys):
    import pytest

    with pytest.raises(SystemExit) as exc_info:
        cli_main.main(["read-passages"])
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "required" in err

from unittest.mock import MagicMock, patch

from harbor_clerk.cli import main as cli_main


def _run(args, mock_response):
    """Invoke CLI in-process with a mocked McpHttpClient."""
    with (
        patch("harbor_clerk.cli.commands.batch_search.McpHttpClient") as MockClient,
        patch("harbor_clerk.cli.commands.batch_search.resolve_config") as MockResolve,
    ):
        instance = MagicMock()
        instance.call_tool.return_value = mock_response
        instance.__enter__.return_value = instance
        instance.__exit__.return_value = False
        MockClient.return_value = instance
        MockResolve.return_value = MagicMock(url="https://test", api_key="hc_t", insecure=False)
        rc = cli_main.main(args)
        return rc, instance


def test_batch_search_multiple_queries_forwarded():
    rc, client = _run(["batch-search", "q1", "q2", "q3", "--json"], {"results": []})
    assert rc == 0
    client.call_tool.assert_called_once_with(
        "kb_batch_search",
        {"queries": ["q1", "q2", "q3"], "k": 5, "detail": "brief"},
    )


def test_batch_search_defaults():
    rc, client = _run(["batch-search", "only-query", "--json"], {"results": []})
    assert rc == 0
    args = client.call_tool.call_args.args[1]
    assert args["k"] == 5
    assert args["detail"] == "brief"
    assert args["queries"] == ["only-query"]


def test_batch_search_forwards_optional_filters():
    rc, client = _run(
        [
            "batch-search",
            "q1",
            "q2",
            "--k",
            "3",
            "--detail",
            "compact",
            "--brief-chars",
            "120",
            "--doc-ids",
            "d1,d2",
            "--language",
            "fr",
            "--mime-type",
            "application/pdf",
            "--metadata-filter",
            '{"sidecar.vendor": "Pinnacle"}',
            "--json",
        ],
        {"results": []},
    )
    assert rc == 0
    args = client.call_tool.call_args.args[1]
    assert args == {
        "queries": ["q1", "q2"],
        "k": 3,
        "detail": "compact",
        "brief_chars": 120,
        "doc_ids": ["d1", "d2"],
        "language": "french",
        "mime_type": "application/pdf",
        "metadata_filter": {"sidecar.vendor": "Pinnacle"},
    }


def test_batch_search_invalid_metadata_filter_exits_1(capsys):
    rc, client = _run(["batch-search", "q1", "--metadata-filter", "not-json", "--json"], {"results": []})
    assert rc == 1
    client.call_tool.assert_not_called()
    assert "--metadata-filter must be valid JSON" in capsys.readouterr().err

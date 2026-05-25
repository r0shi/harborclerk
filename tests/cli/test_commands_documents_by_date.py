"""CLI tests for harbor-clerk documents-by-date."""

from unittest.mock import MagicMock, patch

from harbor_clerk.cli import main as cli_main


def _run(args, mock_response):
    with (
        patch("harbor_clerk.cli.commands.documents_by_date.McpHttpClient") as MockClient,
        patch("harbor_clerk.cli.commands.documents_by_date.resolve_config") as MockResolve,
    ):
        instance = MagicMock()
        instance.call_tool.return_value = mock_response
        instance.__enter__.return_value = instance
        instance.__exit__.return_value = False
        MockClient.return_value = instance
        MockResolve.return_value = MagicMock(url="https://test", api_key="hc_t", insecure=False)
        rc = cli_main.main(args)
        return rc, instance


_EMPTY = {"direction": "earliest", "count": 0, "results": []}


def test_default_direction_is_earliest(capsys):
    rc, client = _run(["documents-by-date", "--json"], _EMPTY)
    assert rc == 0
    args = client.call_tool.call_args.args[1]
    assert args["direction"] == "earliest"


def test_direction_latest_flag(capsys):
    rc, client = _run(["documents-by-date", "--direction", "latest", "--json"], _EMPTY)
    assert rc == 0
    args = client.call_tool.call_args.args[1]
    assert args["direction"] == "latest"


def test_query_flag_forwards(capsys):
    rc, client = _run(["documents-by-date", "--query", "California", "--json"], _EMPTY)
    assert rc == 0
    args = client.call_tool.call_args.args[1]
    assert args["query"] == "California"


def test_after_and_before_flags_forward(capsys):
    rc, client = _run(
        [
            "documents-by-date",
            "--after",
            "2024-01-01",
            "--before",
            "2024-12-31",
            "--json",
        ],
        _EMPTY,
    )
    assert rc == 0
    args = client.call_tool.call_args.args[1]
    assert args["after"] == "2024-01-01"
    assert args["before"] == "2024-12-31"


def test_date_field_flag_forwards(capsys):
    rc, client = _run(["documents-by-date", "--date-field", "sidecar.date", "--json"], _EMPTY)
    assert rc == 0
    args = client.call_tool.call_args.args[1]
    assert args["date_field"] == "sidecar.date"


def test_metadata_filter_parses_json(capsys):
    rc, client = _run(
        [
            "documents-by-date",
            "--metadata-filter",
            '{"sidecar.vendor": "Pinnacle Tech Solutions"}',
            "--json",
        ],
        _EMPTY,
    )
    assert rc == 0
    args = client.call_tool.call_args.args[1]
    assert args["metadata_filter"] == {"sidecar.vendor": "Pinnacle Tech Solutions"}


def test_metadata_filter_invalid_json_returns_usage_error(capsys):
    rc, _client = _run(["documents-by-date", "--metadata-filter", "not-json", "--json"], _EMPTY)
    assert rc == 1


def test_limit_flag_forwards(capsys):
    rc, client = _run(["documents-by-date", "--limit", "25", "--json"], _EMPTY)
    assert rc == 0
    args = client.call_tool.call_args.args[1]
    assert args["limit"] == 25


def test_optional_flags_absent_when_not_provided(capsys):
    rc, client = _run(["documents-by-date", "--json"], _EMPTY)
    assert rc == 0
    args = client.call_tool.call_args.args[1]
    # Only direction + limit should be present in the minimal call.
    assert "query" not in args
    assert "after" not in args
    assert "before" not in args
    assert "date_field" not in args
    assert "metadata_filter" not in args


def test_text_mode_renders_results(capsys):
    payload = {
        "direction": "earliest",
        "count": 2,
        "results": [
            {
                "doc_id": "id-1",
                "title": "Earliest doc",
                "canonical_filename": "a.eml",
                "date": "1999-10-13T08:34:00+00:00",
                "date_source": "tika.created_at",
            },
            {
                "doc_id": "id-2",
                "title": "Second doc",
                "canonical_filename": "b.eml",
                "date": "1999-10-14T00:00:00+00:00",
                "date_source": "tika.created_at",
            },
        ],
    }
    rc, _client = _run(["documents-by-date", "--format", "text"], payload)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Earliest doc" in out
    assert "1999-10-13" in out
    assert "tika.created_at" in out

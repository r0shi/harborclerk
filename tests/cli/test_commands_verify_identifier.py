"""CLI tests for harbor-clerk verify-identifier."""

import json
from unittest.mock import MagicMock, patch

import pytest

from harbor_clerk.cli import main as cli_main


def _run(args, mock_response):
    """Invoke CLI in-process with a mocked McpHttpClient."""
    with (
        patch("harbor_clerk.cli.commands.verify_identifier.McpHttpClient") as MockClient,
        patch("harbor_clerk.cli.commands.verify_identifier.resolve_config") as MockResolve,
    ):
        instance = MagicMock()
        instance.call_tool.return_value = mock_response
        instance.__enter__.return_value = instance
        instance.__exit__.return_value = False
        MockClient.return_value = instance
        MockResolve.return_value = MagicMock(url="https://test", api_key="hc_t", insecure=False)
        rc = cli_main.main(args)
        return rc, instance


def test_verify_identifier_requires_positional(capsys):
    """Calling without an identifier should fail argparse."""
    with pytest.raises(SystemExit) as exc_info:
        _run(["verify-identifier"], {"status": "not_found", "identifier": ""})
    assert exc_info.value.code == 1


def test_verify_identifier_calls_tool_with_identifier(capsys):
    rc, client = _run(
        ["verify-identifier", "Pinnacle Vendor Contract", "--json"],
        {"status": "not_found", "identifier": "Pinnacle Vendor Contract"},
    )
    assert rc == 0
    client.call_tool.assert_called_once_with("kb_verify_identifier", {"identifier": "Pinnacle Vendor Contract"})


def test_verify_identifier_json_mode_passes_payload_through(capsys):
    payload = {
        "status": "unique",
        "match": {
            "doc_id": "abc-123",
            "title": "Pinnacle",
            "canonical_filename": "pin.pdf",
            "discriminating_fields": {},
        },
    }
    rc, _client = _run(["verify-identifier", "Pinnacle", "--json"], payload)
    assert rc == 0
    out = capsys.readouterr().out
    assert json.loads(out) == payload


def test_verify_identifier_text_mode_renders_unique(capsys):
    payload = {
        "status": "unique",
        "match": {
            "doc_id": "abc-123",
            "title": "Pinnacle Vendor Contract",
            "canonical_filename": "pin.pdf",
            "discriminating_fields": {},
        },
    }
    rc, _client = _run(["verify-identifier", "Pinnacle", "--format", "text"], payload)
    assert rc == 0
    out = capsys.readouterr().out
    assert "unique" in out.lower()
    assert "Pinnacle Vendor Contract" in out
    assert "abc-123" in out


def test_verify_identifier_text_mode_renders_ambiguous(capsys):
    payload = {
        "status": "ambiguous",
        "count": 2,
        "candidates": [
            {
                "doc_id": "id-1",
                "title": "Pinnacle A",
                "canonical_filename": "a.pdf",
                "discriminating_fields": {"sidecar.vendor": "Tech Solutions"},
            },
            {
                "doc_id": "id-2",
                "title": "Pinnacle B",
                "canonical_filename": "b.pdf",
                "discriminating_fields": {"sidecar.vendor": "Industries"},
            },
        ],
        "suggestion": "2 candidates differ on sidecar.vendor.",
    }
    rc, _client = _run(["verify-identifier", "Pinnacle", "--format", "text"], payload)
    assert rc == 0
    out = capsys.readouterr().out
    assert "ambiguous" in out.lower()
    assert "Pinnacle A" in out
    assert "Pinnacle B" in out
    assert "Tech Solutions" in out
    assert "Industries" in out


def test_verify_identifier_text_mode_renders_not_found(capsys):
    payload = {"status": "not_found", "identifier": "nope"}
    rc, _client = _run(["verify-identifier", "nope", "--format", "text"], payload)
    assert rc == 0
    out = capsys.readouterr().out
    assert "not_found" in out.lower() or "not found" in out.lower()

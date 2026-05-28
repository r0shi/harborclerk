import io
import json

from harbor_clerk.cli.output import (
    OutputMode,
    render,
    resolve_mode,
)


def test_resolve_mode_tty_defaults_to_text():
    assert resolve_mode(force_json=False, fmt=None, isatty=True) == OutputMode.TEXT


def test_resolve_mode_non_tty_defaults_to_json():
    assert resolve_mode(force_json=False, fmt=None, isatty=False) == OutputMode.JSON


def test_resolve_mode_json_flag_wins_over_tty():
    assert resolve_mode(force_json=True, fmt=None, isatty=True) == OutputMode.JSON


def test_resolve_mode_format_text_wins_over_pipe():
    assert resolve_mode(force_json=False, fmt="text", isatty=False) == OutputMode.TEXT


def test_render_json_emits_indented_json():
    buf = io.StringIO()
    render({"a": 1}, mode=OutputMode.JSON, command="search", stream=buf)
    assert json.loads(buf.getvalue()) == {"a": 1}


def test_render_text_search_results_uses_pretty_printer():
    buf = io.StringIO()
    payload = {
        "hits": [
            {
                "chunk_id": "c1",
                "doc_id": "d1",
                "doc_title": "Doc A",
                "pages": "1",
                "text": "hello world",
                "score": 0.9,
                "language": "english",
            },
        ],
        "possible_conflict": False,
    }
    render(payload, mode=OutputMode.TEXT, command="search", stream=buf)
    out = buf.getvalue()
    assert "Doc A" in out
    assert "hello world" in out
    assert "0.9" in out or "0.90" in out


def test_render_text_search_error_prints_error():
    buf = io.StringIO()
    render({"error": "Cannot specify both doc_id and doc_ids"}, mode=OutputMode.TEXT, command="search", stream=buf)
    out = buf.getvalue()
    assert "error: Cannot specify both doc_id and doc_ids" in out
    assert "0 results" not in out


def test_render_text_falls_back_to_json_for_unknown_command():
    buf = io.StringIO()
    render({"foo": "bar"}, mode=OutputMode.TEXT, command="unknown-command", stream=buf)
    # No pretty-printer for unknown command → indented JSON to keep output usable.
    assert "foo" in buf.getvalue()
    assert "bar" in buf.getvalue()

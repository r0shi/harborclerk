import io
import json

from harbor_clerk.cli.client import McpClientError
from harbor_clerk.cli.errors import (
    EXIT_AUTH,
    EXIT_CLI_DISABLED,
    EXIT_CONNECTION,
    EXIT_HTTP,
    EXIT_PROTOCOL,
    map_client_error_to_exit,
    write_error,
)


def test_connection_error_maps_to_2():
    err = McpClientError(kind="connection", message="ECONNREFUSED")
    assert map_client_error_to_exit(err) == EXIT_CONNECTION == 2


def test_cli_disabled_maps_to_3():
    err = McpClientError(kind="cli_disabled", message="...")
    assert map_client_error_to_exit(err) == EXIT_CLI_DISABLED == 3


def test_auth_maps_to_4():
    err = McpClientError(kind="auth", message="bad key")
    assert map_client_error_to_exit(err) == EXIT_AUTH == 4


def test_http_maps_to_5():
    err = McpClientError(kind="http", message="500")
    assert map_client_error_to_exit(err) == EXIT_HTTP == 5


def test_protocol_maps_to_5():
    err = McpClientError(kind="protocol", message="bad json-rpc")
    assert map_client_error_to_exit(err) == EXIT_PROTOCOL == 5


def test_write_error_text_mode_goes_to_stderr():
    buf = io.StringIO()
    err = McpClientError(kind="connection", message="could not connect to https://localhost")
    write_error(err, json_mode=False, stream=buf)
    out = buf.getvalue()
    assert "could not connect" in out


def test_write_error_json_mode_is_structured():
    buf = io.StringIO()
    err = McpClientError(kind="auth", message="bad key", status_code=401, body={"error": "Unauthorized"})
    write_error(err, json_mode=True, stream=buf)
    parsed = json.loads(buf.getvalue())
    assert parsed["error_kind"] == "auth"
    assert parsed["message"] == "bad key"
    assert parsed["status_code"] == 401

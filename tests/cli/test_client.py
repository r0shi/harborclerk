import json

import pytest
import respx
from httpx import Response

from harbor_clerk.cli import __version__
from harbor_clerk.cli.client import McpClientError, McpHttpClient
from harbor_clerk.cli.config import CliConfig


@pytest.fixture
def cfg():
    return CliConfig(url="https://test.local", api_key="hc_test", insecure=False)


@respx.mock
def test_call_tool_returns_parsed_json(cfg):
    respx.post("https://test.local/mcp").mock(
        return_value=Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"content": [{"type": "text", "text": json.dumps({"results": [{"score": 0.9}]})}]},
            },
        )
    )
    client = McpHttpClient(cfg)
    payload = client.call_tool("kb_search", {"query": "test"})
    assert payload == {"results": [{"score": 0.9}]}


@respx.mock
def test_call_tool_sends_user_agent_and_auth(cfg):
    route = respx.post("https://test.local/mcp").mock(
        return_value=Response(
            200,
            json={"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "{}"}]}},
        )
    )
    client = McpHttpClient(cfg)
    client.call_tool("kb_search", {"query": "x"})
    sent = route.calls.last.request
    assert sent.headers["authorization"] == "Bearer hc_test"
    assert sent.headers["user-agent"] == f"harbor-clerk-cli/{__version__}"


@respx.mock
def test_connection_error_raises_mcp_client_error(cfg):
    respx.post("https://test.local/mcp").mock(side_effect=ConnectionError("boom"))
    client = McpHttpClient(cfg)
    with pytest.raises(McpClientError) as exc:
        client.call_tool("kb_search", {"query": "x"})
    assert exc.value.kind == "connection"


@respx.mock
def test_403_cli_disabled_raises_typed_error(cfg):
    respx.post("https://test.local/mcp").mock(
        return_value=Response(
            403,
            json={"error": "cli_access_disabled", "hint": "Enable in System Settings → Integrations"},
        )
    )
    client = McpHttpClient(cfg)
    with pytest.raises(McpClientError) as exc:
        client.call_tool("kb_search", {"query": "x"})
    assert exc.value.kind == "cli_disabled"
    assert "→" in exc.value.message


@respx.mock
def test_401_raises_auth_error(cfg):
    respx.post("https://test.local/mcp").mock(
        return_value=Response(401, json={"error": "Unauthorized"}),
    )
    client = McpHttpClient(cfg)
    with pytest.raises(McpClientError) as exc:
        client.call_tool("kb_search", {"query": "x"})
    assert exc.value.kind == "auth"

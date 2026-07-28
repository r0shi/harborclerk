"""find_all_documents chat tool — wiring + arg mapping."""


def test_find_all_documents_in_base_chat_tools():
    from harbor_clerk.llm.tools import _BASE_CHAT_TOOLS

    names = {t["function"]["name"] for t in _BASE_CHAT_TOOLS}
    assert "find_all_documents" in names


def test_find_all_documents_schema_has_text_contains():
    from harbor_clerk.llm.tools import _BASE_CHAT_TOOLS

    fa = next(t for t in _BASE_CHAT_TOOLS if t["function"]["name"] == "find_all_documents")
    props = fa["function"]["parameters"]["properties"]
    assert "query" in props
    assert "text_contains" in props
    assert "max_results" in props
    assert "sort_by" in props


def test_map_args_find_all_passes_through_known_keys():
    from harbor_clerk.llm.tools import _map_args_find_all

    out = _map_args_find_all(
        {
            "query": "X",
            "text_contains": "Y",
            "max_results": 50,
            "sort_by": "date_desc",
        }
    )
    assert out["query"] == "X"
    assert out["text_contains"] == "Y"
    assert out["max_results"] == 50
    assert out["sort_by"] == "date_desc"


def test_map_args_find_all_drops_unknown_keys():
    """Unknown keys (e.g. `k` left over from search_documents) are filtered."""
    from harbor_clerk.llm.tools import _map_args_find_all

    out = _map_args_find_all({"query": "X", "k": 99})
    assert "k" not in out


def test_dispatch_routes_find_all_documents_to_kb_find_all():
    """Catch typos in the dispatch table — chat tool name must route to the
    correct MCP function name. Without this, a future rename to e.g.
    kb_find_all_docs would silently return 'Unknown tool' at runtime."""
    from harbor_clerk.llm.tools import _TOOL_DISPATCH

    mcp_name, _ = _TOOL_DISPATCH["find_all_documents"]
    assert mcp_name == "kb_find_all"


def test_get_chat_tools_uses_model_find_all_default(monkeypatch):
    """When called with a model whose find_all_default_max_results is set,
    the find_all_documents schema's max_results.default matches."""
    from harbor_clerk.llm.models import ModelInfo
    from harbor_clerk.llm.tools import get_chat_tools

    custom_model = ModelInfo(
        id="custom-test",
        name="Custom",
        huggingface_repo="x",
        filename="x.gguf",
        size_bytes=1,
        context_window=4096,
        supports_tools=True,
        find_all_default_max_results=30,
    )

    tools = get_chat_tools(model=custom_model)
    fa = next(t for t in tools if t["function"]["name"] == "find_all_documents")
    assert fa["function"]["parameters"]["properties"]["max_results"]["default"] == 30


def test_get_chat_tools_falls_back_to_100_when_model_default_none():
    """find_all_default_max_results=None => schema uses tool default of 100."""
    from harbor_clerk.llm.models import ModelInfo
    from harbor_clerk.llm.tools import get_chat_tools

    null_model = ModelInfo(
        id="null-test",
        name="Null",
        huggingface_repo="x",
        filename="x.gguf",
        size_bytes=1,
        context_window=4096,
        supports_tools=True,
        find_all_default_max_results=None,
    )
    tools = get_chat_tools(model=null_model)
    fa = next(t for t in tools if t["function"]["name"] == "find_all_documents")
    assert fa["function"]["parameters"]["properties"]["max_results"]["default"] == 100


def test_verify_identifier_in_base_chat_tools():
    """verify_identifier resolves a named document to one doc_id before
    quoting it — closes the local-vs-cloud groundedness gap by giving local
    models the same fabrication-prevention surface MCP-direct callers use."""
    from harbor_clerk.llm.tools import _BASE_CHAT_TOOLS

    names = {t["function"]["name"] for t in _BASE_CHAT_TOOLS}
    assert "verify_identifier" in names


def test_verify_identifier_schema_requires_identifier():
    from harbor_clerk.llm.tools import _BASE_CHAT_TOOLS

    vi = next(t for t in _BASE_CHAT_TOOLS if t["function"]["name"] == "verify_identifier")
    params = vi["function"]["parameters"]
    assert params["required"] == ["identifier"]
    assert "identifier" in params["properties"]
    assert params["properties"]["identifier"]["type"] == "string"


def test_map_args_verify_identifier_passes_identifier():
    from harbor_clerk.llm.tools import _map_args_verify_identifier

    assert _map_args_verify_identifier({"identifier": "2ThemartComInc_19990826"}) == {
        "identifier": "2ThemartComInc_19990826"
    }


def test_dispatch_routes_verify_identifier_to_kb_verify_identifier():
    """Catch typos in the dispatch table — without this, a chat-mode call to
    verify_identifier would silently return 'Unknown tool' at runtime."""
    from harbor_clerk.llm.tools import _TOOL_DISPATCH

    mcp_name, _ = _TOOL_DISPATCH["verify_identifier"]
    assert mcp_name == "kb_verify_identifier"


def test_research_dispatch_inherits_verify_identifier():
    """Research mode dispatch inherits from chat dispatch via spread; the new
    tool must be reachable in both modes."""
    from harbor_clerk.llm.tools import _RESEARCH_TOOL_DISPATCH

    assert "verify_identifier" in _RESEARCH_TOOL_DISPATCH
    assert _RESEARCH_TOOL_DISPATCH["verify_identifier"][0] == "kb_verify_identifier"


# ---------------------------------------------------------------------------
# batch_search — gated by model size
# ---------------------------------------------------------------------------


def _mk_model(*, size_bytes: int, mid: str = "test-model"):
    """Build a ModelInfo with a chosen size_bytes for gate tests."""
    from harbor_clerk.llm.models import ModelInfo

    return ModelInfo(
        id=mid,
        name=mid,
        huggingface_repo="x",
        filename="x.gguf",
        size_bytes=size_bytes,
        context_window=32768,
        supports_tools=True,
    )


def test_batch_search_excluded_for_small_model():
    """Small models (< 5 GB GGUF) get the chat surface without batch_search.
    The v3 sweep showed multi-query result aggregation overwhelmed them."""
    from harbor_clerk.llm.tools import get_chat_tools

    small = _mk_model(size_bytes=2_500_000_000, mid="small")
    tools = get_chat_tools(model=small)
    names = {t["function"]["name"] for t in tools}
    assert "batch_search" not in names
    # And the existing tools are still there
    assert "search_documents" in names


def test_batch_search_included_for_mid_tier():
    """Models at the threshold (5 GB) or above get batch_search added to
    their chat surface for compare-and-contrast questions."""
    from harbor_clerk.llm.tools import get_chat_tools

    mid = _mk_model(size_bytes=5_000_000_000, mid="mid")
    tools = get_chat_tools(model=mid)
    names = {t["function"]["name"] for t in tools}
    assert "batch_search" in names


def test_batch_search_included_for_heavy():
    from harbor_clerk.llm.tools import get_chat_tools

    heavy = _mk_model(size_bytes=20_000_000_000, mid="heavy")
    tools = get_chat_tools(model=heavy)
    names = {t["function"]["name"] for t in tools}
    assert "batch_search" in names


def test_batch_search_omitted_when_model_is_none():
    """No active model means no per-model surface decisions — fall back to
    the base set without batch_search."""
    from harbor_clerk.llm.tools import get_chat_tools

    tools = get_chat_tools(model=None)
    names = {t["function"]["name"] for t in tools}
    assert "batch_search" not in names


def test_batch_search_schema_requires_queries():
    from harbor_clerk.llm.tools import _BATCH_SEARCH_TOOL

    params = _BATCH_SEARCH_TOOL["function"]["parameters"]
    assert params["required"] == ["queries"]
    assert params["properties"]["queries"]["type"] == "array"
    assert params["properties"]["queries"]["items"]["type"] == "string"


def test_map_args_batch_search_chat_caps_queries_at_8():
    """A misbehaving model issuing 50 queries shouldn't flood the search
    backend. The chat mapper truncates at 8."""
    from harbor_clerk.llm.tools import _map_args_batch_search_chat

    out = _map_args_batch_search_chat({"queries": [f"q{i}" for i in range(50)]})
    assert len(out["queries"]) == 8
    assert out["queries"][0] == "q0"
    assert out["queries"][7] == "q7"


def test_map_args_batch_search_chat_clamps_k():
    """k must be clamped to the chat-search settings cap, same as
    single search_documents."""
    from harbor_clerk.llm.tools import _map_args_batch_search_chat

    out = _map_args_batch_search_chat({"queries": ["a", "b"], "k": 999})
    # Cap is 50 in paginated mode, settings.chat_search_k otherwise.
    # Either way, 999 must come back smaller.
    assert out["k"] < 999


def test_dispatch_routes_batch_search_to_kb_batch_search():
    from harbor_clerk.llm.tools import _TOOL_DISPATCH

    mcp_name, _ = _TOOL_DISPATCH["batch_search"]
    assert mcp_name == "kb_batch_search"


def test_research_dispatch_overrides_batch_search_with_research_mapper():
    """Research mode has its own batch_search mapper (brief detail, larger
    k cap). Verify the explicit override wins over the chat dispatch
    inherited via spread."""
    from harbor_clerk.llm.tools import _RESEARCH_TOOL_DISPATCH, _map_args_batch_search_chat

    assert "batch_search" in _RESEARCH_TOOL_DISPATCH
    _, mapper = _RESEARCH_TOOL_DISPATCH["batch_search"]
    assert mapper is not _map_args_batch_search_chat


async def test_permission_error_reaches_the_llm_without_a_class_name():
    """`execute_tool`'s error string goes straight into the LLM's tool-result
    stream and is rendered to the user as `Error: <string>`.

    Both PermissionErrors raised here carry authored text ("Not authenticated",
    "Admin access required"), so the class name is noise. Reverting the branch
    to `describe_error` kept the whole suite green before this.
    """
    from harbor_clerk.llm.tools import execute_tool

    # No principal in the context var → mcp_server raises PermissionError.
    result = await execute_tool("search_documents", {"query": "anything"})

    assert "PermissionError" not in result, f"class name leaked to the LLM: {result}"
    assert "authenticat" in result.lower() or "admin" in result.lower(), result

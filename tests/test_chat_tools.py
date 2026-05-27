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

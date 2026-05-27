"""find_all_documents chat tool — wiring + arg mapping."""

import pytest


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

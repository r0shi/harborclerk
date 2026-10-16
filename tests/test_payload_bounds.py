"""Summaries and metadata in a tool payload: the key's snippet cap, then a size bound (#831)."""

import json

import pytest

from harbor_clerk.llm.chat import _tool_result_max_chars
from harbor_clerk.llm.models import TIGHT_FIT_CONTEXT
from harbor_clerk.payload_bounds import (
    CHAT_RESULT_BUDGET_FLOOR_CHARS,
    ELLIPSIS,
    HINT_MAX_CHARS,
    HINT_VALUE_MAX_CHARS,
    METADATA_MAX_CHARS,
    METADATA_VALUE_MAX_CHARS,
    SUMMARY_MAX_CHARS,
    TRUNCATED_KEY,
    bound_metadata,
    bound_summary,
)


def _json_len(value) -> int:
    """As a tool sends it without indentation: json.dumps' default escaping and separators."""
    return len(json.dumps(value))


def _escaped(text: str) -> int:
    return len(json.dumps(text)) - 2


def test_the_budget_floor_is_the_chat_formula_at_the_tight_fit_context():
    """The end-to-end check that kb_get_document fits it is in tests/test_key_snippet_cap.py."""
    assert _tool_result_max_chars(TIGHT_FIT_CONTEXT) == CHAT_RESULT_BUDGET_FLOOR_CHARS
    assert HINT_VALUE_MAX_CHARS < HINT_MAX_CHARS < METADATA_MAX_CHARS
    assert METADATA_VALUE_MAX_CHARS < METADATA_MAX_CHARS


# --- summaries --------------------------------------------------------------


@pytest.mark.parametrize(
    ("cap", "bounded", "expected"),
    [
        (None, False, "abcdefghij"),
        (None, True, "abcdefghij"),
        (4, False, "abcd"),
        (4, True, "abcd"),
        (0, True, None),
        (-5, False, None),
    ],
)
def test_bound_summary_takes_the_passage_rule(cap, bounded, expected):
    assert bound_summary("abcdefghij", cap, bounded=bounded) == expected
    assert bound_summary(None, cap, bounded=bounded) is None


def test_bound_summary_cuts_a_long_summary_with_an_ellipsis_only_when_bounded():
    long = "s" * (SUMMARY_MAX_CHARS * 3)
    cut = bound_summary(long, None, bounded=True)
    assert _escaped(cut) == SUMMARY_MAX_CHARS and cut.endswith(ELLIPSIS) and long.startswith(cut[:-1])
    assert bound_summary(long, None, bounded=False) == long
    # A capped key's prefix carries no ellipsis, as a capped passage carries none.
    assert bound_summary(long, 50, bounded=True) == "s" * 50
    assert bound_summary("s" * SUMMARY_MAX_CHARS, None, bounded=True) == "s" * SUMMARY_MAX_CHARS


# --- metadata ---------------------------------------------------------------


def test_small_metadata_is_unchanged_and_unmarked():
    meta = {"tika": {"title": "Q3 minutes", "pages": 4}, "sidecar": {"tags": ["a", "b"], "ok": True}}
    assert bound_metadata(meta, None, bounded=True) == meta
    assert bound_metadata(meta, None, bounded=False) == meta
    assert bound_metadata(None, 50, bounded=True) is None


def test_a_64kb_sidecar_value_is_cut_and_listed():
    meta = {"tika": {"title": "Q3"}, "sidecar": {"notes": "n" * 64_000, "vendor": "Acme"}}
    out = bound_metadata(meta, None, bounded=True)
    assert _json_len(out) <= METADATA_MAX_CHARS
    assert out["sidecar"]["notes"] == "n" * (METADATA_VALUE_MAX_CHARS - _escaped(ELLIPSIS)) + ELLIPSIS
    assert out["sidecar"]["vendor"] == "Acme" and out["tika"] == {"title": "Q3"}
    assert out[TRUNCATED_KEY] == ["sidecar.notes"]


def test_a_large_payload_keeps_the_entries_that_fit_and_lists_the_rest():
    meta = {"sidecar": {f"k{i:02d}": "v" * 900 for i in range(20)}, "tika": {"title": "kept?"}}
    out = bound_metadata(meta, None, bounded=True)
    assert _json_len(out) <= METADATA_MAX_CHARS
    kept = {f"sidecar.{k}" for k in out.get("sidecar", {})} | {f"tika.{k}" for k in out.get("tika", {})}
    listed = set(out[TRUNCATED_KEY])
    assert kept and listed
    # Every entry is either returned whole or listed as dropped: nothing vanishes silently.
    every = {f"sidecar.k{i:02d}" for i in range(20)} | {"tika.title"}
    assert kept | listed == every and not (kept & listed)
    for k, v in out["sidecar"].items():
        assert v == meta["sidecar"][k]


def test_ten_thousand_keys_keep_the_marker_list_bounded_too():
    meta = {"sidecar": {f"field_{i:05d}": i for i in range(10_000)}}
    out = bound_metadata(meta, None, bounded=True)
    assert _json_len(out) <= METADATA_MAX_CHARS
    assert out[TRUNCATED_KEY][-1].startswith("+") and out[TRUNCATED_KEY][-1].endswith(" more")


def test_nested_and_list_values_are_cut_by_their_own_path():
    meta = {"sidecar": {"parties": [{"name": "p" * 2000}, "q" * 1500, 7], "n": 1.5}}
    out = bound_metadata(meta, None, bounded=True)
    assert out["sidecar"]["parties"][0]["name"].endswith(ELLIPSIS)
    assert out["sidecar"]["parties"][1].endswith(ELLIPSIS)
    assert out["sidecar"]["parties"][2] == 7 and out["sidecar"]["n"] == 1.5
    assert out[TRUNCATED_KEY] == ["sidecar.parties[0].name", "sidecar.parties[1]"]


@pytest.mark.parametrize("bounded", [True, False])
def test_a_capped_key_gets_each_string_value_as_its_prefix(bounded):
    meta = {"tika": {"description": "d" * 300, "pages": 4}, "sidecar": {"tags": ["t" * 80, "ok"], "flag": False}}
    out = bound_metadata(meta, 50, bounded=bounded)
    assert out == {"tika": {"description": "d" * 50, "pages": 4}, "sidecar": {"tags": ["t" * 50, "ok"], "flag": False}}


@pytest.mark.parametrize("cap", [0, -5])
def test_a_cap_of_zero_nulls_every_string_and_keeps_the_rest(cap):
    meta = {"tika": {"description": "d" * 300, "pages": 4}, "sidecar": {"tags": ["x", "y"], "flag": True}}
    out = bound_metadata(meta, cap, bounded=True)
    assert out == {"tika": {"description": None, "pages": 4}, "sidecar": {"tags": [None, None], "flag": True}}


def test_hint_bounds_are_tighter():
    fields = {f"sidecar.f{i}": "h" * 500 for i in range(10)}
    out = bound_metadata(fields, None, bounded=True, value_max=HINT_VALUE_MAX_CHARS, total_max=HINT_MAX_CHARS)
    assert _json_len(out) <= HINT_MAX_CHARS
    for k, v in out.items():
        if k != TRUNCATED_KEY:
            assert _escaped(v) == HINT_VALUE_MAX_CHARS and v.endswith(ELLIPSIS)


@pytest.mark.parametrize("char", ["é", "東", "😀"])
def test_bounds_count_escaped_characters(char):
    """A non-ASCII character is 6 escaped characters in a tool response (12 outside the BMP): the
    bound counts what is sent, not the code points."""
    out = bound_metadata({"sidecar": {"notes": char * 1_000}}, None, bounded=True)
    note = out["sidecar"]["notes"]
    assert note.endswith(ELLIPSIS) and _escaped(note) <= METADATA_VALUE_MAX_CHARS
    assert _escaped(note) > METADATA_VALUE_MAX_CHARS - _escaped(char) - _escaped(ELLIPSIS)
    summary = bound_summary(char * 2_000, None, bounded=True)
    assert _escaped(summary) <= SUMMARY_MAX_CHARS


def test_indented_depth_is_counted():
    """kb_get_document sends metadata indented, one level deep: the bound holds for that string."""
    meta = {"sidecar": {f"k{i:03d}": i for i in range(800)}}
    out = bound_metadata(meta, None, bounded=True, depth=1)
    sent = json.dumps({"metadata": out}, indent=2)
    assert len(sent) - len('{\n  "metadata": \n}') <= METADATA_MAX_CHARS


def test_a_stored_truncated_namespace_does_not_collide_with_the_marker():
    out = bound_metadata({"_truncated": {"x": 1}, "sidecar": {"n": "n" * 2_000}}, None, bounded=True)
    assert out["_truncated (stored)"] == {"x": 1} and out[TRUNCATED_KEY] == ["sidecar.n"]


def test_list_tool_results_degrade_by_dropping_rows():
    """The bounds are per item; a list tool past one chat result is cut by whole rows from its array,
    so what the model reads is still valid JSON. kb_verify_identifier's array is "candidates"."""
    from harbor_clerk.llm.chat import _truncate_for_llm

    for key in ("documents", "related", "candidates"):
        rows = [{"doc_id": str(i), "summary": "s" * 1_400} for i in range(40)]
        out = _truncate_for_llm(json.dumps({key: rows}), CHAT_RESULT_BUDGET_FLOOR_CHARS)
        data = json.loads(out)
        assert 0 < len(data[key]) < 40 and data["_original_count"] == 40, key

"""Tests for the CLI selector parser in sweep.py.

The previous implementation called ``dict(...)`` over the comma-split parts,
silently collapsing repeated keys to the last value. The runbook's documented
multi-id syntax (``--rerun 'id=A,id=B,id=C'``) silently matched zero units
as a result.
"""

from __future__ import annotations

from scripts.test_corpora.runner.sweep import _parse_selectors


def test_empty_string_returns_empty_dict():
    assert _parse_selectors("") == {}


def test_single_key_value_pair():
    assert _parse_selectors("phase=4") == {"phase": ["4"]}


def test_distinct_keys_each_become_a_list():
    assert _parse_selectors("phase=4,corpus=cuad") == {"phase": ["4"], "corpus": ["cuad"]}


def test_repeated_keys_accumulate_into_a_list_in_order():
    """Regression: ``dict(...)`` collapsed repeated keys to the last value, so
    the documented runbook syntax was silently broken."""
    assert _parse_selectors("id=A,id=B,id=C") == {"id": ["A", "B", "C"]}


def test_mixed_repeated_and_distinct_keys():
    result = _parse_selectors("id=cuad-ask-1,id=cuad-ask-2,id=enron-research-1,phase=4")
    assert result == {
        "id": ["cuad-ask-1", "cuad-ask-2", "enron-research-1"],
        "phase": ["4"],
    }


def test_values_containing_equals_signs_survive_intact():
    """``error=~no=match found`` should parse with ``~no=match found`` as the
    value (split only on the first ``=``)."""
    assert _parse_selectors("error=~no=match found") == {"error": ["~no=match found"]}


def test_garbage_parts_without_equals_are_dropped():
    assert _parse_selectors("phase=4,garbage,corpus=cuad") == {"phase": ["4"], "corpus": ["cuad"]}

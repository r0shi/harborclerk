"""Unit tests for src/harbor_clerk/sql_escape.py."""

from harbor_clerk.sql_escape import escape_ilike


def test_escape_ilike_passes_through_safe_input():
    assert escape_ilike("hello world") == "hello world"


def test_escape_ilike_escapes_percent():
    assert escape_ilike("50% off") == "50\\% off"


def test_escape_ilike_escapes_underscore():
    assert escape_ilike("contract_2024") == "contract\\_2024"


def test_escape_ilike_escapes_backslash():
    assert escape_ilike(r"path\to\file") == r"path\\to\\file"


def test_escape_ilike_handles_multiple_metacharacters():
    assert escape_ilike("a%b_c\\d") == "a\\%b\\_c\\\\d"


def test_escape_ilike_empty_string():
    assert escape_ilike("") == ""

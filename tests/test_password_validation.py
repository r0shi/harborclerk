"""Tests for password validation rules."""

from harbor_clerk.password_validation import validate_password


def test_valid_password():
    assert validate_password("StrongPass123!", "user@example.com") == []


def test_too_short():
    errors = validate_password("Short1Aa", "user@example.com")
    assert any("at least 12" in e for e in errors)


def test_missing_uppercase():
    errors = validate_password("alllowercase123", "user@example.com")
    assert any("uppercase" in e for e in errors)


def test_missing_lowercase():
    errors = validate_password("ALLUPPERCASE123", "user@example.com")
    assert any("lowercase" in e for e in errors)


def test_missing_digit():
    errors = validate_password("NoDigitsHereABC", "user@example.com")
    assert any("digit" in e for e in errors)


def test_contains_email():
    errors = validate_password("user@example.comAbc1", "user@example.com")
    assert any("email" in e for e in errors)


def test_single_repeated_char():
    errors = validate_password("aaaaaaaaaaaa", "user@example.com")
    assert any("repeated" in e for e in errors)


def test_multiple_errors():
    errors = validate_password("short", "user@example.com")
    assert len(errors) >= 2  # too short + missing uppercase + missing digit


def test_empty_email_skips_email_check():
    # Empty email should not trigger email-contains check
    errors = validate_password("StrongPass123!", "")
    assert errors == []

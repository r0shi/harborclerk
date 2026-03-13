"""Tests for OAuth 2.1 config settings."""

from harbor_clerk.config import Settings


def test_default_refresh_token_days():
    s = Settings(database_url="postgresql+asyncpg://x:x@localhost/x")
    assert s.oauth_refresh_token_days == 90


def test_custom_refresh_token_days(monkeypatch):
    monkeypatch.setenv("OAUTH_REFRESH_TOKEN_DAYS", "30")
    s = Settings(database_url="postgresql+asyncpg://x:x@localhost/x")
    assert s.oauth_refresh_token_days == 30


def test_public_url_defaults_empty():
    s = Settings(database_url="postgresql+asyncpg://x:x@localhost/x")
    assert s.public_url == ""


def test_public_url_strips_trailing_slash():
    s = Settings(
        database_url="postgresql+asyncpg://x:x@localhost/x",
        public_url="https://example.com/",
    )
    assert s.public_url == "https://example.com"


def test_public_url_strips_multiple_trailing_slashes():
    s = Settings(
        database_url="postgresql+asyncpg://x:x@localhost/x",
        public_url="https://example.com///",
    )
    assert s.public_url == "https://example.com"


def test_oauth_access_token_minutes_default():
    s = Settings(database_url="postgresql+asyncpg://x:x@localhost/x")
    assert s.oauth_access_token_minutes == 60

"""Tests for OAuth 2.1 config settings and OAuth server core helpers."""

import base64
import hashlib

from harbor_clerk.config import Settings
from harbor_clerk.oauth import (
    _verify_code_challenge,
    generate_client_secret,
    generate_token,
    hash_token,
)


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


# ---------------------------------------------------------------------------
# OAuth helper tests
# ---------------------------------------------------------------------------


class TestOAuthHelpers:
    def test_generate_token_length_and_type(self):
        token = generate_token()
        assert isinstance(token, str)
        assert len(token) == 64

    def test_generate_client_secret_length(self):
        secret = generate_client_secret()
        assert isinstance(secret, str)
        assert len(secret) > 32

    def test_hash_token_deterministic(self):
        token = "test-token-abc123"
        assert hash_token(token) == hash_token(token)
        assert hash_token(token) != hash_token("different-token")

    def test_tokens_are_unique(self):
        tokens = {generate_token() for _ in range(100)}
        assert len(tokens) == 100


class TestPKCE:
    def test_s256_valid(self):
        verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        assert _verify_code_challenge(verifier, challenge, "S256") is True

    def test_s256_invalid(self):
        verifier = "correct-verifier"
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        assert _verify_code_challenge("wrong-verifier", challenge, "S256") is False

    def test_plain_rejected(self):
        verifier = "some-verifier"
        assert _verify_code_challenge(verifier, verifier, "plain") is False

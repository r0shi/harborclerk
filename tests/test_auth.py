"""Tests for auth module: password hashing, API keys, JWT tokens."""

import uuid
from datetime import datetime, timedelta, timezone

import jwt
import pytest

from harbor_clerk.auth import (
    create_access_token,
    create_refresh_token,
    decode_token,
    generate_api_key,
    hash_api_key,
    hash_password,
    verify_password,
)
from harbor_clerk.config import get_settings


# --- Password hashing ---


def test_hash_verify_roundtrip():
    pw = "MySecurePassword1"
    hashed = hash_password(pw)
    assert verify_password(pw, hashed)


def test_wrong_password():
    hashed = hash_password("CorrectPassword1")
    assert not verify_password("WrongPassword1", hashed)


def test_different_salts():
    pw = "SamePassword123"
    h1 = hash_password(pw)
    h2 = hash_password(pw)
    assert h1 != h2  # Different salts
    assert verify_password(pw, h1)
    assert verify_password(pw, h2)


# --- API key ---


def test_hash_api_key_deterministic():
    key = "lka_abc123"
    assert hash_api_key(key) == hash_api_key(key)


def test_generate_api_key_prefix():
    key = generate_api_key()
    assert key.startswith("lka_")
    assert len(key) > 20


def test_generate_api_key_unique():
    keys = {generate_api_key() for _ in range(10)}
    assert len(keys) == 10


# --- JWT ---


def test_access_token_roundtrip():
    uid = uuid.uuid4()
    token = create_access_token(uid, "admin")
    payload = decode_token(token)
    assert payload["sub"] == str(uid)
    assert payload["role"] == "admin"
    assert payload["type"] == "access"


def test_refresh_token_roundtrip():
    uid = uuid.uuid4()
    token = create_refresh_token(uid)
    payload = decode_token(token)
    assert payload["sub"] == str(uid)
    assert payload["type"] == "refresh"


def test_expired_token():
    settings = get_settings()
    payload = {
        "sub": str(uuid.uuid4()),
        "type": "access",
        "exp": datetime.now(timezone.utc) - timedelta(hours=1),
    }
    token = jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)
    with pytest.raises(jwt.ExpiredSignatureError):
        decode_token(token)


def test_invalid_token():
    with pytest.raises(jwt.PyJWTError):
        decode_token("not.a.valid.token")


def test_wrong_secret():
    payload = {
        "sub": str(uuid.uuid4()),
        "type": "access",
        "exp": datetime.now(timezone.utc) + timedelta(hours=1),
    }
    token = jwt.encode(payload, "wrong-secret", algorithm="HS256")
    with pytest.raises(jwt.PyJWTError):
        decode_token(token)

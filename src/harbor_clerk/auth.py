"""JWT token management, password hashing, and API key hashing."""

import hashlib
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from harbor_clerk.config import get_settings


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode(), password_hash.encode())


def hash_api_key(raw_key: str) -> str:
    """SHA256 hash for fast API key lookup (high-entropy keys don't need bcrypt)."""
    return hashlib.sha256(raw_key.encode()).hexdigest()


def generate_api_key() -> str:
    """Generate a random API key prefixed with 'hc_'."""
    return f"hc_{uuid.uuid4().hex}{uuid.uuid4().hex[:16]}"


# Prefixes recognized as API keys (hc_ is current, lka_ is legacy)
API_KEY_PREFIXES = ("hc_", "lka_")


def create_access_token(user_id: uuid.UUID, role: str) -> str:
    settings = get_settings()
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=settings.jwt_access_token_expire_minutes
    )
    payload = {
        "sub": str(user_id),
        "role": role,
        "type": "access",
        "exp": expire,
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def create_refresh_token(user_id: uuid.UUID) -> str:
    settings = get_settings()
    expire = datetime.now(timezone.utc) + timedelta(
        days=settings.jwt_refresh_token_expire_days
    )
    payload = {
        "sub": str(user_id),
        "type": "refresh",
        "exp": expire,
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict:
    """Decode and validate a JWT. Raises jwt.PyJWTError on failure."""
    settings = get_settings()
    return jwt.decode(
        token, settings.secret_key, algorithms=[settings.jwt_algorithm]
    )

"""IMAPConnection wraps aioimaplib with typed exceptions."""

from harbor_clerk.mail.exceptions import AuthError
from harbor_clerk.mail.imap_client import IMAPConnection


def test_constructor_stores_credentials():
    conn = IMAPConnection(host="imap.example.com", port=993, username="alex@example.com", password="abcd")
    assert conn.host == "imap.example.com"
    assert conn.port == 993
    assert conn.username == "alex@example.com"
    # Password is NOT exposed as a public attribute — only available during login()
    assert not hasattr(conn, "password")


def test_AuthError_is_distinct_exception_type():
    exc = AuthError("invalid credentials")
    assert str(exc) == "invalid credentials"
    assert isinstance(exc, Exception)

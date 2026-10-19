"""The SPA fallback serves only files inside the static directory, and a GET to an
unknown API path returns a JSON 404 (#603). Other methods on an unknown path keep
the JSON 405 they got before: the fallback is a GET route.

Requests go straight to the ASGI app with a hand-built scope, decoded the way
uvicorn decodes a request target (``urllib.parse.unquote`` of the raw path, no
dot-segment removal), because an HTTP client would normalise the paths these
tests need to send.
"""

from __future__ import annotations

import errno
import json
from pathlib import Path
from urllib.parse import unquote

import pytest

SECRET = b"SECRET-OUTSIDE-STATIC-DIR"
INDEX = b"<!doctype html><title>Harbor Clerk</title>"
ASSET = b"console.log('app');"
FAVICON = b"\x00\x01favicon"


@pytest.fixture
def static_layout(tmp_path: Path, monkeypatch) -> dict[str, Path]:
    """A static dir with an index, an asset and a favicon, and a secret file beside it."""
    import harbor_clerk.config as _config

    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_bytes(INDEX)
    (static / "assets" / "app.js").write_bytes(ASSET)
    (static / "favicon.ico").write_bytes(FAVICON)
    secret = tmp_path / "secret.txt"
    secret.write_bytes(SECRET)
    (static / "leak.txt").symlink_to(secret)
    (static / "outside").symlink_to(tmp_path, target_is_directory=True)
    (static / "assets" / "leak.js").symlink_to(secret)

    monkeypatch.setenv("STATIC_DIR", str(static))
    _config._settings = None
    return {"static": static, "secret": secret}


@pytest.fixture
def app(static_layout):
    from harbor_clerk.api.app import create_app

    return create_app()


async def _get(app, raw_path: str) -> tuple[int, dict[str, str], bytes]:
    """GET ``raw_path`` (the request target as sent on the wire, query string included)."""
    raw_path, _, query = raw_path.partition("?")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": unquote(raw_path),
        "raw_path": raw_path.encode("latin-1"),
        "query_string": query.encode("latin-1"),
        "root_path": "",
        "headers": [(b"host", b"test")],
        "client": ("127.0.0.1", 0),
        "server": ("test", 80),
    }
    sent: dict = {"body": b""}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            sent["status"] = msg["status"]
            sent["headers"] = {k.decode().lower(): v.decode() for k, v in msg["headers"]}
        elif msg["type"] == "http.response.body":
            sent["body"] += msg.get("body", b"")

    await app(scope, receive, send)
    return sent["status"], sent["headers"], sent["body"]


TRAVERSALS = [
    "/../secret.txt",
    "/%2e%2e/secret.txt",
    "/..%2fsecret.txt",
    "/%2e%2e%2fsecret.txt",
    "/%2E%2E%2Fsecret.txt",
    "/%252e%252e%252fsecret.txt",
    "/..%252fsecret.txt",
    "/docs/../../secret.txt",
    "/docs/%2e%2e%2f%2e%2e%2fsecret.txt",
    "/favicon.ico/../../secret.txt",
    "/..%5csecret.txt",
    "/%2e%2e%5csecret.txt",
    "/secret.txt%00",
    "/%00/../secret.txt",
    "/assets/../../secret.txt",
    "/assets/%2e%2e%2f%2e%2e%2fsecret.txt",
]


@pytest.mark.parametrize("raw_path", TRAVERSALS)
async def test_traversal_never_serves_a_file_outside_static_dir(app, raw_path):
    """A dot-dot, backslash or NUL path is a 404 or the index, never the outside file."""
    status, _, body = await _get(app, raw_path)
    assert SECRET not in body
    assert status == 404 or (status == 200 and body == INDEX), (status, body[:80])


async def test_absolute_path_is_not_served(app, static_layout):
    """A path that is absolute after the leading slash is the index at most, never the file."""
    status, _, body = await _get(app, "/" + str(static_layout["secret"]))
    assert SECRET not in body
    assert status == 404 or (status == 200 and body == INDEX), (status, body[:80])


@pytest.mark.parametrize("raw_path", ["//docs", "//docs/3f6c", "//etc/hosts", "//favicon.ico", "//assets/app.js"])
async def test_a_leading_empty_segment_serves_the_index(app, raw_path):
    """``//docs`` is the index, as before this change; ``//<anything>`` never serves a file."""
    status, headers, body = await _get(app, raw_path)
    assert status == 200
    assert body == INDEX
    assert headers["content-type"].startswith("text/html")


@pytest.mark.parametrize("raw_path", ["/leak.txt", "/outside/secret.txt", "/assets/leak.js"])
async def test_symlink_out_of_static_dir_is_not_served(app, raw_path):
    """A symlink inside the static dir that resolves outside it is not followed."""
    status, _, body = await _get(app, raw_path)
    assert SECRET not in body
    assert status == 404


async def test_lookup_error_is_a_404_not_a_500(app, monkeypatch):
    """An OSError that escapes the file lookup is a 404 (defensive: os.path swallows most)."""
    import os

    long_segment = "x" * 300
    real_isfile = os.path.isfile

    def isfile(path):
        if long_segment in str(path):
            raise OSError(errno.ENAMETOOLONG, "File name too long", str(path))
        return real_isfile(path)

    monkeypatch.setattr(os.path, "isfile", isfile)
    status, headers, body = await _get(app, "/" + long_segment)
    assert status == 404
    assert json.loads(body) == {"detail": "Not Found"}


@pytest.mark.parametrize("raw_path", ["/..", "/docs/..", "/%2e%2e"])
async def test_dot_dot_alone_is_a_404(app, raw_path):
    status, _, body = await _get(app, raw_path)
    assert status == 404
    assert SECRET not in body


@pytest.mark.parametrize(
    ("raw_path", "expected"),
    [("/favicon.ico", FAVICON), ("/assets/app.js", ASSET), ("/index.html", INDEX)],
)
async def test_file_inside_static_dir_is_served(app, raw_path, expected):
    status, _, body = await _get(app, raw_path)
    assert status == 200
    assert body == expected


@pytest.mark.parametrize("raw_path", ["/", "/docs", "/docs/3f6c", "/c/abc", "/settings/api-keys", "/no-such-file.png"])
async def test_client_route_serves_the_index(app, raw_path):
    status, headers, body = await _get(app, raw_path)
    assert status == 200
    assert body == INDEX
    assert headers["content-type"].startswith("text/html")


@pytest.mark.parametrize(
    "raw_path",
    [
        "/api/x",
        "/api/definitely-not-a-route",
        "/api/folders",
        "/api/folders/",
        "/api",
        "/api/",
        "/api/../secret.txt",
        "/oauth/nope",
        "/.well-known/openid-configuration",
    ],
)
async def test_get_of_an_unknown_api_path_is_a_json_404(app, raw_path):
    status, headers, body = await _get(app, raw_path)
    assert status == 404
    assert headers["content-type"] == "application/json"
    assert json.loads(body) == {"detail": "Not Found"}


async def test_real_api_route_is_still_routed(app):
    status, headers, body = await _get(app, "/api/system/ping")
    assert status == 200
    assert json.loads(body) == {"status": "ok"}


@pytest.mark.parametrize(
    ("raw_path", "location"),
    [
        ("/api/system/ping/", "/api/system/ping"),
        ("/api/watch/folders/", "/api/watch/folders"),
        ("/api/system/ping/?a=1&b=two", "/api/system/ping?a=1&b=two"),
    ],
)
async def test_get_of_an_api_route_with_a_trailing_slash_redirects_to_it(app, raw_path, location):
    """As without the SPA catch-all (development): a 307 to the route without the slash."""
    status, headers, _ = await _get(app, raw_path)
    assert status == 307
    assert headers["location"] == location


@pytest.mark.parametrize(
    "raw_path",
    [
        "//evil.example/",
        "//evil.example/api/system/ping/",
        "/api//evil.example/",
        "/api/%2f%2fevil.example/",
        "/api/%5c%5cevil.example/",
        "/%5c%5cevil.example/",
        "/api/..//evil.example/",
        "/api/system/ping//",
        "/api///system/ping/",
    ],
)
async def test_a_trailing_slash_redirect_never_leaves_the_host(app, raw_path):
    """Whatever the path, a redirect target starts with exactly one slash; without a
    redirect the answer is a 404 or the SPA index."""
    status, headers, body = await _get(app, raw_path)
    if status in (301, 302, 303, 307, 308):
        location = headers["location"]
        assert location.startswith("/") and not location.startswith(("//", "/\\")), location
    else:
        assert status == 404 or (status == 200 and body == INDEX), (status, body[:80])


@pytest.mark.parametrize("raw_path", ["/docs/../favicon.ico", "/docs/%2e%2e/favicon.ico", "/x/../index.html"])
async def test_a_dot_dot_segment_is_a_404_even_when_it_resolves_inside(app, raw_path):
    """Only the `..` check catches these: they resolve to a file inside the static dir."""
    status, _, body = await _get(app, raw_path)
    assert status == 404, (status, body[:80])


async def test_a_backslash_is_a_404(app, static_layout):
    """Only the backslash check catches this: a file with that very name exists inside."""
    (static_layout["static"] / "x\\y.txt").write_bytes(b"backslash name")
    status, _, body = await _get(app, "/x%5cy.txt")
    assert status == 404, (status, body[:80])


async def test_a_nul_is_a_404_whatever_realpath_does_with_it(app, monkeypatch):
    """realpath raises on a NUL in Python 3.12+, but returned the path unchanged in older
    versions; simulated here, so only the NUL check refuses it."""
    import os

    real_realpath = os.path.realpath
    monkeypatch.setattr(os.path, "realpath", lambda p, *a, **k: p if "\x00" in str(p) else real_realpath(p, *a, **k))
    status, _, body = await _get(app, "/favicon.ico%00")
    assert status == 404, (status, body[:80])

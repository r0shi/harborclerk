"""Tests for scope field on research routes.

POST /research checks settings.llm_model_id before creating the DB row, so we
must patch get_settings() to return a valid model_id. We also patch
research_stream to a no-op async generator so the StreamingResponse completes
immediately — the row is committed before the generator runs, so the subsequent
GET can read it without any timing issues.
"""

import uuid
from collections.abc import AsyncGenerator
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(model_id: str = "qwen3-8b") -> MagicMock:
    """Return a minimal settings mock with llm_model_id set."""
    s = MagicMock()
    s.llm_model_id = model_id
    return s


async def _noop_research_stream(*args, **kwargs) -> AsyncGenerator[bytes, None]:
    """Minimal no-op replacement for research_stream.

    Yields a single SSE done event so the StreamingResponse body terminates
    cleanly. The conversation row is committed BEFORE this generator is called,
    so no DB interaction is needed here.
    """
    yield b'data: {"type": "done"}\n\n'


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_start_research_with_no_scope_defaults_to_empty(client, admin_token):
    with (
        patch("harbor_clerk.api.routes.research.get_settings", return_value=_make_settings()),
        patch("harbor_clerk.api.routes.research.research_stream", new=_noop_research_stream),
    ):
        r = await client.post(
            "/api/research",
            json={"question": "What are the termination clauses?", "time_limit_minutes": 15, "depth": "light"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
    assert r.status_code == 200
    rid = r.headers["x-research-id"]

    d = await client.get(
        f"/api/research/{rid}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert d.status_code == 200
    assert d.json()["scope"] == {}


async def test_start_research_with_folder_scope_persists(client, admin_token, two_folder_corpus):
    folder_a, _, _, _ = two_folder_corpus
    with (
        patch("harbor_clerk.api.routes.research.get_settings", return_value=_make_settings()),
        patch("harbor_clerk.api.routes.research.research_stream", new=_noop_research_stream),
    ):
        r = await client.post(
            "/api/research",
            json={
                "question": "Q",
                "time_limit_minutes": 15,
                "depth": "light",
                "scope": {"folder_ids": [str(folder_a.folder_id)]},
            },
            headers={"Authorization": f"Bearer {admin_token}"},
        )
    assert r.status_code == 200
    rid = r.headers["x-research-id"]

    d = await client.get(
        f"/api/research/{rid}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert d.status_code == 200
    assert d.json()["scope"] == {"folder_ids": [str(folder_a.folder_id)]}


async def test_start_research_rejects_unknown_folder_id(client, admin_token):
    with (
        patch("harbor_clerk.api.routes.research.get_settings", return_value=_make_settings()),
        patch("harbor_clerk.api.routes.research.research_stream", new=_noop_research_stream),
    ):
        r = await client.post(
            "/api/research",
            json={
                "question": "Q",
                "time_limit_minutes": 15,
                "depth": "light",
                "scope": {"folder_ids": [str(uuid.uuid4())]},
            },
            headers={"Authorization": f"Bearer {admin_token}"},
        )
    assert r.status_code == 422


async def test_start_research_rejects_unavailable_folder(client, admin_token, unavailable_folder):
    with (
        patch("harbor_clerk.api.routes.research.get_settings", return_value=_make_settings()),
        patch("harbor_clerk.api.routes.research.research_stream", new=_noop_research_stream),
    ):
        r = await client.post(
            "/api/research",
            json={
                "question": "Q",
                "time_limit_minutes": 15,
                "depth": "light",
                "scope": {"folder_ids": [str(unavailable_folder.folder_id)]},
            },
            headers={"Authorization": f"Bearer {admin_token}"},
        )
    assert r.status_code == 422

def test_watch_root_default_empty():
    from harbor_clerk.config import Settings

    s = Settings()
    assert s.watch_root == ""


def test_watch_root_from_env(monkeypatch):
    monkeypatch.setenv("WATCH_ROOT", "/data/watch")
    from harbor_clerk.config import Settings

    s = Settings()
    assert s.watch_root == "/data/watch"


def test_embed_settings_have_granite_defaults(monkeypatch):
    """Defaults match the embedding-v2 spec: Granite-R2, 768-dim, no prefix."""
    monkeypatch.delenv("EMBED_MODEL", raising=False)
    monkeypatch.delenv("EMBED_DIM", raising=False)
    monkeypatch.delenv("EMBED_NEEDS_PREFIX", raising=False)

    from harbor_clerk.config import Settings

    s = Settings()
    assert s.embed_model == "ibm-granite/granite-embedding-311m-multilingual-r2"
    assert s.embed_dim == 768
    assert s.embed_needs_prefix is False


def test_embed_settings_override_via_env(monkeypatch):
    """Env vars override defaults — required for the e5-small rollback path."""
    monkeypatch.setenv("EMBED_MODEL", "intfloat/multilingual-e5-small")
    monkeypatch.setenv("EMBED_DIM", "384")
    monkeypatch.setenv("EMBED_NEEDS_PREFIX", "true")

    from harbor_clerk.config import Settings

    s = Settings()
    assert s.embed_model == "intfloat/multilingual-e5-small"
    assert s.embed_dim == 384
    assert s.embed_needs_prefix is True

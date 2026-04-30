def test_watch_root_default_empty():
    from harbor_clerk.config import Settings

    s = Settings()
    assert s.watch_root == ""


def test_watch_root_from_env(monkeypatch):
    monkeypatch.setenv("WATCH_ROOT", "/data/watch")
    from harbor_clerk.config import Settings

    s = Settings()
    assert s.watch_root == "/data/watch"

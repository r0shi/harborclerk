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


def test_reranker_settings_have_defaults(monkeypatch):
    for var in (
        "RERANKER_ENABLED",
        "RERANKER_URL",
        "RERANKER_TOP_K_PAD",
        "RERANKER_POOL_SIZE",
        "RERANKER_STRICT",
        "RERANKER_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(var, raising=False)

    from harbor_clerk.config import Settings

    s = Settings()
    assert s.reranker_enabled is True
    assert s.reranker_url == "http://reranker:8001"
    assert s.reranker_top_k_pad == 40
    assert s.reranker_pool_size == 50
    assert s.reranker_strict is False
    assert s.reranker_timeout_seconds == 30.0


def test_reranker_url_override_for_macos(monkeypatch):
    """macOS native sets RERANKER_URL to 127.0.0.1 with the per-instance port."""
    monkeypatch.setenv("RERANKER_URL", "http://127.0.0.1:8201")

    from harbor_clerk.config import Settings

    s = Settings()
    assert s.reranker_url == "http://127.0.0.1:8201"


def test_enable_cli_access_defaults_false(monkeypatch):
    monkeypatch.delenv("ENABLE_CLI_ACCESS", raising=False)
    from harbor_clerk.config import Settings

    s = Settings()
    assert s.enable_cli_access is False


def test_enable_cli_access_reads_env(monkeypatch):
    monkeypatch.setenv("ENABLE_CLI_ACCESS", "true")
    from harbor_clerk.config import Settings

    s = Settings()
    assert s.enable_cli_access is True


def test_refresh_cli_access_setting_no_native_config(monkeypatch):
    """refresh_cli_access_setting is a no-op when native_config_file is unset."""
    monkeypatch.delenv("ENABLE_CLI_ACCESS", raising=False)
    from harbor_clerk import config as _config

    _config._settings = None
    monkeypatch.setenv("NATIVE_CONFIG_FILE", "")

    from harbor_clerk.config import refresh_cli_access_setting

    _config._settings = None
    # Should not raise, and enable_cli_access keeps its default False
    refresh_cli_access_setting()
    assert _config.get_settings().enable_cli_access is False


def test_refresh_cli_access_setting_reads_from_file(monkeypatch, tmp_path):
    """refresh_cli_access_setting picks up enable_cli_access=true from config.json."""
    config_file = tmp_path / "config.json"
    config_file.write_text('{"enable_cli_access": true}\n')

    monkeypatch.delenv("ENABLE_CLI_ACCESS", raising=False)
    monkeypatch.setenv("NATIVE_CONFIG_FILE", str(config_file))

    from harbor_clerk import config as _config

    _config._settings = None

    from harbor_clerk.config import refresh_cli_access_setting

    # Before refresh: default is False (env var absent)
    assert _config.get_settings().enable_cli_access is False

    refresh_cli_access_setting()
    assert _config.get_settings().enable_cli_access is True


def test_refresh_cli_access_setting_disable_after_enable(monkeypatch, tmp_path):
    """Writing enable_cli_access=false to config.json disables access after refresh."""
    config_file = tmp_path / "config.json"
    config_file.write_text('{"enable_cli_access": true}\n')

    monkeypatch.delenv("ENABLE_CLI_ACCESS", raising=False)
    monkeypatch.setenv("NATIVE_CONFIG_FILE", str(config_file))

    from harbor_clerk import config as _config

    _config._settings = None

    from harbor_clerk.config import refresh_cli_access_setting

    refresh_cli_access_setting()
    assert _config.get_settings().enable_cli_access is True

    # Toggle off
    config_file.write_text('{"enable_cli_access": false}\n')
    refresh_cli_access_setting()
    assert _config.get_settings().enable_cli_access is False


def test_refresh_cli_access_setting_key_absent_leaves_unchanged(monkeypatch, tmp_path):
    """If enable_cli_access key is absent from config.json, existing value is kept."""
    config_file = tmp_path / "config.json"
    config_file.write_text('{"other_key": 123}\n')

    monkeypatch.setenv("ENABLE_CLI_ACCESS", "true")
    monkeypatch.setenv("NATIVE_CONFIG_FILE", str(config_file))

    from harbor_clerk import config as _config

    _config._settings = None

    from harbor_clerk.config import refresh_cli_access_setting

    # env set it to True; config.json lacks the key — should stay True
    assert _config.get_settings().enable_cli_access is True
    refresh_cli_access_setting()
    assert _config.get_settings().enable_cli_access is True

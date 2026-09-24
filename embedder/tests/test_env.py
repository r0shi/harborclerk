"""positive_int_env is total: it is read at import by both services."""

from embedder.env import positive_int_env


def test_positive_int_env_reads_the_value(monkeypatch):
    monkeypatch.setenv("RERANK_BATCH_SIZE", "8")
    assert positive_int_env("RERANK_BATCH_SIZE", 4) == 8


def test_positive_int_env_defaults_on_unset_empty_or_junk(monkeypatch, caplog):
    monkeypatch.delenv("RERANK_BATCH_SIZE", raising=False)
    assert positive_int_env("RERANK_BATCH_SIZE", 4) == 4
    monkeypatch.setenv("RERANK_BATCH_SIZE", "")
    assert positive_int_env("RERANK_BATCH_SIZE", 4) == 4
    monkeypatch.setenv("RERANK_BATCH_SIZE", "many")
    assert positive_int_env("RERANK_BATCH_SIZE", 4) == 4
    assert "RERANK_BATCH_SIZE='many' is not an integer" in caplog.text


def test_positive_int_env_never_goes_below_one(monkeypatch):
    monkeypatch.setenv("RERANK_BATCH_SIZE", "0")
    assert positive_int_env("RERANK_BATCH_SIZE", 4) == 1

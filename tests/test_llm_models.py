from harbor_clerk.llm.models import ModelInfo, MODELS


def test_modelinfo_has_find_all_default_max_results():
    """New optional field; defaults to None on all curated models."""
    info = ModelInfo(
        id="dummy",
        name="Dummy",
        huggingface_repo="repo",
        filename="dummy.gguf",
        size_bytes=1,
        context_window=4096,
        supports_tools=True,
    )
    assert info.find_all_default_max_results is None


def test_all_curated_models_default_find_all_max_to_none():
    for m in MODELS.values():
        assert m.find_all_default_max_results is None, m.id

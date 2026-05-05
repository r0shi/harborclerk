"""Pytest fixtures shared across runner and corpora tests."""

from pathlib import Path

import pytest


@pytest.fixture
def tmp_results(tmp_path: Path) -> Path:
    """Disposable results directory for a single test."""
    d = tmp_path / "results"
    d.mkdir()
    return d


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def baseline_sample() -> dict:
    """A representative baseline JSON for metrics tests."""
    return {
        "question_id": "cuad-research-1",
        "answer": "Most contracts use 30, 60, or 90 day notice periods. California, Delaware appear frequently.",
        "cited_doc_ids": ["doc-a", "doc-b", "doc-c", "doc-d", "doc-e"],
        "named_entities": ["California", "Delaware", "Acme", "30 days", "60 days"],
        "elapsed_seconds": 87.4,
    }

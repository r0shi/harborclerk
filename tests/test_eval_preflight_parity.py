"""The eval harness is its own project and does not import the app, so its machine preflight carries a copy
of the registry's fit arithmetic (Swift carries another). This holds the copy to the original."""

import pytest

from harbor_clerk.llm.models import HOST_HEADROOM_BYTES, MODELS, RUNTIME_OVERHEAD_BYTES, max_context
from scripts.test_corpora import preflight

GIB = 1024**3


def test_the_harness_reads_every_model_and_its_memory_figures_from_the_registry():
    read = preflight.registry()
    assert list(read) == list(MODELS), "every model, in the registry's order"
    for model_id, m in MODELS.items():
        assert read[model_id] == {
            "size_bytes": m.size_bytes,
            "kv_bytes_per_token": m.kv_bytes_per_token,
            "kv_fixed_bytes": m.kv_fixed_bytes,
        }
    assert preflight._constant("RUNTIME_OVERHEAD_BYTES") == RUNTIME_OVERHEAD_BYTES
    assert preflight._constant("HOST_HEADROOM_BYTES") == HOST_HEADROOM_BYTES


@pytest.mark.parametrize("ram_gib", [8, 12, 16, 18, 24, 32, 36, 48, 64, 128])
def test_the_preflight_says_a_model_fits_exactly_when_the_app_would_load_it(ram_gib):
    read = preflight.registry()
    for model_id, m in MODELS.items():
        ours = preflight.fits(
            read[model_id], ram_gib * GIB, overhead=RUNTIME_OVERHEAD_BYTES, headroom=HOST_HEADROOM_BYTES
        )
        assert ours == (max_context(m, ram_gib * GIB) > 0), f"{model_id} at {ram_gib} GiB"

import os
import re
import struct
from pathlib import Path

import pytest

from harbor_clerk.llm.models import (
    FREE_MEMORY_DIVISOR,
    HOST_HEADROOM_BYTES,
    MODELS,
    RUNTIME_OVERHEAD_BYTES,
    TIGHT_FIT_CONTEXT,
    ModelInfo,
    YarnConfig,
    free_margin_bytes,
    kv_bytes,
    max_context,
    memory_bytes,
    min_ram_gb,
    ram_required_bytes,
    swa_cache_cells,
    system_ram_bytes,
)


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


def test_modelinfo_parallel_slots_defaults_to_one():
    """Heavy-tier-safe default — no model gets implicit extra KV cache."""
    info = ModelInfo(
        id="dummy",
        name="Dummy",
        huggingface_repo="repo",
        filename="dummy.gguf",
        size_bytes=1,
        context_window=4096,
        supports_tools=True,
    )
    assert info.parallel_slots == 1


def test_every_curated_model_runs_one_slot():
    """A slot divides -c; it does not add memory. Two slots halved every
    request's context (Qwen3-8B budgeted 32K prompts for a 16K slot) for
    concurrency the product barely uses, so every model runs one. Raising a
    model's slots is a decision about its per-request context: change it here
    on purpose, and divide what the models API reports (`max_context_here` is
    the total -c) the way `context_budget()` already does."""
    assert {m.id: m.parallel_slots for m in MODELS.values()} == dict.fromkeys(MODELS, 1)


# --- memory budget (#556) ---------------------------------------------------------------------------------------

MODELS_DIR = Path.home() / "Library/Application Support/Harbor Clerk/models"


def _info(**over) -> ModelInfo:
    base = dict(
        id="dummy",
        name="Dummy",
        huggingface_repo="repo",
        filename="dummy.gguf",
        size_bytes=5_000_000_000,
        context_window=32768,
        supports_tools=True,
        kv_bytes_per_token=147_456,
    )
    return ModelInfo(**{**base, **over})


def test_every_curated_model_states_its_kv_cost():
    """Without it the budget is weights alone, which is what let a 19 GB KV cache be launched by hand."""
    for m in MODELS.values():
        assert m.kv_bytes_per_token > 0, m.id


def test_gemma_context_window_is_the_ggufs_not_the_small_models():
    assert MODELS["gemma4-26b-a4b"].context_window == 262144  # #548
    assert MODELS["gpt-oss-20b"].context_window == 128000  # correct as is; #548 says so


def test_memory_arithmetic_is_weights_plus_kv_plus_overhead():
    m = _info()
    assert kv_bytes(m, 32768) == 147_456 * 32768
    assert memory_bytes(m) == 5_000_000_000 + 147_456 * 32768 + RUNTIME_OVERHEAD_BYTES
    assert memory_bytes(m, 131072) - memory_bytes(m, 32768) == 147_456 * (131072 - 32768)
    # With a tenth of the Mac free (#684): the smallest R with R - R // 10 >= what the model and the host use.
    need = memory_bytes(m) + HOST_HEADROOM_BYTES
    assert (
        ram_required_bytes(m) == -(-need * 10 // 9)
        and ram_required_bytes(m) - free_margin_bytes(ram_required_bytes(m)) >= need
    )
    fixed = _info(kv_bytes_per_token=20_480, kv_fixed_bytes=209_715_200)
    assert kv_bytes(fixed, 0) == 209_715_200 and kv_bytes(fixed, 1000) == 209_715_200 + 20_480_000


def test_min_ram_rounds_up_to_a_size_macs_come_with():
    # 5 + 4.8 + 1 + 6 = 16.8e9 bytes, and a tenth of the Mac free on top: 18.7e9 = 17.4 GiB. Without the margin
    # this was a 16 GB model, and a 16 GB Mac does still load it, at less than its window.
    assert min_ram_gb(_info()) == 18
    assert min_ram_gb(_info(size_bytes=2_500_000_000)) == 16, "14.3e9 and the margin: 15.9e9 = 14.8 GiB"
    assert min_ram_gb(_info(size_bytes=6_000_000_000)) == 24, "17.8e9 and the margin: 19.8e9 = 18.5 GiB, past 18"
    assert min_ram_gb(MODELS["qwen36-35b-a3b"]) == 36, "the 35B-A3B at full context is a 36 GB model, not a 32 GB one"
    assert min_ram_gb(MODELS["gemma4-26b-a4b"]) == 32, "#548 does not move Gemma out of the 32 GB tier"
    assert min_ram_gb(_info(size_bytes=600_000_000_000)) == 634, "past the largest Mac, the exact number of GiB"


def test_the_tier_a_model_is_listed_under_is_the_smallest_mac_that_gives_it_its_whole_window():
    """The models page groups by `min_ram_gb` and states `max_context` per Mac. They must not disagree."""
    from harbor_clerk.llm.models import GIB, MAC_RAM_TIERS_GB

    for m in MODELS.values():
        tier = min_ram_gb(m)
        assert max_context(m, tier * GIB) == m.context_window, m.id
        smaller = [t for t in MAC_RAM_TIERS_GB if t < tier]
        assert not smaller or max_context(m, smaller[-1] * GIB) < m.context_window, m.id


def test_max_context_is_what_fits_never_more_than_the_model_offers_and_zero_when_the_weights_alone_do_not():
    m = _info()
    plenty = 64 * 1024**3
    assert max_context(m, plenty) == 32768
    # 5 GB weights + 1 GB overhead + 6 GB headroom = 12 GB, at 147,456 bytes a token. A tenth of the Mac stays
    # free where the model allows it (#684):
    # 20 GB: 8 GB spare less 2 GB free is 40690 tokens, past what the model offers.
    assert max_context(m, 20_000_000_000) == 32768
    # 17 GB: 5 GB spare less 1.7 GB free is 22379 tokens, 21504 in 1024s. Over the working context: the margin holds.
    assert max_context(m, 17_000_000_000) == 21504
    # 16 GB: 4 GB spare fits 26624, and less 1.6 GB free only 15360. It fits only by eating into the margin, so
    # it gets the working context and no more.
    assert max_context(m, 16_000_000_000) == TIGHT_FIT_CONTEXT == 16384
    # 14 GB: 2 GB spare fits 13312 and the margin leaves 3072. What fits, since that is under the working context.
    assert max_context(m, 14_000_000_000) == 13312
    assert max_context(m, 12_500_000_000) == 0, "3389 tokens is not worth running"
    assert max_context(m, 11_000_000_000) == 0
    assert max_context(_info(kv_bytes_per_token=0), 16_000_000_000) == 32768, (
        "no per-token cost: the model's own window"
    )
    assert max_context(_info(kv_bytes_per_token=0), 11_000_000_000) == 0
    fixed = _info(kv_bytes_per_token=20_480, kv_fixed_bytes=209_715_200, context_window=262144)
    assert (
        max_context(fixed, 16_000_000_000) == (16_000_000_000 - 12_209_715_200 - 1_600_000_000) // 20_480 // 1024 * 1024
    )
    assert free_margin_bytes(16_000_000_000) == 1_600_000_000 and free_margin_bytes(0) == 0
    # No per-token cost and nothing left once the margin is kept: the working context, not the whole window.
    assert max_context(_info(kv_bytes_per_token=0), 13_000_000_000) == 16384


def test_effective_context_is_the_requested_window_clamped_to_what_fits():
    from harbor_clerk.llm.models import effective_context, requested_context

    m = _info(yarn=YarnConfig(extended_context=131072, rope_scale=4.0, original_context=32768))
    assert requested_context(m, False) == 32768 and requested_context(m, True) == 131072
    assert requested_context(_info(), True) == 32768, "YaRN on, but the model has none"
    plenty = 64 * 1024**3
    assert effective_context(m, True, plenty) == 131072
    assert effective_context(m, True, 17_000_000_000) == 21504, "the YaRN window, clamped to what 17 GB has room for"
    assert effective_context(m, False, 17_000_000_000) == 21504
    assert effective_context(m, False, 0) == 32768, "memory unknown: the requested window"
    assert effective_context(m, True, 11_000_000_000) == 131072, (
        "does not fit at all: the launcher refuses; nothing to budget by"
    )
    assert max_context(m, plenty, requested=131072) == 131072 and max_context(m, plenty) == 32768


def test_this_machines_ram_is_readable():
    assert system_ram_bytes() > 4 * 1024**3


def _gguf_header(path: Path, prefix: str) -> dict:
    """The architecture keys of a GGUF header. Only the header is read."""
    with path.open("rb") as f:
        return _read_header(f, prefix)


class _HubFile:
    """Just enough of a file for `_read_header`, over HTTP range requests."""

    def __init__(self, url: str):
        import httpx

        self._http, self._url, self._buf, self._pos = httpx, url, b"", 0
        head = httpx.head(url, follow_redirects=True, timeout=60)
        head.raise_for_status()
        self.size = int(head.headers.get("x-linked-size") or head.headers["content-length"])

    def read(self, n: int) -> bytes:
        while len(self._buf) - self._pos < n:
            start = len(self._buf)
            r = self._http.get(
                self._url,
                headers={"Range": f"bytes={start}-{start + max(n, 4 << 20) - 1}"},
                follow_redirects=True,
                timeout=120,
            )
            r.raise_for_status()
            assert r.status_code == 206, "the server ignored Range; refusing to read a whole model into memory"
            self._buf += r.content
        out = self._buf[self._pos : self._pos + n]
        self._pos += n
        return out


def _gguf_header_from_hub(repo: str, filename: str, prefix: str) -> tuple[dict, int]:
    f = _HubFile(f"https://huggingface.co/{repo}/resolve/main/{filename}")
    return _read_header(f, prefix), f.size


def _read_header(f, prefix: str) -> dict:
    def u32():
        return struct.unpack("<I", f.read(4))[0]

    def u64():
        return struct.unpack("<Q", f.read(8))[0]

    def string():
        return f.read(u64()).decode("utf-8", "replace")

    sizes = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f", 7: "<B", 10: "<Q", 11: "<q", 12: "<d"}

    def value(t):
        if t == 8:
            return string()
        if t == 9:
            et, n = u32(), u64()
            return [value(et) for _ in range(n)]
        return struct.unpack(sizes[t], f.read(struct.calcsize(sizes[t])))[0]

    assert f.read(4) == b"GGUF"
    u32()
    u64()
    out = {}
    for _ in range(u64()):
        k, t = string(), u32()
        v = value(t)
        if k.startswith(prefix + "."):
            out[k[len(prefix) + 1 :]] = v
    return out


# (model id, architecture prefix, layers whose KV grows with context out of block_count)
GEOMETRY = {
    "qwen3-8b": ("qwen3", "all"),
    "qwen3-4b": ("qwen3", "all"),
    "gemma4-12b": ("gemma4", "global"),
    "gemma4-26b-a4b": ("gemma4", "global"),  # sliding_window_pattern: 1 = windowed, 0 = global
    "gpt-oss-20b": ("gpt-oss", "half"),  # llama.cpp alternates window and full layers for this architecture
    "qwen36-35b-a3b": ("qwen35moe", "interval"),  # full_attention_interval
    "qwen35-9b": ("qwen35", "interval"),
    "qwen35-4b": ("qwen35", "interval"),
}


# Hybrid models: the recurrent state per slot, and so one context checkpoint, is derived from the header.
RECURRENT_STATE_DERIVED = {"qwen35-9b", "qwen35-4b", "qwen36-35b-a3b"}


def test_the_launcher_bounds_what_llama_server_keeps_in_host_ram_and_the_budget_knows_both():
    """llama-server keeps two things in host RAM that the launcher used to leave at their defaults: a prompt
    cache (8 GiB) and context checkpoints (32 per slot, each a copy of a sliding-window cache or a recurrent
    state: up to 10 GB for the 26B). Both are now bounded, by numbers the budget uses (#657).

    As a flag, a short flag, or llama.cpp's environment form, from anywhere in the app: the shared
    environment is built in ServiceManager, not in the launcher."""
    launcher = _swift("Services/LlamaService.swift")
    assert re.search(r'"--cache-ram",\s*String\(promptCacheMiB\)', launcher), "the cache is bounded by the budget"
    assert launcher.count('"--cache-ram"') == 1, "once: llama-server takes the last value it is given"
    assert '"--no-cache-idle-slots"' in launcher, (
        "with the cache off, llama-server is told so rather than left to complain"
    )
    assert "MemoryBudget.promptCacheMiB(" in launcher and "context: contextWindow" in launcher, (
        "from the context the launcher is about to pass, not the one it asked for"
    )
    assert re.search(r'"--ctx-checkpoints",\s*String\(MemoryBudget\.ctxCheckpoints\)', launcher)
    assert launcher.count('"--ctx-checkpoints"') == 1
    # The context and the cache are both sized from the fixed cost WITH the checkpoints in it.
    assert launcher.count("fixedBytes: settings.activeModelFixedBytes") == 2
    assert "activeModelKvFixedBytes" not in launcher, "that is the fixed KV alone: the checkpoints would be unbudgeted"
    for path in sorted(SWIFT_APP.rglob("*.swift")):
        source = _swift(str(path.relative_to(SWIFT_APP)))
        for name in ("LLAMA_ARG_CACHE_RAM", '"-cram"', "LLAMA_ARG_CTX_CHECKPOINTS", '"-ctxcp"', '"--swa-checkpoints"'):
            assert name not in source, f"{path.name}: {name} would bypass the budget"
        if path.name != "LlamaService.swift":
            assert "--cache-ram" not in source and "--ctx-checkpoints" not in source, (
                f"{path.name}: bounded in one place"
            )


def test_the_checkpoints_the_launcher_keeps_are_in_every_memory_figure():
    from harbor_clerk.llm.models import LLAMA_CTX_CHECKPOINTS, fixed_bytes

    assert LLAMA_CTX_CHECKPOINTS == 3, (
        "one request's worth at the pin: the last user message and two near the prompt's end"
    )
    gemma, qwen8 = MODELS["gemma4-26b-a4b"], MODELS["qwen3-8b"]
    assert fixed_bytes(gemma) == 314_572_800 + 3 * 314_572_800
    assert fixed_bytes(_info(kv_fixed_bytes=10, checkpoint_bytes=7, parallel_slots=2)) == 10 + 2 * 3 * 7
    assert fixed_bytes(qwen8) == 0 and qwen8.checkpoint_bytes == 0, "plain attention can roll back: it makes none"
    assert kv_bytes(gemma, 1000) == fixed_bytes(gemma) + 20_480_000
    assert memory_bytes(gemma, 0) == gemma.size_bytes + fixed_bytes(gemma) + RUNTIME_OVERHEAD_BYTES
    for m in MODELS.values():
        # A model with memory that cannot be rolled back makes checkpoints of it, and the others make none.
        assert (m.checkpoint_bytes > 0) == (m.kv_fixed_bytes > 0), m.id


def test_compose_bounds_what_llama_server_keeps_and_the_cache_holds_a_whole_saved_state():
    """The sibling site: Compose starts its own llama-server and cannot know the host's memory. At the pin a
    state larger than the cache limit is not cached at all, and a saved state is the KV plus the context
    checkpoints saved with it. With those bounded, `kv_bytes` is the whole of it, for every curated model
    at Compose's context."""
    from harbor_clerk.llm.models import MIB, PROMPT_CACHE_MAX_BYTES

    compose = (Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text()
    code = "\n".join(line.split("#")[0] for line in compose.splitlines())
    from harbor_clerk.llm.models import LLAMA_CTX_CHECKPOINTS

    assert re.search(rf'"--ctx-checkpoints",\s*"{LLAMA_CTX_CHECKPOINTS}"', code), (
        "the same bound the macOS launcher passes"
    )
    bound = int(re.search(r'"--cache-ram",\s*"(\d+)"', code).group(1)) * MIB
    context = int(re.search(r'"-c",\s*"(\d+)"', code).group(1))
    assert max(kv_bytes(m, context) for m in MODELS.values()) <= bound < PROMPT_CACHE_MAX_BYTES


def test_the_prompt_cache_gets_what_the_context_leaves():
    """Context first, cache second. Same cases as `testThePromptCacheGetsWhatTheContextLeaves` in Swift."""
    from harbor_clerk.llm.models import PROMPT_CACHE_MAX_BYTES, PROMPT_CACHE_MIN_TOKENS, prompt_cache_mib

    gib = 1024**3
    qwen8, gptoss, big, nine = MODELS["qwen3-8b"], MODELS["gpt-oss-20b"], MODELS["qwen36-35b-a3b"], MODELS["qwen35-9b"]
    assert prompt_cache_mib(qwen8, 32 * gib, 32768) == 8192, "room to spare: llama-server's own default, as before"
    # The free margin (a tenth of the Mac, #684) is not the cache's to spend. On an 18 GB Mac 32K of context
    # and the margin leave 510 MiB. At the pin a state larger than the limit is not cached at all, and a
    # 4096-token state of this model is 576 MiB: a cache in name only, so it is off. Before the margin this
    # Mac had 2353 MiB of cache; the margin comes first, then the context, then the cache.
    assert prompt_cache_mib(qwen8, 18 * gib, 32768) == 0
    assert prompt_cache_mib(qwen8, 24 * gib, 32768) == 6039, "what 32K of context and the margin leave on 24 GB"
    assert max_context(qwen8, 16 * gib) == 22528 and prompt_cache_mib(qwen8, 16 * gib, 22528) == 0
    # The cache never comes before the context. This model fits an 18 GB Mac only inside the margin, so it gets
    # the working context, and no cache.
    assert max_context(gptoss, 18 * gib) == TIGHT_FIT_CONTEXT and prompt_cache_mib(gptoss, 18 * gib, 16384) == 0
    # The mini's 35B. Sized to fill the machine it was given 241,664 tokens and Metal refused the first prompt;
    # 98,304 was measured working with 11% of the machine free, 131,072 with 8% (#684).
    assert max_context(big, 32 * gib) == 73_728 and prompt_cache_mib(big, 32 * gib, 73_728) == 0
    assert prompt_cache_mib(big, 36 * gib, 262_144) == 0, "21 MiB left once the margin is kept"
    # The fixed cost (recurrent state and the checkpoints kept of it) counts in what is left.
    assert max_context(nine, 18 * gib) == 137_216 and prompt_cache_mib(nine, 18 * gib, 137_216) == 0
    assert prompt_cache_mib(nine, 24 * gib, 262_144) == 1632
    # And in what a state needs. With 250 MiB left: a 4096-token state of this model is 329 MiB, 201 of it
    # fixed, so the cache is off. Forgetting the fixed part would switch on a cache that holds nothing.
    full = memory_bytes(nine, 262_144) + HOST_HEADROOM_BYTES
    assert full == 21_481_220_832

    def ram_leaving(spare: int) -> int:
        """The smallest Mac that has `spare` bytes left after the model, the headroom and its own margin."""
        ram = (full + spare) * 10 // 9
        while ram - free_margin_bytes(ram) - full < spare:
            ram += 1
        return ram

    assert prompt_cache_mib(nine, ram_leaving(250 * 1024**2), 262_144) == 0
    assert prompt_cache_mib(nine, ram_leaving(400 * 1024**2), 262_144) == 400
    assert prompt_cache_mib(qwen8, 0, 32768) == 0, "memory unknown"
    assert (8 * gib, 4096) == (PROMPT_CACHE_MAX_BYTES, PROMPT_CACHE_MIN_TOKENS)


# (context, --cache-ram in MiB) for every curated model on the Macs people have, with three context
# checkpoints budgeted for the models that make them. A change to any memory figure shows up here.
WHAT_EACH_MAC_GETS = {
    "qwen3-8b": {
        16: (22528, 0),
        18: (32768, 0),
        24: (32768, 6039),
        32: (32768, 8192),
        36: (32768, 8192),
        64: (32768, 8192),
    },
    "qwen3-4b": {
        16: (32768, 1080),
        18: (32768, 2923),
        24: (32768, 8192),
        32: (32768, 8192),
        36: (32768, 8192),
        64: (32768, 8192),
    },
    "qwen35-9b": {
        16: (77824, 0),
        18: (137216, 0),
        24: (262144, 1632),
        32: (262144, 8192),
        36: (262144, 8192),
        64: (262144, 8192),
    },
    "qwen35-4b": {
        16: (167936, 0),
        18: (226304, 0),
        24: (262144, 4435),
        32: (262144, 8192),
        36: (262144, 8192),
        64: (262144, 8192),
    },
    "gemma4-12b": {
        16: (16384, 0),
        18: (76800, 0),
        24: (262144, 2634),
        32: (262144, 8192),
        36: (262144, 8192),
        64: (262144, 8192),
    },
    "gemma4-26b-a4b": {16: (0, 0), 18: (0, 0), 24: (16384, 0), 32: (262144, 0), 36: (262144, 3936), 64: (262144, 8192)},
    "gpt-oss-20b": {
        16: (0, 0),
        18: (16384, 0),
        24: (128000, 1284),
        32: (128000, 8192),
        36: (128000, 8192),
        64: (128000, 8192),
    },
    "qwen36-35b-a3b": {16: (0, 0), 18: (0, 0), 24: (0, 0), 32: (73728, 0), 36: (262144, 0), 64: (262144, 8192)},
}


def test_the_cache_never_costs_a_model_its_context_or_its_place():
    """For every curated model on every Mac: the context and the cache are the pinned ones, the whole of it
    fits the machine, and a cache that is on can hold a conversation."""
    from harbor_clerk.llm.models import MIB, PROMPT_CACHE_MAX_BYTES, PROMPT_CACHE_MIN_TOKENS, prompt_cache_mib

    assert set(WHAT_EACH_MAC_GETS) == set(MODELS)
    for m in MODELS.values():
        for tier, (context, cache_mib) in WHAT_EACH_MAC_GETS[m.id].items():
            ram = tier * 1024**3
            assert max_context(m, ram) == context, f"{m.id} on {tier} GB"
            got = prompt_cache_mib(m, ram, context) if context else 0
            assert got == cache_mib, f"{m.id} on {tier} GB"
            if context:
                slack = ram - HOST_HEADROOM_BYTES - memory_bytes(m, context) - got * MIB
                assert slack >= 0
                # Only a model that would not otherwise run may eat into the free margin, and only this far.
                assert slack >= free_margin_bytes(ram) or context <= TIGHT_FIT_CONTEXT, f"{m.id} on {tier} GB"
            if got:
                assert kv_bytes(m, PROMPT_CACHE_MIN_TOKENS) <= got * MIB <= PROMPT_CACHE_MAX_BYTES


def test_the_budget_relies_on_how_the_pinned_llama_server_treats_a_state_over_the_limit():
    """At v0.4.1 `server_prompt_cache::alloc` skips a state larger than --cache-ram. The May build kept one
    state whatever the limit, and under that behaviour a small bound bounds nothing. Whoever moves the pin
    re-reads that function and moves PROMPT_CACHE_SEMANTICS_READ_AT with it."""
    from harbor_clerk.llm.models import PROMPT_CACHE_SEMANTICS_READ_AT

    script = (Path(__file__).resolve().parents[1] / "macos/scripts/build-llama.sh").read_text()
    pinned = re.search(r'LLAMA_CPP_TAG="\$\{LLAMA_CPP_TAG:-(v[\d.]+)\}"', script).group(1)
    assert pinned == PROMPT_CACHE_SEMANTICS_READ_AT


def test_what_the_qwen35_pair_gets_on_the_macs_people_have():
    """The figures the PR and the models page state. One slot, so all of it goes to each request."""
    gib = 1024**3
    nine, four = MODELS["qwen35-9b"], MODELS["qwen35-4b"]
    # With 33 copies of the recurrent state budgeted (llama-server's default of 32 checkpoints) these were
    # 83,968 and 173,056 on a 16 GB Mac. The launcher now keeps 3.
    # And a tenth of each Mac is left free (#684): 130,048 and 220,160 on 16 GB before that, 195,584 and 262,144 on 18 GB.
    assert [max_context(nine, g * gib) for g in (8, 16, 18, 24)] == [0, 77_824, 137_216, 262_144]
    assert [max_context(four, g * gib) for g in (8, 16, 18, 24)] == [0, 167_936, 226_304, 262_144]
    assert (min_ram_gb(nine), min_ram_gb(four)) == (24, 24), "at the full window; the launcher clamps below that"


@pytest.mark.parametrize("model_id", sorted(GEOMETRY))
def test_kv_cost_matches_the_gguf_header_when_the_file_is_here(model_id: str):
    """The constants were read from these headers; if a file changes under
    the same name, this notices. Skipped where the model is not downloaded."""
    m = MODELS[model_id]
    path = MODELS_DIR / m.filename
    prefix, growing = GEOMETRY[model_id]
    if not path.is_file() and os.environ.get("HC_GGUF_LIVE") != "1":
        pytest.skip(f"{m.filename} is not downloaded here; HC_GGUF_LIVE=1 reads its header from the Hub")
    if path.is_file():
        h, actual_size = _gguf_header(path, prefix), path.stat().st_size
    else:
        h, actual_size = _gguf_header_from_hub(m.huggingface_repo, m.filename, prefix)
    layers = h["block_count"]
    heads = h["attention.head_count_kv"]
    k, v = h["attention.key_length"], h.get("attention.value_length", h["attention.key_length"])
    if growing == "all":
        per_token = layers * heads * (k + v) * 2
    elif growing == "half":
        per_token = layers // 2 * heads * (k + v) * 2
        # The other half hold a sliding window, at the same head size, over the cells llama.cpp allocates.
        assert m.checkpoint_bytes == m.kv_fixed_bytes, f"{model_id}: a checkpoint copies the sliding-window cache"
        assert m.kv_fixed_bytes == layers // 2 * heads * (k + v) * 2 * swa_cache_cells(h["attention.sliding_window"]), (
            f"{model_id}: the header's window is {h['attention.sliding_window']}"
        )
    elif growing == "interval":
        per_token = layers // h["full_attention_interval"] * heads * (k + v) * 2
    else:
        pattern = h["attention.sliding_window_pattern"]
        per_token = sum(hd * (k + v) * 2 for hd, windowed in zip(heads, pattern, strict=True) if not windowed)
        # The windowed layers hold a sliding window of KV, at their own (smaller) head size, over the cells
        # llama.cpp allocates for it: the window plus a micro-batch, padded, not the window alone.
        k_swa, v_swa = h["attention.key_length_swa"], h["attention.value_length_swa"]
        per_cell = sum(hd * (k_swa + v_swa) * 2 for hd, windowed in zip(heads, pattern, strict=True) if windowed)
        assert m.checkpoint_bytes == m.kv_fixed_bytes, f"{model_id}: a checkpoint copies the sliding-window cache"
        assert m.kv_fixed_bytes == per_cell * swa_cache_cells(h["attention.sliding_window"]), (
            f"{model_id}: header says {per_cell} bytes per cell over a {h['attention.sliding_window']}-token window"
        )
    assert m.kv_bytes_per_token == per_token, f"{model_id}: header says {per_token} bytes per token"
    if model_id in RECURRENT_STATE_DERIVED:
        recurrent_layers = layers - layers // h["full_attention_interval"]
        conv_channels = h["ssm.inner_size"] + 2 * h["ssm.group_count"] * h["ssm.state_size"]
        per_layer = (h["ssm.state_size"] * h["ssm.inner_size"] + (h["ssm.conv_kernel"] - 1) * conv_channels) * 4
        per_slot = recurrent_layers * per_layer
        assert m.kv_fixed_bytes == m.parallel_slots * per_slot, (
            f"{model_id}: header says {per_slot} bytes of recurrent state per slot; the constant follows the slots"
        )
        assert m.checkpoint_bytes == per_slot, f"{model_id}: a context checkpoint is a copy of one slot's state"
    assert m.size_bytes == actual_size, (
        f"{model_id}: registry size {m.size_bytes} but the file is {actual_size}; the launcher measures the file"
    )
    assert m.context_window <= h["context_length"] or m.yarn is not None, (
        f"{model_id}: registry context exceeds the GGUF's"
    )


def test_prompt_budgets_come_from_one_function_that_follows_the_launcher(monkeypatch):
    """chat, research and summarize used to read the registry window each on
    their own; after the launcher started clamping -c, prompts were budgeted
    against a context llama-server was not running with."""
    import re

    from harbor_clerk.llm import chat, research, summarize
    from harbor_clerk.llm.models import context_budget

    monkeypatch.setattr("harbor_clerk.llm.models.system_ram_bytes", lambda: 17_000_000_000)
    assert context_budget(None, False) == 32768
    # llama-server splits -c across slots: `-c 32768 -np 2` reports 16384 per slot. A budget of the whole
    # window let a 20K-token conversation through to a 16K slot untrimmed.
    assert context_budget(_info(parallel_slots=2), False) == 21504 // 2
    # The shipped case: one slot, so Qwen3-8B's requests get the whole 32K.
    monkeypatch.setattr("harbor_clerk.llm.models.system_ram_bytes", lambda: 64 * 1024**3)
    assert context_budget(MODELS["qwen3-8b"], False) == 32768
    monkeypatch.setattr("harbor_clerk.llm.models.system_ram_bytes", lambda: 17_000_000_000)
    assert context_budget(_info(), False) == max_context(_info(), 17_000_000_000) == 21504, (
        "clamped, as the launcher clamps"
    )
    for module in (chat, research, summarize):
        code = "\n".join(
            line for line in Path(module.__file__).read_text().splitlines() if not line.lstrip().startswith("#")
        )
        assert "context_budget(" in code, module.__name__
        assert not re.search(r"\.context_window\b|\.extended_context\b", code), (
            f"{module.__name__} reads the registry window directly"
        )


SWIFT_APP = Path(__file__).resolve().parents[1] / "macos/HarborClerkServer/HarborClerkServer"


def _swift(rel: str) -> str:
    """A Swift source with its comments removed: a commented-out entry must not satisfy a guard."""
    text = (SWIFT_APP / rel).read_text()
    return re.sub(r"(?m)(^|\s)//.*$", "", re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL))


def test_the_preferences_picker_lists_the_registrys_models_and_their_sizes():
    block = re.search(r"let modelOptions: [^=]*= \[(.*?)\n\]", _swift("PreferencesWindow.swift"), re.DOTALL)
    assert block
    listed = dict(re.findall(r'\("([a-z0-9.-]+)",\s*"[^"]*\(([\d.]+) GB\)"\)', block.group(1)))
    assert listed == {m.id: f"{m.size_bytes / 1e9:.1f}" for m in MODELS.values()}


def test_a_sliding_window_cache_is_the_window_plus_a_micro_batch_padded():
    """`llama_kv_cache_iswa` at v0.4.1: GGML_PAD(min(size_base, n_swa + n_ubatch), 256). Found in review of
    #667: the registry had budgeted the window alone, a third too little for Gemma 4."""
    assert swa_cache_cells(1024) == 1536 and swa_cache_cells(128) == 768 and swa_cache_cells(4096) == 4608
    assert swa_cache_cells(1024, ubatch=2048) == 3072
    assert MODELS["gemma4-26b-a4b"].kv_fixed_bytes == 25 * 8 * (256 + 256) * 2 * 1536
    assert MODELS["gpt-oss-20b"].kv_fixed_bytes == 12 * 8 * (64 + 64) * 2 * 768


def test_nothing_in_the_app_changes_what_the_sliding_window_budget_assumes():
    """swa_cache_cells() and every sliding-window kv_fixed_bytes assume llama-server's defaults: a micro-batch
    of 512, one sequence, and a sliding-window cache that is not widened to the full context. As a flag or as
    llama.cpp's environment form, from anywhere in the app: the shared environment is built in
    ServiceManager, not in the launcher."""
    assumed = (
        '"-ub"', '"--ubatch-size"', '"-b"', '"--batch-size"', "LLAMA_ARG_UBATCH", "LLAMA_ARG_BATCH",
        '"--swa-full"', "LLAMA_ARG_SWA_FULL", '"--kv-unified"', '"-kvu"', "LLAMA_ARG_KV_UNIFIED",
    )  # fmt: skip
    for path in sorted(SWIFT_APP.rglob("*.swift")):
        source = _swift(str(path.relative_to(SWIFT_APP)))
        for flag in assumed:
            assert flag not in source, f"{path.name} sets {flag}: the sliding-window budget assumes the default"


def test_swift_mirrors_the_registrys_memory_tables():
    """Two hand-kept copies: the Swift launcher clamps by its own table and the
    API by the registry. `AppSettingsTests` pins Swift to hardcoded values;
    this pins those values to the registry, so a change on one side alone
    fails here."""
    swift = _swift("Settings.swift")

    def table(name: str) -> dict[str, int]:
        block = re.search(name + r"[^\[]*\[String: Int\] = \[(.*?)\n\s*\]", swift, re.DOTALL)
        assert block, name
        return {k: int(n.replace("_", "")) for k, n in re.findall(r'"([a-z0-9.-]+)":\s*([\d_]+)', block.group(1))}

    names = re.search(r"let filenames: \[String: String\] = \[(.*?)\n\s*\]", swift, re.DOTALL)
    assert names and dict(re.findall(r'"([a-z0-9.-]+)":\s*"([^"]+)"', names.group(1))) == {
        m.id: m.filename for m in MODELS.values()
    }, "a model missing here launches as 'Model file not found'"
    assert table("let slots") == {m.id: m.parallel_slots for m in MODELS.values()}
    from harbor_clerk.llm.models import LLAMA_CTX_CHECKPOINTS

    assert table("checkpointBytes") == {m.id: m.checkpoint_bytes for m in MODELS.values()}
    assert re.search(rf"static let ctxCheckpoints = {LLAMA_CTX_CHECKPOINTS}\b", swift)
    per_token, fixed, windows = table("kvBytesPerToken"), table("kvFixedBytes"), table("contextWindows")
    assert per_token == {m.id: m.kv_bytes_per_token for m in MODELS.values()}
    assert fixed == {m.id: m.kv_fixed_bytes for m in MODELS.values()}
    assert windows == {m.id: m.context_window for m in MODELS.values()}
    from harbor_clerk.llm.models import PROMPT_CACHE_MAX_BYTES, PROMPT_CACHE_MIN_TOKENS

    for name, value in (
        ("runtimeOverheadBytes", RUNTIME_OVERHEAD_BYTES),
        ("hostHeadroomBytes", HOST_HEADROOM_BYTES),
        ("freeMemoryDivisor", FREE_MEMORY_DIVISOR),
        ("tightFitContext", TIGHT_FIT_CONTEXT),
        ("promptCacheMaxBytes", PROMPT_CACHE_MAX_BYTES),
        ("promptCacheMinTokens", PROMPT_CACHE_MIN_TOKENS),
    ):
        assert re.search(rf"static let {name} = {value:_}\b", swift), name

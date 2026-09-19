import os
import re
import struct
from pathlib import Path

import pytest

from harbor_clerk.llm.models import (
    HOST_HEADROOM_BYTES,
    MODELS,
    RUNTIME_OVERHEAD_BYTES,
    ModelInfo,
    YarnConfig,
    kv_bytes,
    max_context,
    memory_bytes,
    min_ram_gb,
    ram_required_bytes,
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
    assert ram_required_bytes(m) == memory_bytes(m) + HOST_HEADROOM_BYTES
    fixed = _info(kv_bytes_per_token=20_480, kv_fixed_bytes=209_715_200)
    assert kv_bytes(fixed, 0) == 209_715_200 and kv_bytes(fixed, 1000) == 209_715_200 + 20_480_000


def test_min_ram_rounds_up_to_a_size_macs_come_with():
    assert min_ram_gb(_info()) == 16  # 5 + 4.8 + 1 + 6 = 16.8e9 bytes = 15.7 GiB: a "16 GB" Mac has 16 GiB
    assert min_ram_gb(_info(size_bytes=2_500_000_000)) == 16
    assert min_ram_gb(_info(size_bytes=6_000_000_000)) == 18, "17.8e9 bytes is 16.6 GiB: past a 16 GB Mac"
    assert min_ram_gb(MODELS["qwen36-35b-a3b"]) == 36, "the 35B-A3B at full context is a 36 GB model, not a 32 GB one"
    assert min_ram_gb(MODELS["gemma4-26b-a4b"]) == 32, "#548 does not move Gemma out of the 32 GB tier"
    assert min_ram_gb(_info(size_bytes=600_000_000_000)) == 570, "past the largest Mac, the exact number of GiB"


def test_max_context_is_what_fits_never_more_than_the_model_offers_and_zero_when_the_weights_alone_do_not():
    m = _info()
    plenty = 64 * 1024**3
    assert max_context(m, plenty) == 32768
    # 5 GB weights + 1 GB overhead + 6 GB headroom = 12 GB; 16 GB leaves 4 GB = 27126 tokens, floored to 1024s.
    assert max_context(m, 16_000_000_000) == 26624
    assert max_context(m, 12_500_000_000) == 0, "3389 tokens is not worth running"
    assert max_context(m, 11_000_000_000) == 0
    assert max_context(_info(kv_bytes_per_token=0), 16_000_000_000) == 32768, (
        "no per-token cost: the model's own window"
    )
    assert max_context(_info(kv_bytes_per_token=0), 11_000_000_000) == 0
    fixed = _info(kv_bytes_per_token=20_480, kv_fixed_bytes=209_715_200, context_window=262144)
    assert max_context(fixed, 16_000_000_000) == (16_000_000_000 - 12_209_715_200) // 20_480 // 1024 * 1024


def test_effective_context_is_the_requested_window_clamped_to_what_fits():
    from harbor_clerk.llm.models import effective_context, requested_context

    m = _info(yarn=YarnConfig(extended_context=131072, rope_scale=4.0, original_context=32768))
    assert requested_context(m, False) == 32768 and requested_context(m, True) == 131072
    assert requested_context(_info(), True) == 32768, "YaRN on, but the model has none"
    plenty = 64 * 1024**3
    assert effective_context(m, True, plenty) == 131072
    assert effective_context(m, True, 16_000_000_000) == 26624, "the YaRN window, clamped to what 16 GB fits"
    assert effective_context(m, False, 16_000_000_000) == 26624
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
    "gemma4-26b-a4b": ("gemma4", "global"),  # sliding_window_pattern: 1 = windowed, 0 = global
    "gpt-oss-20b": ("gpt-oss", "half"),  # llama.cpp alternates window and full layers for this architecture
    "qwen36-35b-a3b": ("qwen35moe", "interval"),  # full_attention_interval
    "qwen35-9b": ("qwen35", "interval"),
    "qwen35-4b": ("qwen35", "interval"),
}


# Models whose fixed cost is derived from the header rather than estimated. qwen36-35b-a3b joins with #657.
RECURRENT_STATE_DERIVED = {"qwen35-9b", "qwen35-4b"}
LLAMA_CTX_CHECKPOINTS = 32  # llama-server's default at the pin; the launcher does not pass --ctx-checkpoints


def test_the_launcher_bounds_the_prompt_cache_and_leaves_checkpoints_at_the_default_the_budget_assumes():
    """llama-server keeps two things in host RAM that the launcher used to leave at their defaults: a prompt
    cache (8 GiB) and context checkpoints (32 per slot). The cache is now bounded by the budget (#657). The
    checkpoints are not, and the Qwen3.5 entries' kv_fixed_bytes assumes the default: whoever bounds them
    brings LLAMA_CTX_CHECKPOINTS and those figures down in the same change.

    As a flag or as llama.cpp's environment form, from anywhere in the app: the shared environment is built
    in ServiceManager, not in the launcher."""
    launcher = _swift("Services/LlamaService.swift")
    assert re.search(r'"--cache-ram",\s*String\(promptCacheMiB\)', launcher), "the cache is bounded by the budget"
    assert launcher.count('"--cache-ram"') == 1, "once: llama-server takes the last value it is given"
    assert "MemoryBudget.promptCacheMiB(" in launcher and "context: contextWindow" in launcher, (
        "from the context the launcher is about to pass, not the one it asked for"
    )
    for path in sorted(SWIFT_APP.rglob("*.swift")):
        source = _swift(str(path.relative_to(SWIFT_APP)))
        assert "LLAMA_ARG_CACHE_RAM" not in source, f"{path.name}: the environment form would bypass the budget"
        if path.name != "LlamaService.swift":
            assert "--cache-ram" not in source, f"{path.name}: the cache is bounded in one place"
        for name in ("--ctx-checkpoints", "--swa-checkpoints", "LLAMA_ARG_CTX_CHECKPOINTS"):
            assert name not in source, (
                f"{path.name} now sets {name}: bring LLAMA_CTX_CHECKPOINTS and the kv_fixed_bytes that use it down to match"
            )


def test_compose_bounds_the_prompt_cache_at_something_that_holds_a_conversation():
    """The sibling site: Compose starts its own llama-server and cannot know the host's memory. At the pin a
    state larger than the limit is not cached at all, so the fixed bound has to hold the largest state any
    curated model can save at Compose's context."""
    from harbor_clerk.llm.models import MIB, PROMPT_CACHE_MAX_BYTES

    compose = (Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text()
    code = "\n".join(line.split("#")[0] for line in compose.splitlines())
    bound = int(re.search(r'"--cache-ram",\s*"(\d+)"', code).group(1)) * MIB
    context = int(re.search(r'"-c",\s*"(\d+)"', code).group(1))
    assert max(kv_bytes(m, context) for m in MODELS.values()) <= bound < PROMPT_CACHE_MAX_BYTES


def test_the_prompt_cache_gets_what_the_context_leaves():
    """Context first, cache second. Same cases as `testThePromptCacheGetsWhatTheContextLeaves` in Swift."""
    from harbor_clerk.llm.models import PROMPT_CACHE_MAX_BYTES, PROMPT_CACHE_MIN_TOKENS, prompt_cache_mib

    gib = 1024**3
    qwen8, gptoss, big, nine = MODELS["qwen3-8b"], MODELS["gpt-oss-20b"], MODELS["qwen36-35b-a3b"], MODELS["qwen35-9b"]
    assert prompt_cache_mib(qwen8, 32 * gib, 32768) == 8192, "room to spare: llama-server's own default, as before"
    assert prompt_cache_mib(qwen8, 18 * gib, 32768) == 2353, "what 32K of context leaves on an 18 GB Mac"
    # 305 MiB is left on a 16 GB Mac. At the pin a state larger than the limit is not cached at all, and a
    # 4096-token state of this model is 576 MiB: a cache in name only, so it is off.
    assert prompt_cache_mib(qwen8, 16 * gib, 32768) == 0
    # The reason the cache does not come first: reserving 512 MiB ahead of the context would take this model
    # from 27K tokens to 6K on an 18 GB Mac.
    assert max_context(gptoss, 18 * gib) == 27648 and prompt_cache_mib(gptoss, 18 * gib, 27648) == 0
    assert max_context(big, 32 * gib) == 239_616 and prompt_cache_mib(big, 32 * gib, 239_616) == 0, "the mini's 35B"
    assert prompt_cache_mib(big, 36 * gib, 262_144) == 3673
    # The fixed KV cost counts, in what is left and in what a state needs: without its 1.7 GB of recurrent
    # state and checkpoints, 18 GB would leave 1666 MiB.
    assert max_context(nine, 18 * gib) == 149_504 and prompt_cache_mib(nine, 18 * gib, 149_504) == 0
    assert prompt_cache_mib(nine, 24 * gib, 262_144) == 2632
    # And in what a state needs: with 23 GiB, 1608 MiB is left, but a 4096-token state of this model is
    # 1786 MiB, nearly all of it the fixed cost. Forgetting that would switch on a cache that holds nothing.
    assert prompt_cache_mib(nine, 23 * gib, 262_144) == 0
    assert prompt_cache_mib(qwen8, 0, 32768) == 0, "memory unknown"
    assert (8 * gib, 4096) == (PROMPT_CACHE_MAX_BYTES, PROMPT_CACHE_MIN_TOKENS)


# (context, --cache-ram in MiB) for every curated model on the Macs people have. The contexts are what they
# were before the cache was budgeted: that is the claim, and this is where it is held.
WHAT_EACH_MAC_GETS = {
    "qwen3-8b": {16: (32768, 0), 18: (32768, 2353), 24: (32768, 8192), 32: (32768, 8192), 36: (32768, 8192)},
    "qwen3-4b": {16: (32768, 2718), 18: (32768, 4766), 24: (32768, 8192), 32: (32768, 8192), 36: (32768, 8192)},
    "qwen35-9b": {16: (83968, 0), 18: (149504, 0), 24: (262144, 2632), 32: (262144, 8192), 36: (262144, 8192)},
    "qwen35-4b": {16: (173056, 0), 18: (238592, 0), 24: (262144, 5436), 32: (262144, 8192), 36: (262144, 8192)},
    "gemma4-26b-a4b": {16: (0, 0), 18: (0, 0), 24: (73728, 0), 32: (262144, 4526), 36: (262144, 8192)},
    "gpt-oss-20b": {16: (0, 0), 18: (27648, 0), 24: (128000, 3811), 32: (128000, 8192), 36: (128000, 8192)},
    "qwen36-35b-a3b": {16: (0, 0), 18: (0, 0), 24: (0, 0), 32: (239616, 0), 36: (262144, 3673)},
}


def test_the_cache_never_costs_a_model_its_context_or_its_place():
    """For every curated model on every Mac: the context is what it was before the cache was budgeted, the
    whole of it fits the machine, and a cache that is on can hold a conversation."""
    from harbor_clerk.llm.models import MIB, PROMPT_CACHE_MAX_BYTES, PROMPT_CACHE_MIN_TOKENS, prompt_cache_mib

    assert set(WHAT_EACH_MAC_GETS) == set(MODELS)
    for m in MODELS.values():
        for tier, (context, cache_mib) in WHAT_EACH_MAC_GETS[m.id].items():
            ram = tier * 1024**3
            assert max_context(m, ram) == context, f"{m.id} on {tier} GB"
            got = prompt_cache_mib(m, ram, context) if context else 0
            assert got == cache_mib, f"{m.id} on {tier} GB"
            if context:
                assert memory_bytes(m, context) + got * MIB + HOST_HEADROOM_BYTES <= ram
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
    assert [max_context(nine, g * gib) for g in (8, 16, 18, 24)] == [0, 83_968, 149_504, 262_144]
    assert [max_context(four, g * gib) for g in (8, 16, 18, 24)] == [0, 173_056, 238_592, 262_144]
    assert min_ram_gb(nine) == min_ram_gb(four) == 24, "at the full window; the launcher clamps below that"


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
    elif growing == "interval":
        per_token = layers // h["full_attention_interval"] * heads * (k + v) * 2
    else:
        pattern = h["attention.sliding_window_pattern"]
        per_token = sum(hd * (k + v) * 2 for hd, windowed in zip(heads, pattern, strict=True) if not windowed)
    assert m.kv_bytes_per_token == per_token, f"{model_id}: header says {per_token} bytes per token"
    if model_id in RECURRENT_STATE_DERIVED:
        recurrent_layers = layers - layers // h["full_attention_interval"]
        conv_channels = h["ssm.inner_size"] + 2 * h["ssm.group_count"] * h["ssm.state_size"]
        per_layer = (h["ssm.state_size"] * h["ssm.inner_size"] + (h["ssm.conv_kernel"] - 1) * conv_channels) * 4
        per_slot = recurrent_layers * per_layer
        assert m.kv_fixed_bytes == m.parallel_slots * (1 + LLAMA_CTX_CHECKPOINTS) * per_slot, (
            f"{model_id}: header says {per_slot} bytes of recurrent state per slot; the constant follows the slots"
        )
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

    monkeypatch.setattr("harbor_clerk.llm.models.system_ram_bytes", lambda: 16_000_000_000)
    assert context_budget(None, False) == 32768
    # llama-server splits -c across slots: `-c 32768 -np 2` reports 16384 per slot. A budget of the whole
    # window let a 20K-token conversation through to a 16K slot untrimmed.
    assert context_budget(_info(parallel_slots=2), False) == 26624 // 2
    # The shipped case: one slot, so Qwen3-8B's requests get the whole 32K.
    monkeypatch.setattr("harbor_clerk.llm.models.system_ram_bytes", lambda: 64 * 1024**3)
    assert context_budget(MODELS["qwen3-8b"], False) == 32768
    monkeypatch.setattr("harbor_clerk.llm.models.system_ram_bytes", lambda: 16_000_000_000)
    assert context_budget(_info(), False) == max_context(_info(), 16_000_000_000) == 26624, (
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
    per_token, fixed, windows = table("kvBytesPerToken"), table("kvFixedBytes"), table("contextWindows")
    assert per_token == {m.id: m.kv_bytes_per_token for m in MODELS.values()}
    assert fixed == {m.id: m.kv_fixed_bytes for m in MODELS.values()}
    assert windows == {m.id: m.context_window for m in MODELS.values()}
    from harbor_clerk.llm.models import PROMPT_CACHE_MAX_BYTES, PROMPT_CACHE_MIN_TOKENS

    for name, value in (
        ("runtimeOverheadBytes", RUNTIME_OVERHEAD_BYTES),
        ("hostHeadroomBytes", HOST_HEADROOM_BYTES),
        ("promptCacheMaxBytes", PROMPT_CACHE_MAX_BYTES),
        ("promptCacheMinTokens", PROMPT_CACHE_MIN_TOKENS),
    ):
        assert re.search(rf"static let {name} = {value:_}\b", swift), name

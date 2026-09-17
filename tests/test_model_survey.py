"""The model survey's rules, offline. The shapes are the Hub API's, recorded
from a real run (2026-09-17) and trimmed; the network is never touched."""

from __future__ import annotations

from datetime import date

import pytest

from scripts.model_survey import llamacpp
from scripts.model_survey.hf import summarize_gguf, summarize_model
from scripts.model_survey.screen import (
    Policy,
    excluded_fragment,
    family_of,
    flags_for,
    generation_of,
    ladder_gap_fillers,
    load_policy,
    params_class,
    rank,
    screen,
    size_matched_successors,
)

POLICY = load_policy()
PIN = {"qwen3", "qwen35", "qwen35moe", "gemma4", "gpt-oss"}
HEAD = PIN | {"qwen4exp"}

CURATED = [
    {"id": "qwen3-4b", "family": "qwen", "generation": (3,), "size_bytes": 2_500_000_000},
    {"id": "qwen3-8b", "family": "qwen", "generation": (3,), "size_bytes": 5_030_000_000},
    {"id": "gpt-oss-20b", "family": "gpt-oss", "generation": (), "size_bytes": 11_600_000_000},
    {"id": "gemma4-26b-a4b", "family": "gemma", "generation": (4,), "size_bytes": 17_000_000_000, "params_b": 26.0},
    {"id": "qwen36-35b-a3b", "family": "qwen", "generation": (3, 6), "size_bytes": 22_134_528_992},
]


def _model(repo: str, **over) -> dict:
    base = {
        "repo": repo,
        "created": "2026-08-05",
        "pipeline_tag": "image-text-to-text",
        "license": "apache-2.0",
        "likes": 100,
    }
    return {**base, **over}


def _gguf(repo: str, **over) -> dict:
    base = {
        "repo": repo,
        "architecture": "qwen35",
        "context_length": 262144,
        "tool_calls_in_template": 29,
        "quant_bytes": 16_460_000_000,
    }
    return {**base, **over}


@pytest.mark.parametrize(
    ("name", "family", "generation"),
    [
        ("Qwen/Qwen3.8-27B", "qwen", (3, 8)),
        ("unsloth/Qwen3.6-35B-A3B-GGUF", "qwen", (3, 6)),
        ("Qwen/Qwen3-8B-GGUF", "qwen", (3,)),
        ("google/gemma-4-12b-it", "gemma", (4,)),
        ("unsloth/gpt-oss-20b-GGUF", "gpt-oss", ()),  # 20b is a size, not a version
        ("mistralai/Ministral-3-8B-Instruct", "ministral", (3,)),
        ("InternScience/Agents-A1", "agents", ()),
        ("someone/unknown-model", None, ()),
    ],
)
def test_family_and_generation(name: str, family: str | None, generation: tuple) -> None:
    assert family_of(name) == family
    assert generation_of(name) == generation


def test_name_exclusions_and_flags() -> None:
    assert excluded_fragment("Qwen/Qwen3.8-27B-FP8", POLICY) == "fp8"
    assert excluded_fragment("Qwen/Qwen3-ASR-1.7B-hf", POLICY) == "asr"
    assert excluded_fragment("Qwen/Qwen-AgentWorld-35B-A3B", POLICY) in ("world", "agentworld")
    assert excluded_fragment("Qwen/Qwen3.8-27B", POLICY) is None
    assert flags_for("empero-ai/Qwen3.8-9B-Distill-GGUF", POLICY) == ["distill"]


def test_screen_accepts_the_in_range_release_without_an_upgrade_note() -> None:
    verdict = screen(
        _model("Qwen/Qwen3.8-27B"), _gguf("unsloth/Qwen3.8-27B-GGUF"), POLICY, arch_at_pin=PIN, arch_at_head=HEAD
    )
    assert verdict == {"verdict": "candidate", "reasons": [], "notes": []}


def test_screen_records_every_reason() -> None:
    verdict = screen(
        _model("Qwen/Qwen3.8-Flash-Next", license="other"),
        _gguf("unsloth/Qwen3.8-Flash-Next-GGUF", architecture="qwen4exp", quant_bytes=101_000_000_000),
        POLICY,
        arch_at_pin=PIN,
        arch_at_head=HEAD,
    )
    assert verdict["verdict"] == "screened"
    assert any("licence is other" in r for r in verdict["reasons"])
    assert any("over the 24 GB ceiling" in r for r in verdict["reasons"])


def test_screen_distinguishes_needs_upgrade_from_cannot_load() -> None:
    needs = screen(
        _model("x/y"), _gguf("x/y-GGUF", architecture="qwen4exp"), POLICY, arch_at_pin=PIN, arch_at_head=HEAD
    )
    assert needs["verdict"] == "candidate" and "needs a llama.cpp upgrade" in needs["notes"][0]
    cannot = screen(
        _model("x/y"), _gguf("x/y-GGUF", architecture="brandnew"), POLICY, arch_at_pin=PIN, arch_at_head=HEAD
    )
    assert cannot["verdict"] == "screened" and "even at head" in cannot["reasons"][0]


@pytest.mark.parametrize(
    ("gguf", "fragment"),
    [
        (None, "no GGUF"),
        (_gguf("x/y-GGUF", quant_bytes=None), "no Q4_K_M file"),
        (_gguf("x/y-GGUF", context_length=8192), "context 8192"),
        (_gguf("x/y-GGUF", tool_calls_in_template=0), "no tool calling"),
    ],
)
def test_screen_reasons(gguf: dict | None, fragment: str) -> None:
    verdict = screen(_model("x/y"), gguf, POLICY, arch_at_pin=PIN, arch_at_head=HEAD)
    assert verdict["verdict"] == "screened" and any(fragment in r for r in verdict["reasons"]), verdict


def test_rank_puts_the_successor_of_a_curated_family_first() -> None:
    """The product's tuning is per family, so a newer generation of one it
    already ships outranks a more popular stranger."""
    successor = {"model": _model("Qwen/Qwen3.8-27B", likes=15500), "gguf": _gguf("unsloth/Qwen3.8-27B-GGUF")}
    stranger = {
        "model": _model("hype/Hyped-30B", likes=90000, created="2026-09-10"),
        "gguf": _gguf("unsloth/Hyped-30B-GGUF"),
    }
    sibling = {
        "model": _model("google/gemma-4-12b-it", likes=3000),
        "gguf": _gguf("unsloth/gemma-4-12b-it-GGUF", quant_bytes=7_120_000_000),
    }
    ranked = rank([stranger, sibling, successor], CURATED, today=date(2026, 9, 17))
    assert [c["model"]["repo"] for c in ranked][0] == "Qwen/Qwen3.8-27B"
    assert "successor of a curated family" in ranked[0]["score_parts"]
    by_repo = {c["model"]["repo"]: c for c in ranked}
    assert "fills a gap in the size ladder" in by_repo["google/gemma-4-12b-it"]["score_parts"], (
        "7 GB sits in the 5 -> 11.6 GB gap"
    )
    assert "same family as a curated model" in by_repo["google/gemma-4-12b-it"]["score_parts"]


def test_summarize_gguf_picks_the_quant_and_ignores_projectors_and_drafters() -> None:
    info = {
        "id": "unsloth/Qwen3.8-27B-GGUF",
        "lastModified": "2026-08-20T00:00:00Z",
        "gguf": {"architecture": "qwen35", "context_length": 262144, "chat_template": "<tool_call> tools <tool_call>"},
        "siblings": [
            {"rfilename": "Qwen3.8-27B-UD-Q4_K_M.gguf", "size": 16_460_000_000},
            {"rfilename": "Qwen3.8-27B-Q8_0.gguf", "size": 29_000_000_000},
            {"rfilename": "mmproj-BF16.gguf", "size": 930_000_000},
            {"rfilename": "MTP/mtp-Qwen3.8-27B-Q4_K_M.gguf", "size": 2_790_000_000},
            {"rfilename": "imatrix_unsloth.gguf", "size": 10_000_000},
        ],
    }
    s = summarize_gguf(info, "Q4_K_M")
    assert s["quant_file"] == "Qwen3.8-27B-UD-Q4_K_M.gguf" and s["quant_bytes"] == 16_460_000_000
    assert s["architecture"] == "qwen35" and s["context_length"] == 262144 and s["tool_calls_in_template"] == 3


def test_summarize_gguf_sums_shards() -> None:
    info = {
        "id": "x/Big-GGUF",
        "siblings": [
            {"rfilename": "Q4_K_M/Big-Q4_K_M-00001-of-00002.gguf", "size": 40_000_000_000},
            {"rfilename": "Q4_K_M/Big-Q4_K_M-00002-of-00002.gguf", "size": 38_000_000_000},
        ],
    }
    s = summarize_gguf(info, "Q4_K_M")
    assert s["quant_bytes"] == 78_000_000_000 and s["quant_file"] == "2 shards"


def test_summarize_model_reads_licence_from_card_or_tags() -> None:
    info = {
        "id": "Qwen/Qwen3.8-27B",
        "createdAt": "2026-08-05T10:00:00Z",
        "pipeline_tag": "image-text-to-text",
        "tags": ["license:apache-2.0"],
        "safetensors": {"total": 27_781_427_952},
        "config": {"model_type": "qwen3_5"},
        "likes": 15500,
    }
    s = summarize_model(info)
    assert s["license"] == "apache-2.0" and s["created"] == "2026-08-05" and s["params"] == 27_781_427_952


def test_pinned_tag_is_read_from_the_build_script() -> None:
    assert llamacpp.pinned_tag('LLAMA_CPP_TAG="${LLAMA_CPP_TAG:-b9018}"\n') == "b9018"
    assert llamacpp.pinned_tag(), "the real build script must still carry a default tag"
    with pytest.raises(ValueError, match="LLAMA_CPP_TAG"):
        llamacpp.pinned_tag("no tag here")


def test_policy_file_is_coherent() -> None:
    assert isinstance(POLICY, Policy) and "Qwen" in POLICY.orgs and POLICY.ceiling_gb == 24
    assert "unsloth" in POLICY.gguf_publishers and "apache-2.0" in POLICY.permissive_licenses


def test_hub_treats_401_403_404_as_absent() -> None:
    """The Hub answers 401 for a missing repo when unauthenticated; probing for
    a GGUF build must read that as "no such repo", not as a failure."""
    import httpx

    from scripts.model_survey.hf import Hub

    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response({"gone": 404, "private": 401, "gated": 403}.get(name, 200), json={"id": "x/ok"})

    hub = Hub(httpx.Client(transport=httpx.MockTransport(handler)), pause_s=0)
    assert hub.model("x/gone") is None and hub.model("x/private") is None and hub.model("x/gated") is None
    assert hub.model("x/ok") == {"id": "x/ok"}


def test_hub_backs_off_on_429_and_honours_retry_after() -> None:
    """Three surveys in ten minutes were rate-limited; an unattended loop must
    wait and retry rather than die half-way through."""
    import httpx

    from scripts.model_survey.hf import Hub

    calls, sleeps = [], []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        if len(calls) == 2:
            return httpx.Response(429)
        return httpx.Response(200, json={"id": "x/ok"})

    hub = Hub(httpx.Client(transport=httpx.MockTransport(handler)), pause_s=0, sleep=sleeps.append)
    assert hub.model("x/ok") == {"id": "x/ok"}
    assert len(calls) == 3 and 7.0 in sleeps and 10.0 in sleeps, sleeps


def test_hub_gives_up_after_max_tries() -> None:
    import httpx

    from scripts.model_survey.hf import Hub

    hub = Hub(
        httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(429))),
        pause_s=0,
        max_tries=3,
        sleep=lambda s: None,
    )
    with pytest.raises(httpx.HTTPStatusError):
        hub.model("x/ok")
    assert hub.requests == 3


def test_hub_cache_serves_repeat_requests_without_the_network(tmp_path) -> None:
    import httpx

    from scripts.model_survey.hf import Hub

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"id": "x/ok"}) if request.url.path.endswith("/ok") else httpx.Response(401)

    def new_hub() -> Hub:
        return Hub(
            httpx.Client(transport=httpx.MockTransport(handler)), pause_s=0, cache_dir=tmp_path, sleep=lambda s: None
        )

    assert new_hub().model("x/ok") == {"id": "x/ok"} and new_hub().model("x/missing") is None
    assert new_hub().model("x/ok") == {"id": "x/ok"} and new_hub().model("x/missing") is None
    assert len(calls) == 2, "the second pass, including the cached absence, must not touch the network"


@pytest.mark.parametrize(
    ("name", "size"),
    [
        ("Qwen/Qwen3.5-9B", 9.0),
        ("unsloth/Qwen3.6-35B-A3B-GGUF", 35.0),
        ("openai/gpt-oss-20b", 20.0),
        ("google/gemma-4-26B-A4B-it", 26.0),
        ("InternScience/Agents-A1", None),
    ],
)
def test_params_class_reads_total_not_active_parameters(name: str, size: float | None) -> None:
    assert params_class(name) == size


def test_size_matched_successors_ignore_the_window_and_the_wrong_size_class() -> None:
    """A tier's replacement may predate the survey window, and a 27B is not a
    successor to a 4B."""
    curated = {"repo": "Qwen/Qwen3-4B-GGUF", "family": "qwen", "generation": (3,)}
    rows = [
        {"id": "Qwen/Qwen3.8-27B", "createdAt": "2026-08-05T00:00:00Z", "pipeline_tag": "image-text-to-text"},
        {"id": "Qwen/Qwen3.5-4B", "createdAt": "2026-03-02T00:00:00Z", "pipeline_tag": "image-text-to-text"},
        {"id": "Qwen/Qwen3.5-4B-Base", "createdAt": "2026-03-02T00:00:00Z", "pipeline_tag": "text-generation"},
        {"id": "Qwen/Qwen3.5-4B-FP8", "createdAt": "2026-03-02T00:00:00Z", "pipeline_tag": "image-text-to-text"},
        {"id": "Qwen/Qwen3.5-2B", "createdAt": "2026-03-02T00:00:00Z", "pipeline_tag": "image-text-to-text"},
        {"id": "Qwen/Qwen3-4B-Instruct-2507", "createdAt": "2025-08-06T00:00:00Z", "pipeline_tag": "text-generation"},
        {
            "id": "Qwen/Qwen3-ASR-1.7B",
            "createdAt": "2026-06-26T00:00:00Z",
            "pipeline_tag": "automatic-speech-recognition",
        },
    ]
    found = [f["repo"] for f in size_matched_successors(curated, rows, POLICY)]
    assert found == ["Qwen/Qwen3.5-4B"], found


def test_exclusions_cover_gui_agents_and_derivative_uncensored_builds() -> None:
    for name in (
        "tencent/UI-Mate-27B",
        "OBLITERATUS/Qwen3.8-27B-OBLITERATED",
        "huihui-ai/Huihui-Qwen3.8-27B-abliterated",
        "LiquidAI/LFM2.5-2.6B-ONNX",
    ):
        assert excluded_fragment(name, POLICY), name
    assert excluded_fragment("ibm-granite/granite-4.2-8b", POLICY) is None


def _rows(*ids: str, tag: str = "image-text-to-text") -> list[dict]:
    return [{"id": i, "createdAt": "2026-04-02T00:00:00Z", "pipeline_tag": tag} for i in ids]


def test_ladder_gap_fillers_find_the_sibling_the_window_and_successor_search_miss() -> None:
    """Gemma 4 12B is the same generation as the curated Gemma and older than
    any survey window, and it sits in the 5 to 11.6 GB gap. Google files it
    under `any-to-any`, which the policy has to admit."""
    rows = _rows("google/gemma-4-12B-it", "google/gemma-4-12B", tag="any-to-any") + _rows(
        "google/gemma-4-26B-A4B-it",  # the curated model itself
        "google/gemma-4-12B-it-qat-q4_0-unquantized",
        "google/gemma-4-12B-it-assistant",  # a speculative-decoding drafter
        "google/gemma-3-12b-it",  # a generation the registry has moved past
    )
    found = ladder_gap_fillers(CURATED, rows, "gemma", POLICY)
    assert [f["repo"] for f in found] == ["google/gemma-4-12B-it"], found
    assert found[0]["gap_gb"] == [5.0, 11.6]
    assert "any-to-any" in POLICY.pipeline_tags
    assert ladder_gap_fillers(CURATED, rows, "granite", POLICY) == [], (
        "a family that is not curated has no ladder to fill"
    )


def test_ladder_gap_fillers_accept_a_newer_generation_above_their_own_family() -> None:
    """The 5 to 11.6 GB gap sits above qwen3-8b. A Qwen3.5 14B is newer than
    that neighbour although older than the newest Qwen carried, so it counts;
    Qwen3-14B is the neighbour's own generation and does not."""
    rows = _rows("Qwen/Qwen3.5-14B", "Qwen/Qwen3-14B", tag="text-generation")
    found = ladder_gap_fillers(CURATED, rows, "qwen", POLICY)
    assert [f["repo"] for f in found] == ["Qwen/Qwen3.5-14B"], found


def test_hub_follows_the_redirect_for_a_repo_name_in_the_wrong_case() -> None:
    """`unsloth/gemma-4-12B-it-GGUF` lives at `gemma-4-12b-it-GGUF`; the Hub
    answers the first spelling with a 307, which once ended the whole run."""
    import httpx

    from scripts.model_survey.hf import Hub

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/unsloth/gemma-4-12B-it-GGUF"):
            return httpx.Response(307, headers={"Location": "/api/models/unsloth/gemma-4-12b-it-GGUF"})
        return httpx.Response(200, json={"id": "unsloth/gemma-4-12b-it-GGUF"})

    hub = Hub(httpx.Client(transport=httpx.MockTransport(handler)), pause_s=0)
    assert hub.model("unsloth/gemma-4-12B-it-GGUF") == {"id": "unsloth/gemma-4-12b-it-GGUF"}


def test_main_refuses_to_overwrite_before_making_any_request(tmp_path, monkeypatch, capsys) -> None:
    """One file per run. The refusal comes first, so it costs the Hub nothing."""
    from scripts.model_survey import __main__ as cli

    def no_survey(*a, **k):
        raise AssertionError("the survey ran although the report already exists")

    monkeypatch.setattr(cli, "survey", no_survey)
    existing = tmp_path / "report.md"
    existing.write_text("kept")
    assert cli.main(["--out", str(existing)]) == 2
    assert existing.read_text() == "kept"
    assert "refusing to overwrite" in capsys.readouterr().err


def test_report_header_names_the_commit_it_ran_from() -> None:
    """docs/reports/README.md asks every loop report for its commit."""
    from datetime import datetime

    from scripts.model_survey.__main__ import commit_id, render

    facts = {
        "since": "2026-07-22",
        "llamacpp": {
            "pin": "b1",
            "architectures_at_pin": 1,
            "architectures_at_head": 2,
            "latest": {"tag": "v1", "published": "2026-09-14"},
            "only_at_head": [],
        },
        "candidates": [],
        "screened": [],
        "curated": [],
        "gap_fillers": [],
        "outside_watchlist": [],
    }
    text = render(facts, POLICY, "r1", datetime(2026, 9, 17), commit="abc1234-dirty")
    assert "commit `abc1234-dirty`" in text.split("## Reading")[0]
    assert commit_id() != "", "falls back to 'unknown', never to an empty string"

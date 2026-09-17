"""The model survey's rules, offline. The shapes are the Hub API's, recorded
from a real run (2026-09-17) and trimmed; the network is never touched."""

from __future__ import annotations

import json
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
    only_waiting_for_gguf,
    params_class,
    rank,
    score,
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
            # Each of these names the quant and is shorter than the model's file,
            # so only the exclusion list keeps it from being chosen.
            {"rfilename": "mmproj-Q4_K_M.gguf", "size": 930_000_000},
            {"rfilename": "MTP/mtp-Q4_K_M.gguf", "size": 2_790_000_000},
            {"rfilename": "imatrix-Q4_K_M.gguf", "size": 10_000_000},
            {"rfilename": "draft-Q4_K_M.gguf", "size": 500_000_000},
            {"rfilename": "eagle3-Q4_K_M.gguf", "size": 500_000_000},
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


# --- a small Hub and GitHub, served through a mock transport ------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_ambient_token(monkeypatch):
    monkeypatch.delenv("HF_TOKEN", raising=False)


TOOLS_TEMPLATE = "{% if tools %}<tool_call>{% endif %}"


class World:
    """Listings, repos, redirects and the two GitHub endpoints the survey reads."""

    def __init__(self) -> None:
        self.listings: dict[str, list[dict]] = {}
        self.repos: dict[str, dict] = {}
        self.trending: list[dict] = []
        self.redirects: dict[str, str] = {}
        self.arch_pin = {f"arch{i}" for i in range(60)} | {"qwen3", "qwen35"}
        self.arch_release = self.arch_pin | {"relarch"}
        self.arch_head = self.arch_release | {"newarch"}
        self.requests: list = []

    def release(self, repo: str, created: str, *, likes: int = 100, licence="apache-2.0", tag="text-generation"):
        """A release in its vendor's listing and readable on its own. `tag=None` is a repo with no task."""
        org = repo.split("/")[0]
        self.listings.setdefault(org, []).append(
            {"id": repo, "createdAt": f"{created}T00:00:00Z", "pipeline_tag": tag, "likes": likes}
        )
        self.listings[org].sort(key=lambda r: r["createdAt"], reverse=True)
        self.repos[repo] = {
            "id": repo,
            "createdAt": f"{created}T00:00:00Z",
            "pipeline_tag": tag,
            "likes": likes,
            "cardData": {"license": licence},
        }

    def gguf(self, repo: str, files: dict[str, int], *, arch="qwen35", ctx=262144, template=TOOLS_TEMPLATE):
        self.repos[repo] = {
            "id": repo,
            "lastModified": "2026-08-20T00:00:00Z",
            "gguf": {"architecture": arch, "context_length": ctx, "chat_template": template},
            "siblings": [{"rfilename": n, "size": s} for n, s in files.items()],
        }

    def handler(self, request):
        import httpx

        self.requests.append(request)
        host, path = request.url.host, request.url.path
        if host == "raw.githubusercontent.com":
            names = self.arch_head if "/master/" in path else self.arch_release if "/v9.9.9/" in path else self.arch_pin
            return httpx.Response(
                200, text="\n".join(f'{{ LLM_ARCH_X{i}, "{n}" }},' for i, n in enumerate(sorted(names)))
            )
        if host == "api.github.com":
            return httpx.Response(200, json={"tag_name": "v9.9.9", "published_at": "2026-09-14T00:00:00Z"})
        if path == "/api/models":
            params = request.url.params
            return httpx.Response(
                200, json=self.listings.get(params["author"], []) if "author" in params else self.trending
            )
        repo = path.removeprefix("/api/models/")
        if repo in self.redirects:
            return httpx.Response(307, headers={"Location": f"/api/models/{self.redirects[repo]}"})
        return httpx.Response(200, json=self.repos[repo]) if repo in self.repos else httpx.Response(401)

    def clients(self, **hub_kwargs):
        import httpx

        from scripts.model_survey.hf import Hub

        client = httpx.Client(transport=httpx.MockTransport(self.handler))
        return Hub(client, pause_s=0, sleep=lambda s: None, **{"token": None, **hub_kwargs}), client


def _policy(**over):
    import dataclasses

    return dataclasses.replace(POLICY, **{"orgs": ["Qwen", "acme"], "family_orgs": {"qwen": "Qwen"}, **over})


def _curated(**over) -> dict:
    return {
        "id": "qwen3-8b",
        "name": "Qwen3 8B",
        "repo": "Qwen/Qwen3-8B-GGUF",
        "filename": "Qwen3-8B-Q4_K_M.gguf",
        "size_bytes": 5_030_000_000,
        "context_window": 32768,
        "family": "qwen",
        "generation": (3,),
        "params_b": 8.0,
        **over,
    }


SINCE, TODAY = date(2026, 7, 22), date(2026, 9, 17)


def _run(world: World, *, curated=None, policy=None, **kw) -> dict:
    from scripts.model_survey.__main__ import survey

    hub, client = world.clients()
    if curated is None:
        world.gguf("Qwen/Qwen3-8B-GGUF", {"Qwen3-8B-Q4_K_M.gguf": 5_030_000_000}, arch="qwen3", ctx=40960)
        curated = [_curated()]
    return survey(policy or _policy(), SINCE, hub, client, curated=curated, today=TODAY, **kw)


def test_survey_examines_the_window_ranks_what_passes_and_names_what_it_left_out() -> None:
    w = World()
    w.release("Qwen/Qwen3.8-27B", "2026-08-05", likes=15500)
    w.gguf("unsloth/Qwen3.8-27B-GGUF", {"Qwen3.8-27B-UD-Q4_K_M.gguf": 16_464_440_224})
    w.release("Qwen/Qwen2.5-7B-Instruct", "2024-09-01")  # before the window
    w.gguf("Qwen/Qwen2.5-7B-Instruct-GGUF", {"qwen2.5-7b-instruct-q4_k_m.gguf": 4_000_000_000})
    w.release("Qwen/Qwen-AgentWorld-35B-A3B", "2026-08-01")  # a world model, by name
    w.release("Qwen/Qwen3.8-Speech-2B", "2026-08-02", tag="automatic-speech-recognition")
    w.release("acme/Closed-7B", "2026-08-03", licence="other")
    w.gguf("acme/Closed-7B-GGUF", {"Closed-7B-Q4_K_M.gguf": 4_000_000_000})
    w.listings["acme"].append(
        {"id": "acme/Other-7B-GGUF", "createdAt": "2026-08-02T00:00:00Z", "pipeline_tag": "text-generation"}
    )
    w.release("acme/Gated-7B", "2026-08-01")
    del w.repos["acme/Gated-7B"]  # listed, but the Hub will not let us read it

    facts = _run(w)
    assert [c["model"]["repo"] for c in facts["candidates"]] == ["Qwen/Qwen3.8-27B"]
    assert facts["candidates"][0]["score_parts"]["successor of a curated family"] == 40.0
    assert [(c["model"]["repo"], c["reasons"]) for c in facts["screened"]] == [("acme/Closed-7B", ["licence is other"])]
    assert {(e["repo"], e["why"]) for e in facts["excluded"]} == {
        ("Qwen/Qwen-AgentWorld-35B-A3B", "name contains 'world'"),
        ("Qwen/Qwen3.8-Speech-2B", "task is automatic-speech-recognition"),
        ("acme/Gated-7B", "not readable (gated or removed)"),
    }
    assert [(x["org"], x["listed"], x["in_window"], x["examined"]) for x in facts["vendors"]] == [
        ("Qwen", 4, 3, 1),
        ("acme", 3, 2, 1),
    ], "a GGUF repo in a listing is a build, not a release: not counted, not examined, not listed as left out"
    everything = json.dumps(facts, default=str)
    assert "Qwen2.5-7B-Instruct" not in everything, "a release older than the window was examined"


def test_find_gguf_prefers_the_vendor_then_publishers_in_policy_order_and_knows_bartowskis_naming() -> None:
    from scripts.model_survey.__main__ import find_gguf

    q4 = {"m-Q4_K_M.gguf": 4_000_000_000}
    w = World()
    for repo in (
        "acme/A-7B-GGUF",
        "unsloth/A-7B-GGUF",
        "unsloth/B-7B-GGUF",
        "bartowski/B-7B-GGUF",
        "bartowski/acme_C-7B-GGUF",
    ):
        w.gguf(repo, q4)
    w.gguf("unsloth/D-7B-GGUF", {"m-Q8_0.gguf": 8_000_000_000})  # exists, but not the policy's quant
    w.gguf("bartowski/D-7B-GGUF", q4)
    hub, _ = w.clients()
    assert POLICY.gguf_publishers.index("unsloth") < POLICY.gguf_publishers.index("bartowski")
    assert find_gguf(hub, "acme/A-7B", POLICY)["repo"] == "acme/A-7B-GGUF"
    assert find_gguf(hub, "acme/B-7B", POLICY)["repo"] == "unsloth/B-7B-GGUF"
    assert find_gguf(hub, "acme/C-7B", POLICY)["repo"] == "bartowski/acme_C-7B-GGUF"
    assert find_gguf(hub, "acme/D-7B", POLICY)["repo"] == "bartowski/D-7B-GGUF", "a repo without the quant must not win"
    assert find_gguf(hub, "acme/E-7B", POLICY) is None


def test_the_audit_says_so_when_a_curated_repo_or_its_registered_file_is_gone() -> None:
    """The worst drift the audit can find must not read as a clean bill of health."""
    w = World()
    w.gguf("Qwen/Renamed-GGUF", {"Qwen3-8B-UD-Q4_K_M.gguf": 5_030_000_000}, arch="qwen3", ctx=40960)
    w.gguf("Qwen/Fine-GGUF", {"Qwen3-8B-Q4_K_M.gguf": 5_030_000_000}, arch="qwen3", ctx=40960)
    curated = [
        _curated(id="gone", repo="Qwen/Gone-GGUF"),
        _curated(id="renamed", repo="Qwen/Renamed-GGUF"),
        _curated(id="fine", repo="Qwen/Fine-GGUF"),
    ]
    findings = {c["id"]: c["findings"] for c in _run(w, curated=curated)["curated"]}
    assert findings["gone"] == ["the GGUF repo is not reachable on the Hub (deleted, renamed, private or gated)"]
    assert findings["renamed"] == ["registry file `Qwen3-8B-Q4_K_M.gguf` is not in the repo"]
    assert findings["fine"] == []


def test_the_audit_measures_the_registered_file_and_reports_context_and_architecture_drift() -> None:
    w = World()
    # The shortest Q4_K_M name matches the registry's size; the registered file does not.
    w.gguf(
        "Qwen/Drift-GGUF",
        {"Qwen3-8B-Q4_K_M.gguf": 5_030_000_000, "Qwen3-8B-UD-Q4_K_M.gguf": 6_500_000_000},
        arch="qwen3",
        ctx=40960,
    )
    w.gguf("Qwen/Short-GGUF", {"Qwen3-8B-Q4_K_M.gguf": 5_030_000_000}, arch="qwen3", ctx=16384)
    w.gguf("Qwen/Unused-GGUF", {"Qwen3-8B-Q4_K_M.gguf": 5_030_000_000}, arch="qwen3", ctx=262144)
    w.gguf("Qwen/Newarch-GGUF", {"Qwen3-8B-Q4_K_M.gguf": 5_030_000_000}, arch="newarch", ctx=40960)
    curated = [
        _curated(id="drift", repo="Qwen/Drift-GGUF", filename="Qwen3-8B-UD-Q4_K_M.gguf"),
        _curated(id="short", repo="Qwen/Short-GGUF"),
        _curated(id="unused", repo="Qwen/Unused-GGUF"),
        _curated(id="newarch", repo="Qwen/Newarch-GGUF"),
    ]
    findings = {c["id"]: c["findings"] for c in _run(w, curated=curated)["curated"]}
    assert findings["drift"] == ["registry size 5.03 GB but the file is 6.50 GB"]
    assert findings["short"] == ["registry context_window 32768 EXCEEDS the GGUF's 16384"]
    assert findings["unused"] == ["registry context_window 32768 leaves the GGUF's 262144 unused"]
    assert findings["newarch"] == ["architecture 'newarch' is not in the pinned llama.cpp"]


def test_successors_are_screened_like_candidates() -> None:
    w = World()
    w.release("Qwen/Qwen3.5-9B", "2026-02-27", licence="other")
    w.gguf("unsloth/Qwen3.5-9B-GGUF", {"Qwen3.5-9B-Q4_K_M.gguf": 5_700_000_000}, arch="newarch")
    facts = _run(w)
    successor = facts["curated"][0]["successors"][0]
    assert successor["repo"] == "Qwen/Qwen3.5-9B" and successor["reasons"] == ["licence is other"]
    assert successor["notes"] == [
        "architecture 'newarch' needs a llama.cpp upgrade past the pin (only master has it, not the stable release v9.9.9)"
    ]
    from datetime import datetime

    from scripts.model_survey.__main__ import render

    table = render(facts, _policy(), "r1", datetime(2026, 9, 17)).split("## What replaces each tier")[1]
    row = next(line for line in table.splitlines() if "Qwen/Qwen3.5-9B" in line)
    assert "| licence is other; ⚠ architecture 'newarch' needs" in row, row


def test_the_per_vendor_cap_keeps_the_most_liked_and_names_the_rest() -> None:
    from scripts.model_survey import __main__ as cli

    w = World()
    for i in range(1, cli.PER_ORG + 3):
        w.release(f"acme/Thing{i}-7B", "2026-08-10", likes=i)
    facts = _run(w)
    examined = {c["model"]["repo"] for c in facts["screened"]}
    assert len(examined) == cli.PER_ORG and "acme/Thing1-7B" not in examined and "acme/Thing14-7B" in examined
    assert [r["repo"] for r in facts["not_examined"]] == ["acme/Thing2-7B", "acme/Thing1-7B"]
    from datetime import datetime

    report = cli.render(facts, _policy(), "r1", datetime(2026, 9, 17))
    pending = cli.pending_from_report(report)
    assert pending[-2:] == ["acme/Thing2-7B", "acme/Thing1-7B"], "over the cap is carried to the next run"
    assert len(pending) == cli.PER_ORG + 2, "the twelve examined have no GGUF yet, so they wait too"


def test_a_listing_that_does_not_reach_back_to_the_window_is_reported(monkeypatch) -> None:
    from scripts.model_survey import __main__ as cli

    monkeypatch.setattr(cli, "LISTING_LIMIT", 3)
    w = World()
    for i in range(3):
        w.release(f"acme/Thing{i}-7B", "2026-08-10")
    w.release("Qwen/Qwen3.8-27B", "2026-08-05")
    assert _run(w)["coverage"] == ["`acme` has more than 3 repos since 2026-07-22; the oldest were not listed"]


def test_a_release_waiting_for_a_gguf_is_carried_to_the_next_survey_and_examined_again() -> None:
    from datetime import datetime

    from scripts.model_survey.__main__ import CARRIED_NOTE, pending_from_report, render

    w = World()
    w.release("acme/Soon-7B", "2026-09-10")  # in the window, nobody has built it yet
    w.release("acme/Late-7B", "2026-07-01")  # older than the window, on the previous waiting list, built since
    w.gguf("unsloth/Late-7B-GGUF", {"Late-7B-Q4_K_M.gguf": 4_000_000_000})
    w.release("acme/Never-7B", "2026-03-01")  # carried, still nothing, and past the waiting period
    w.release("acme/Closed-7B", "2026-09-01", licence="other")  # no GGUF either, but that is not all that is wrong

    facts = _run(w, recheck=["acme/Late-7B", "acme/Never-7B", "acme/Soon-7B"])
    late = next(c for c in facts["candidates"] if c["model"]["repo"] == "acme/Late-7B")
    assert late["notes"] == [CARRIED_NOTE]
    assert [x["repo"] for x in facts["waiting"]] == ["acme/Soon-7B"]
    assert sum(c["model"]["repo"] == "acme/Soon-7B" for c in facts["screened"]) == 1, "examined twice"

    report = render(facts, _policy(), "r1", datetime(2026, 9, 17), previous="2026-08-01-model-survey-r0.md")
    assert pending_from_report(report) == ["acme/Soon-7B"]
    assert "3 release(s) carried from its lists, 2 examined again" in report.split("## Reading")[0]
    assert facts["rechecked"] == ["acme/Late-7B", "acme/Never-7B"], "Soon-7B was already in the window"
    assert pending_from_report("# no such section") == []


def test_trending_outside_the_watchlist_leaves_out_watched_vendors_and_gguf_publishers() -> None:
    w = World()
    row = {"createdAt": "2026-09-01T00:00:00Z", "likes": 9}
    w.trending = [
        {"id": "Qwen/Qwen3.8-27B", **row},
        {"id": "unsloth/Something-7B", **row},
        {"id": "Edge0/Edge0-35B-A3B-preview", **row},
        {"id": "Edge0/Old-7B", "createdAt": "2025-01-01T00:00:00Z", "likes": 9},
    ]
    assert [r["repo"] for r in _run(w)["outside_watchlist"]] == ["Edge0/Edge0-35B-A3B-preview"]


def test_a_redirect_to_another_owner_is_not_the_trusted_publisher() -> None:
    from scripts.model_survey.__main__ import find_gguf

    w = World()
    w.gguf("randomuser/Foo-7B-GGUF", {"Foo-7B-Q4_K_M.gguf": 4_000_000_000})
    w.redirects["unsloth/Foo-7B-GGUF"] = "randomuser/Foo-7B-GGUF"
    hub, _ = w.clients()
    assert hub.model("unsloth/Foo-7B-GGUF") is None
    assert find_gguf(hub, "acme/Foo-7B", POLICY) is None


def test_a_rejected_token_fails_the_run_and_is_never_cached(tmp_path) -> None:
    """Every answer is 401 with a bad token. Read as "absent", that is an empty
    survey that exits 0, cached for six hours."""
    import httpx

    from scripts.model_survey.hf import Hub, HubError

    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401)))
    hub = Hub(client, token="expired", pause_s=0, cache_dir=tmp_path)
    with pytest.raises(HubError, match="HF_TOKEN"):
        hub.model("Qwen/Qwen3-8B-GGUF")
    assert list(tmp_path.iterdir()) == []


def test_a_listing_that_fails_is_an_error_not_an_empty_vendor() -> None:
    import httpx

    from scripts.model_survey.hf import Hub, HubError

    hub = Hub(httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401))), token=None, pause_s=0)
    with pytest.raises(HubError):
        hub.org_models("Qwen")
    with pytest.raises(HubError):
        hub.trending("text-generation")
    assert hub.model("Qwen/whatever") is None, "a probe for one repo may still read 401 as absent"


def test_the_token_is_sent_as_a_bearer_header_and_only_when_there_is_one(monkeypatch) -> None:
    w = World()
    w.gguf("x/ok", {})
    for kwargs, expected in (({"token": "t0ken"}, "Bearer t0ken"), ({"token": None}, None)):
        hub, _ = w.clients(**kwargs)
        hub.model("x/ok")
        assert w.requests[-1].headers.get("Authorization") == expected
    monkeypatch.setenv("HF_TOKEN", "from-env")
    hub, _ = w.clients(token="env")
    hub.model("x/ok")
    assert w.requests[-1].headers.get("Authorization") == "Bearer from-env" and hub.authenticated


def test_hub_retries_5xx_and_transport_errors_caps_the_backoff_and_does_not_sleep_after_the_last_try() -> None:
    import httpx

    from scripts.model_survey.hf import Hub

    answers = [httpx.Response(503), httpx.ReadTimeout("slow"), httpx.Response(200, json={"id": "x/ok"})]

    def flaky(request):
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a

    sleeps: list[float] = []
    hub = Hub(httpx.Client(transport=httpx.MockTransport(flaky)), token=None, pause_s=0, sleep=sleeps.append)
    assert hub.model("x/ok") == {"id": "x/ok"} and hub.requests == 3

    def down(request):
        raise httpx.ConnectError("down")

    sleeps.clear()
    hub = Hub(
        httpx.Client(transport=httpx.MockTransport(down)), token=None, pause_s=0, max_tries=8, sleep=sleeps.append
    )
    with pytest.raises(httpx.ConnectError):
        hub.model("x/ok")
    waits = [s for s in sleeps if s]
    assert waits == [5.0, 10.0, 20.0, 40.0, 80.0, 160.0, 300.0], "seven waits for eight tries, capped at five minutes"
    assert sum(waits) > 300, "the waits must outlast the Hub's 300 s rate-limit window"

    impatient = [httpx.Response(429, headers={"Retry-After": "86400"}), httpx.Response(200, json={"id": "x/ok"})]
    sleeps.clear()
    hub = Hub(
        httpx.Client(transport=httpx.MockTransport(lambda r: impatient.pop(0))),
        token=None,
        pause_s=0,
        sleep=sleeps.append,
    )
    assert hub.model("x/ok") == {"id": "x/ok"} and max(sleeps) == 600.0, "a day-long Retry-After must not park the run"


def test_hub_cache_expires(tmp_path) -> None:
    w = World()
    w.gguf("x/ok", {})
    hub, _ = w.clients(cache_dir=tmp_path, cache_ttl_s=0)
    hub.model("x/ok")
    hub.model("x/ok")
    assert hub.requests == 2
    hub, _ = w.clients(cache_dir=tmp_path, cache_ttl_s=3600)
    hub.model("x/ok")
    assert hub.requests == 0


def test_summarize_gguf_chooses_one_build_of_the_quant_never_the_sum() -> None:
    from scripts.model_survey.hf import file_size

    def info(*files: tuple[str, int]) -> dict:
        return {"id": "unsloth/Big-GGUF", "siblings": [{"rfilename": n, "size": s} for n, s in files]}

    shard = "Big-{}Q4_K_M-0000{}-of-00002.gguf"
    two_builds = info(
        *((f"Q4_K_M/{shard.format('', i)}", 39_000_000_000) for i in (1, 2)),
        *((f"UD-Q4_K_M/{shard.format('UD-', i)}", 40_000_000_000) for i in (1, 2)),
    )
    s = summarize_gguf(two_builds, "Q4_K_M")
    assert (s["quant_file"], s["quant_bytes"]) == ("2 shards", 78_000_000_000)

    single_beside_shards = info(
        ("Big-Q4_K_M-a-single-file-with-the-longer-name.gguf", 7), *((shard.format("", i), 39) for i in (1, 2))
    )
    s = summarize_gguf(single_beside_shards, "Q4_K_M")
    assert (s["quant_file"], s["quant_bytes"]) == ("Big-Q4_K_M-a-single-file-with-the-longer-name.gguf", 7)
    assert file_size(single_beside_shards, "Big-Q4_K_M-a-single-file-with-the-longer-name.gguf") == 7
    assert file_size(single_beside_shards, "absent.gguf") is None


def test_a_list_valued_licence_does_not_end_the_run() -> None:
    m = summarize_model({"id": "acme/Dual-7B", "cardData": {"license": ["apache-2.0", "mit"]}})
    assert m["license"] == "apache-2.0"
    verdict = screen(
        {**_model("acme/Dual-7B"), "license": ["MIT"]},
        _gguf("unsloth/X-GGUF"),
        POLICY,
        arch_at_pin={"qwen35"},
        arch_at_head={"qwen35"},
    )
    assert "licence is ['mit']" in verdict["reasons"]


def test_parse_architectures_and_the_guard_against_a_reshaped_table() -> None:
    import httpx

    sample = """
    static const std::map<llm_arch, const char *> LLM_ARCH_NAMES = {
        { LLM_ARCH_QWEN3,       "qwen3"       },
        { LLM_ARCH_QWEN35MOE,   "qwen35moe"   },
        {LLM_ARCH_GPT_OSS,"gpt-oss"},
        { LLM_ARCH_UNKNOWN,     "(unknown)"   },
    };"""
    assert llamacpp.parse_architectures(sample) == {"qwen3", "qwen35moe", "gpt-oss"}
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=sample)))
    with pytest.raises(ValueError, match="reshaped"):
        llamacpp.architectures_at("master", client)


def test_latest_release_reads_the_tag_and_the_day() -> None:
    import httpx

    body = {"tag_name": "v0.4.1", "published_at": "2026-09-14T08:00:00Z"}
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body)))
    assert llamacpp.latest_release(client) == {"tag": "v0.4.1", "published": "2026-09-14"}


def test_screen_refuses_a_task_that_is_not_chat() -> None:
    verdict = screen(
        _model("acme/Ears-7B", pipeline_tag="automatic-speech-recognition"),
        _gguf("unsloth/X-GGUF"),
        POLICY,
        arch_at_pin={"qwen35"},
        arch_at_head={"qwen35"},
    )
    assert verdict["reasons"] == ["not a chat model on the Hub (pipeline: automatic-speech-recognition)"]


def test_only_the_familys_vendor_publishes_a_successor() -> None:
    today = date(2026, 9, 17)
    vendor = {"model": _model("Qwen/Qwen4-14B"), "gguf": _gguf("unsloth/X-GGUF")}
    remix = {"model": _model("arcee-ai/Arcee-Qwen4-Remix-14B"), "gguf": _gguf("unsloth/X-GGUF")}
    assert "successor of a curated family" in score(vendor, CURATED, today, POLICY)
    parts = score(remix, CURATED, today, POLICY)
    assert "successor of a curated family" not in parts and parts["derived from a curated family"] == 15.0


def test_the_family_is_the_earliest_stem_in_the_name() -> None:
    assert family_of("Qwen/Qwen4-Agents-8B") == "qwen"
    assert family_of("InternScience/Agents-A1") == "agents"
    assert family_of("openai/gpt-oss-20b") == "gpt-oss"


def test_recency_fades_over_ninety_days() -> None:
    def recency(created: str) -> float:
        return score(
            {"model": _model("acme/New-7B", created=created), "gguf": _gguf("unsloth/X-GGUF")},
            CURATED,
            date(2026, 9, 17),
        )["recency"]

    assert recency("2026-09-17") == 10.0 and recency("2026-08-05") == 5.2 and recency("2026-01-01") == 0.0


def test_successors_come_newest_generation_first_then_closest_in_size() -> None:
    rows = _rows("Qwen/Qwen3.5-9B", "Qwen/Qwen3.8-12B", "Qwen/Qwen3.8-9B", tag="text-generation")
    found = size_matched_successors(_curated(), rows, POLICY)
    assert [f["repo"] for f in found] == ["Qwen/Qwen3.8-9B", "Qwen/Qwen3.8-12B", "Qwen/Qwen3.5-9B"]


def test_only_waiting_for_gguf_means_nothing_else_is_wrong() -> None:
    assert only_waiting_for_gguf(["no GGUF from the vendor or a trusted publisher"], POLICY)
    assert only_waiting_for_gguf(["no Q4_K_M file in unsloth/X-GGUF"], POLICY)
    assert not only_waiting_for_gguf(["licence is other", "no GGUF from the vendor or a trusted publisher"], POLICY)
    assert not only_waiting_for_gguf([], POLICY)


def test_every_curated_family_names_its_vendor() -> None:
    """The successor bonus is restricted to the family's vendor, so a curated
    family without one in the policy would hand the bonus to anyone."""
    from scripts.model_survey.__main__ import curated_models

    assert {c["family"] for c in curated_models()} <= set(POLICY.family_orgs)


def test_the_previous_report_sets_the_window(tmp_path) -> None:
    from scripts.model_survey.__main__ import previous_report, report_date

    assert previous_report(tmp_path) is None and previous_report(tmp_path / "absent") is None
    for name in ("2026-09-17-model-survey-a.md", "2026-10-01-model-survey-b.md", "2026-10-05-acceptance-x.md"):
        (tmp_path / name).write_text("")
    newest = previous_report(tmp_path)
    assert newest.name == "2026-10-01-model-survey-b.md" and report_date(newest) == date(2026, 10, 1)
    assert report_date(tmp_path / "notes.md") is None


def test_commit_id_falls_back_to_unknown(monkeypatch) -> None:
    import subprocess

    from scripts.model_survey import __main__ as cli

    def no_git(*a, **k):
        raise FileNotFoundError("git")

    monkeypatch.setattr(cli.subprocess, "run", no_git)
    assert cli.commit_id() == "unknown"
    monkeypatch.setattr(
        cli.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 128, stdout="", stderr="not a repo")
    )
    assert cli.commit_id() == "unknown"


def test_the_report_shows_what_was_found_and_what_was_left_out() -> None:
    from datetime import datetime

    from scripts.model_survey.__main__ import render

    w = World()
    w.release("Qwen/Qwen3.8-27B", "2026-08-05", likes=15500)
    w.gguf("unsloth/Qwen3.8-27B-GGUF", {"Qwen3.8-27B-UD-Q4_K_M.gguf": 16_464_440_224})
    w.release("Qwen/Qwen-AgentWorld-35B-A3B", "2026-08-01")
    w.release("acme/Closed-7B", "2026-08-03", licence="other", likes=50)
    w.release("acme/Quiet-7B", "2026-08-03", licence="other", likes=2)
    facts = _run(w)
    facts["coverage"] = ["`acme` has more than 500 repos"]
    text = render(facts, _policy(), "r1", datetime(2026, 9, 17), commit="abc1234-dirty", authenticated=True)
    header = text.split("## Reading")[0]
    assert "commit `abc1234-dirty`" in header and "with a token" in header and "⚠ `acme` has more than 500" in header
    assert "3 releases examined · 1 left out by name or task" in header
    assert "| 1 | `Qwen/Qwen3.8-27B` | 2026-08-05 | apache-2.0 |" in text and "| 16.5 | 262144 | qwen35 |" in text
    assert "| `acme/Closed-7B` | 2026-08-03 | 50 | licence is other; no GGUF" in text
    assert "And 1 with under 10 likes: `acme/Quiet-7B` (licence is other" in text
    assert "- `Qwen`: `Qwen-AgentWorld-35B-A3B` (name contains 'world')" in text
    assert (
        "| `qwen3-8b` | `Qwen/Qwen3-8B-GGUF` | 32768 | 40960 | 2026-08-20 | a newer generation of the family exists, but not in this size class |"
        in text
    )


# --- review round 2 -----------------------------------------------------------------------------------------------


def test_a_release_with_no_task_tag_is_examined() -> None:
    """Mistral publishes without a pipeline tag. Read as "not chat", that
    removed the vendor most likely to matter for a French corpus."""
    w = World()
    w.release("acme/Ministral-4-8B-Instruct", "2026-08-20", tag=None)
    w.gguf("unsloth/Ministral-4-8B-Instruct-GGUF", {"Ministral-4-8B-Instruct-Q4_K_M.gguf": 5_000_000_000})
    facts = _run(w)
    assert [c["model"]["repo"] for c in facts["candidates"]] == ["acme/Ministral-4-8B-Instruct"]
    assert facts["excluded"] == []
    rows = _rows("Qwen/Qwen3.5-9B", tag=None)
    assert [f["repo"] for f in size_matched_successors(_curated(), rows, POLICY)] == ["Qwen/Qwen3.5-9B"]


def test_a_vendor_that_lists_nothing_is_reported() -> None:
    """The Hub answers 200 [] for an org that was renamed (THUDM is now zai-org) or never existed."""
    w = World()
    w.release("Qwen/Qwen3.8-27B", "2026-08-05")
    facts = _run(w)
    assert facts["coverage"] == ["`acme` lists no repos at all: renamed or mistyped in the watchlist?"]
    assert {"org": "acme", "listed": 0, "in_window": 0, "examined": 0}.items() <= facts["vendors"][1].items()


def test_a_newly_watched_vendor_gets_a_first_run_window() -> None:
    from datetime import datetime

    from scripts.model_survey.__main__ import render, vendors_from_report

    w = World()
    w.release("acme/Earlier-7B", "2026-07-01")  # before the window, inside a first run's 90 days
    w.release("Qwen/Qwen3.5-Earlier-7B", "2026-07-01")  # same age, but Qwen was watched last time
    facts = _run(w, known_orgs={"qwen"})
    assert [c["model"]["repo"] for c in facts["screened"]] == ["acme/Earlier-7B"]
    acme = facts["vendors"][1]
    assert acme["first_run"] and acme["since"] == "2026-06-19"
    report = render(facts, _policy(), "r1", datetime(2026, 9, 17))
    assert "- `acme` · 1 listed · 1 since 2026-06-19 (first run for this vendor) · 1 examined" in report
    assert vendors_from_report(report) == {"qwen", "acme"}
    assert vendors_from_report("# a report from before this section existed") is None
    assert _run(w)["screened"] == [], "with no previous report there is nothing to be new against"


def test_a_pretrained_base_is_not_the_successor_when_its_instruct_sibling_exists() -> None:
    rows = _rows("google/gemma-5-27B", "google/gemma-5-31B", "google/gemma-5-27B-it", "google/gemma-5-31B-it")
    gemma = next(c for c in CURATED if c["family"] == "gemma")
    found = size_matched_successors({**gemma, "repo": "bartowski/google_gemma-4-26B-A4B-it-GGUF"}, rows, POLICY)
    assert [f["repo"] for f in found] == ["google/gemma-5-27B-it", "google/gemma-5-31B-it"]


def test_the_upgrade_note_says_whether_a_stable_release_carries_the_architecture() -> None:
    def note(arch: str) -> list[str]:
        verdict = screen(
            _model("acme/New-7B"),
            _gguf("unsloth/New-7B-GGUF", architecture=arch),
            POLICY,
            arch_at_pin={"qwen35"},
            arch_at_release={"qwen35", "relarch"},
            arch_at_head={"qwen35", "relarch", "newarch"},
            release_tag="v0.4.1",
        )
        assert verdict["verdict"] == "candidate"
        return verdict["notes"]

    assert note("qwen35") == []
    assert note("relarch") == ["architecture 'relarch' needs a llama.cpp upgrade past the pin (v0.4.1 has it)"]
    assert note("newarch") == [
        "architecture 'newarch' needs a llama.cpp upgrade past the pin (only master has it, not the stable release v0.4.1)"
    ]


def test_the_survey_separates_the_stable_release_from_master() -> None:
    lc = _run(World())["llamacpp"]
    assert lc["in_release_not_pin"] == ["relarch"] and lc["only_on_master"] == ["newarch"]
    assert lc["latest"] == {"tag": "v9.9.9", "published": "2026-09-14"}


def test_gap_fillers_reach_the_report_screened_and_without_what_is_already_curated() -> None:
    from datetime import datetime

    from scripts.model_survey.__main__ import render

    w = World()
    for repo in ("Qwen/Qwen3-12B", "Qwen/Qwen3-14B"):
        w.release(repo, "2025-04-27", licence="other" if "12B" in repo else "apache-2.0")
    w.gguf("unsloth/Qwen3-12B-GGUF", {"m-Q4_K_M.gguf": 8_000_000_000}, arch="qwen3")
    w.gguf("Qwen/Qwen3-14B-GGUF", {"Qwen3-14B-Q4_K_M.gguf": 15_000_000_000}, arch="qwen3")
    w.gguf("Qwen/Qwen3-8B-GGUF", {"Qwen3-8B-Q4_K_M.gguf": 5_030_000_000}, arch="qwen3", ctx=40960)
    curated = [
        _curated(),
        # Curated under a size its name does not state, so only its GGUF repo says it is already carried.
        _curated(
            id="odd",
            repo="Qwen/Qwen3-14B-GGUF",
            filename="Qwen3-14B-Q4_K_M.gguf",
            size_bytes=15_000_000_000,
            params_b=None,
        ),
        _curated(id="big", size_bytes=20_000_000_000, params_b=None),
    ]
    facts = _run(w, curated=curated)
    assert [(f["repo"], f["reasons"]) for f in facts["gap_fillers"]] == [("Qwen/Qwen3-12B", ["licence is other"])]
    table = (
        render(facts, _policy(), "r1", datetime(2026, 9, 17)).split("## Gaps in the size ladder")[1].split("\n## ")[0]
    )
    assert "| `Qwen/Qwen3-12B` | 2025-04-27 | other | 5 to 15 GB | `unsloth/Qwen3-12B-GGUF` |" in table
    assert table.rstrip().endswith("| licence is other |")


def test_the_family_vendor_is_searched_even_when_it_is_not_watched_and_two_successors_are_kept() -> None:
    from scripts.model_survey.__main__ import SUCCESSORS_PER_TIER

    w = World()
    for repo in ("Qwen/Qwen3.5-9B", "Qwen/Qwen3.6-9B", "Qwen/Qwen3.8-9B"):
        w.release(repo, "2026-02-27")
    facts = _run(w, policy=_policy(orgs=["acme"]))
    assert SUCCESSORS_PER_TIER == 2
    assert [s["repo"] for s in facts["curated"][0]["successors"]] == ["Qwen/Qwen3.8-9B", "Qwen/Qwen3.6-9B"]
    assert [v["org"] for v in facts["vendors"]] == ["acme"]


def test_a_truncated_family_catalogue_is_reported(monkeypatch) -> None:
    from scripts.model_survey import __main__ as cli

    monkeypatch.setattr(cli, "LISTING_LIMIT", 2)
    w = World()
    w.release("Qwen/Qwen3-Old-7B", "2024-02-13")
    w.release("Qwen/Qwen2-Older-7B", "2024-01-01")
    assert _run(w, policy=_policy(orgs=["Qwen"]))["coverage"] == [
        "the successor and gap search saw only the newest 2 repos of `Qwen`, back to 2024-01-01"
    ]


def test_hub_closes_the_client_it_made_and_leaves_a_borrowed_one_open() -> None:
    import httpx

    from scripts.model_survey.hf import Hub

    with Hub(token=None) as own:
        made = own._http
    assert made.is_closed
    borrowed = httpx.Client()
    with Hub(borrowed, token=None):
        pass
    assert not borrowed.is_closed
    borrowed.close()


def _previous(directory, name: str, since: str = "2026-07-22"):
    from datetime import datetime

    from scripts.model_survey.__main__ import render

    w = World()
    w.release("acme/Soon-7B", "2026-09-10")
    text = render(_run(w), _policy(), "r0", datetime(2026, 9, 17)).replace("since 2026-07-22", f"since {since}", 1)
    path = directory / name
    path.write_text(text)
    return path


def test_the_plan_opens_the_window_at_the_previous_report_and_repeats_it_for_a_rerun(tmp_path) -> None:
    from scripts.model_survey.__main__ import plan

    today = date(2026, 9, 24)
    assert plan(None, None, False, today) == {
        "since": date(2026, 6, 26),
        "recheck": [],
        "known_orgs": None,
        "label": None,
    }

    last_week = _previous(tmp_path, "2026-09-17-model-survey-r0.md")
    run = plan(last_week, None, False, today)
    assert run["since"] == date(2026, 9, 17) and run["recheck"] == ["acme/Soon-7B"]
    assert run["known_orgs"] == {"qwen", "acme"} and run["label"] == "`2026-09-17-model-survey-r0.md`"

    # After a policy fix: the same window again, not the week since.
    redo = plan(last_week, None, True, today)
    assert redo["since"] == date(2026, 7, 22) and "whose window this run repeats" in redo["label"]

    # A report dated today is the one this run replaces. Opening the window at it would survey nothing.
    same_day = plan(_previous(tmp_path, "2026-09-24-model-survey-r1.md", since="2026-09-17"), None, False, today)
    assert same_day["since"] == date(2026, 9, 17) and "repeats" in same_day["label"]

    assert plan(last_week, date(2026, 1, 1), False, today)["since"] == date(2026, 1, 1), "--since overrides"


def test_main_hands_the_plan_to_the_survey_and_writes_the_report_and_the_facts(tmp_path, monkeypatch) -> None:
    from scripts.model_survey import __main__ as cli

    reports = tmp_path / "reports"
    reports.mkdir()
    _previous(reports, "2026-09-10-model-survey-r0.md")
    monkeypatch.setattr(cli, "REPORTS", reports)
    monkeypatch.setattr(cli, "utc_today", lambda: date(2026, 9, 17))
    handed: dict = {}
    real_survey, w = cli.survey, World()

    def fake_survey(policy, since, hub, client, **kw):
        handed.update(since=since, **kw)
        fake_hub, fake_client = w.clients()
        w.gguf("Qwen/Qwen3-8B-GGUF", {"Qwen3-8B-Q4_K_M.gguf": 5_030_000_000}, arch="qwen3", ctx=40960)
        return real_survey(_policy(), since, fake_hub, fake_client, curated=[_curated()], today=kw["today"])

    monkeypatch.setattr(cli, "survey", fake_survey)
    facts_path = tmp_path / "facts.json"
    assert cli.main(["--out", str(reports), "--run-id", "r1", "--json", str(facts_path)]) == 0
    assert handed == {
        "since": date(2026, 9, 10),
        "recheck": ["acme/Soon-7B"],
        "known_orgs": {"qwen", "acme"},
        "today": date(2026, 9, 17),
    }
    written = reports / "2026-09-17-model-survey-r1.md"
    assert "Previous survey:** `2026-09-10-model-survey-r0.md`" in written.read_text()
    assert json.loads(facts_path.read_text())["since"] == "2026-09-10"

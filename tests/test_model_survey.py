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
    may_pass_later,
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
    assert excluded_fragment("tencent/Penguin-VL-8B", POLICY) is None, "`gui` once matched Pen-gui-n"
    assert (
        excluded_fragment("acme/GUIAgent-7B", POLICY) is None and excluded_fragment("acme/Holo-GUI", POLICY) == "-gui"
    )
    assert excluded_fragment("acme/gui-owl-7b", POLICY), "an earlier fragment (`ui-`) may be the one that matches"


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
        self.good_tokens = {"t", "t0ken", "from-env"}

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
        if path == "/api/whoami-v2":
            good = request.headers.get("Authorization") in {f"Bearer {t}" for t in self.good_tokens}
            return httpx.Response(200 if good else 401, json={"name": "someone"})
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
        return Hub(client, **{"token": None, "pause_s": 0, "sleep": lambda s: None, **hub_kwargs}), client


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
        ("acme/Gated-7B", "not readable this run (renamed, private or gated); carried to the next"),
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
    pending = cli.pending(cli.state_from_report(report))
    over_cap = cli.state_from_report(report)["outputs"]["over_cap"]
    assert set(over_cap) == {"acme/Thing2-7B", "acme/Thing1-7B"}, "over the cap is carried to the next run"
    assert pending["acme/Thing1-7B"] == "2026-08-10", "with the day it was created, so it can be aged out"
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

    from scripts.model_survey.__main__ import CARRIED_NOTE, pending, render, state_from_report

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
    assert pending(state_from_report(report)) == {"acme/Soon-7B": "2026-09-10"}
    assert "3 release(s) carried into this run, 3 of them examined" in report.split("## Reading")[0]
    assert sorted(facts["rechecked"]) == ["acme/Late-7B", "acme/Never-7B", "acme/Soon-7B"]
    soon = next(c for c in facts["screened"] if c["model"]["repo"] == "acme/Soon-7B")
    assert soon["notes"] == [CARRIED_NOTE], "the window reached it first; it is a carried release all the same"
    assert state_from_report("# a report with no state block") is None


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


def test_may_pass_later_means_nothing_else_is_wrong() -> None:
    assert may_pass_later(["no GGUF from the vendor or a trusted publisher"], POLICY)
    assert may_pass_later(["no Q4_K_M file in unsloth/X-GGUF"], POLICY)
    assert not may_pass_later(["licence is other", "no GGUF from the vendor or a trusted publisher"], POLICY)
    assert not may_pass_later([], POLICY)
    # llama.cpp gains architectures and publishers re-upload templates and metadata; GLM-5.3-Flash was lost this way.
    assert may_pass_later(
        ["no Q4_K_M file in unsloth/GLM-5.3-Flash-GGUF", "llama.cpp cannot load architecture 'glm5next', even at head"],
        POLICY,
    )
    assert may_pass_later(["chat template has no tool calling"], POLICY)
    assert may_pass_later(["context 8192 is under 32768"], POLICY)
    assert not may_pass_later(["Q4_K_M is 119.6 GB, over the 24 GB ceiling"], POLICY)
    assert not may_pass_later(["not a chat model on the Hub (pipeline: robotics)"], POLICY)


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

    from scripts.model_survey.__main__ import render, state_from_report

    w = World()
    w.release("acme/Earlier-7B", "2026-07-01")  # before the window, inside a first run's 90 days
    w.release("Qwen/Qwen3.5-Earlier-7B", "2026-07-01")  # same age, but Qwen was watched last time
    facts = _run(w, known_orgs={"qwen"})
    assert [c["model"]["repo"] for c in facts["screened"]] == ["acme/Earlier-7B"]
    acme = facts["vendors"][1]
    assert acme["first_run"] and acme["since"] == "2026-06-19"
    report = render(facts, _policy(), "r1", datetime(2026, 9, 17))
    assert "- `acme` · 1 listed · 1 since 2026-06-19 (first run for this vendor) · 1 examined" in report
    state = state_from_report(report)
    assert state["outputs"]["vendors"] == ["Qwen", "acme"] and state["inputs"]["known_orgs"] == ["qwen"]
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


def test_the_family_vendor_is_searched_even_when_it_is_not_a_watched_org() -> None:
    w = World()
    w.release("Qwen/Qwen3.5-9B", "2026-02-27")
    facts = _run(w, policy=_policy(orgs=["acme"]))
    assert [s["repo"] for s in facts["curated"][0]["successors"]] == ["Qwen/Qwen3.5-9B"]
    assert [x["org"] for x in facts["vendors"]] == ["acme"]


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


def _report(directory, name: str, facts: dict, *, generated: str = "2026-09-17T09:00:00+00:00"):
    from datetime import datetime

    from scripts.model_survey.__main__ import render

    path = directory / name
    path.write_text(render(facts, _policy(), "r0", datetime.fromisoformat(generated)))
    return path


def _soon_world() -> World:
    w = World()
    w.release("acme/Soon-7B", "2026-09-10")
    return w


def test_replacing_a_report_finds_again_what_it_found_through_a_carried_release_or_a_new_vendor() -> None:
    """Run B ranked a release that had been waiting for a GGUF, and another
    from a vendor in its first-run window. A re-run of B after a policy fix
    returned no candidates, B was withdrawn, and neither was surveyed again."""
    from datetime import datetime

    from scripts.model_survey.__main__ import render, state_from_report

    w = World()
    w.release("acme/Late-7B", "2026-07-01")  # older than B's window; on A's waiting list; built since
    w.gguf("unsloth/Late-7B-GGUF", {"Late-7B-Q4_K_M.gguf": 4_000_000_000})
    w.release("newco/Hist-7B", "2026-07-05")  # older than B's window; newco was not watched before B
    w.gguf("unsloth/Hist-7B-GGUF", {"Hist-7B-Q4_K_M.gguf": 4_000_000_000})
    policy = _policy(orgs=["Qwen", "acme", "newco"])
    b = _run(w, policy=policy, recheck=["acme/Late-7B"], known_orgs={"qwen", "acme"})
    found = sorted(c["model"]["repo"] for c in b["candidates"])
    assert found == ["acme/Late-7B", "newco/Hist-7B"]

    state = state_from_report(render(b, policy, "b", datetime(2026, 9, 17)))
    c = _run(w, policy=policy, recheck=state["inputs"]["carried"], known_orgs=set(state["inputs"]["known_orgs"]))
    assert sorted(x["model"]["repo"] for x in c["candidates"]) == found


def test_a_previous_report_without_readable_state_stops_the_run_unless_a_window_is_given(tmp_path, capsys) -> None:
    from scripts.model_survey import __main__ as cli

    old = tmp_path / "2026-09-10-model-survey-r0.md"
    old.write_text("# Model survey\n\n## Waiting for a GGUF\n\n- `acme/Soon-7B` · 2026-09-01\n")
    with pytest.raises(cli.PlanError, match="no state block"):
        cli.plan(old, None, False, date(2026, 9, 17))
    run = cli.plan(old, date(2026, 8, 1), False, date(2026, 9, 17))
    assert run["recheck"] == {} and "could not be read" in run["warnings"][0]
    assert run["known_orgs"] is None, "no vendor counts as new: an empty set would give every vendor a first-run window"

    future = tmp_path / "2026-09-11-model-survey-r1.md"
    future.write_text('## State\n\n```json\n{"format": 99}\n```\n')
    assert cli.state_from_report(future.read_text()) is None, "a format this tool does not know is not guessed at"
    assert cli.main(["--out", str(tmp_path / "out.md"), "--previous", str(old)]) == 2
    assert "no state block" in capsys.readouterr().err and not (tmp_path / "out.md").exists()


def test_only_the_state_block_is_parsed_so_a_trending_repo_is_never_carried() -> None:
    from datetime import datetime

    from scripts.model_survey.__main__ import pending, render, state_from_report

    w = _soon_world()
    w.trending = [{"id": "Edge0/Edge0-35B-A3B-preview", "createdAt": "2026-09-08T00:00:00Z", "likes": 3284}]
    report = render(_run(w), _policy(), "r1", datetime(2026, 9, 17))
    assert "- `Edge0/Edge0-35B-A3B-preview`" in report.split("## Trending outside the watchlist")[1]
    assert pending(state_from_report(report)) == {"acme/Soon-7B": "2026-09-10"}
    own = state_from_report(report)
    fake = json.dumps({**own, "outputs": {**own["outputs"], "waiting": {"evil/Repo": "2026-09-01"}}})
    quoted = f"## State\n\n```json\n{fake}\n```\n\n_Written"
    reading = report.replace("_Written by whoever ran the loop", quoted)
    assert reading != report
    assert pending(state_from_report(reading)) == {"acme/Soon-7B": "2026-09-10"}, (
        "the last state block is the report's own"
    )


def test_the_newest_report_is_the_last_generated_not_the_last_by_name(tmp_path) -> None:
    from scripts.model_survey.__main__ import previous_report

    assert previous_report(tmp_path) is None and previous_report(tmp_path / "absent") is None
    facts = _run(_soon_world())
    _report(tmp_path, "2026-09-17-model-survey-ix-3.md", facts, generated="2026-09-17T09:00:00+00:00")
    _report(tmp_path, "2026-09-17-model-survey-ix-10.md", facts, generated="2026-09-17T15:00:00+00:00")
    _report(tmp_path, "2026-09-10-model-survey-zz.md", facts, generated="2026-09-10T23:00:00+00:00")
    (tmp_path / "2026-09-18-acceptance-x.md").write_text("")
    assert previous_report(tmp_path).name == "2026-09-17-model-survey-ix-10.md"


def test_a_carried_release_the_policy_now_excludes_is_left_out_once_and_not_carried_again() -> None:
    w = World()
    w.release("acme/Thing-Probe-7B", "2026-07-01")  # carried from before `probe` was an exclusion
    w.release("acme/Thing-Probe-Checkpoint-7B", "2026-08-01")  # carried, and also in the window
    facts = _run(w, recheck=["acme/Thing-Probe-7B", "acme/Thing-Probe-Checkpoint-7B"])
    assert [(e["repo"], e["why"]) for e in facts["excluded"]] == [
        ("acme/Thing-Probe-Checkpoint-7B", "name contains 'probe'"),
        ("acme/Thing-Probe-7B", "name contains 'probe'"),
    ]
    assert facts["screened"] == [] and facts["waiting"] == [] and facts["rechecked"] == []


def test_an_over_cap_release_examined_because_it_was_carried_is_not_reported_as_not_examined() -> None:
    from scripts.model_survey import __main__ as cli

    w = World()
    for i in range(1, cli.PER_ORG + 3):
        w.release(f"acme/Thing{i}-7B", "2026-08-10", likes=i)
    facts = _run(w, recheck=["acme/Thing1-7B"])
    assert [r["repo"] for r in facts["not_examined"]] == ["acme/Thing2-7B"]
    assert len(facts["screened"]) == cli.PER_ORG + 1 and facts["rechecked"] == ["acme/Thing1-7B"]


def test_find_gguf_passes_over_a_vendor_build_that_would_fail_the_screen() -> None:
    from scripts.model_survey.__main__ import find_gguf

    q4 = {"m-Q4_K_M.gguf": 4_000_000_000}
    w = World()
    w.gguf("acme/Short-7B-GGUF", q4, ctx=8192)
    w.gguf("unsloth/Short-7B-GGUF", q4)
    w.gguf("acme/Mute-7B-GGUF", q4, template="{{ messages }}")
    w.gguf("bartowski/Mute-7B-GGUF", q4)
    w.gguf("acme/Only-7B-GGUF", q4, ctx=8192)
    hub, _ = w.clients()
    assert find_gguf(hub, "acme/Short-7B", POLICY)["repo"] == "unsloth/Short-7B-GGUF"
    assert find_gguf(hub, "acme/Mute-7B", POLICY)["repo"] == "bartowski/Mute-7B-GGUF"
    assert find_gguf(hub, "acme/Only-7B", POLICY)["repo"] == "acme/Only-7B-GGUF", (
        "the only build is still reported, and screened"
    )


def test_a_newer_generation_from_another_org_is_not_the_familys_newer_generation() -> None:
    w = World()
    w.release("acme/Acme-Qwen4-Remix-30B", "2026-08-20")
    w.gguf("unsloth/Acme-Qwen4-Remix-30B-GGUF", {"m-Q4_K_M.gguf": 17_000_000_000})
    facts = _run(w)
    assert [c["model"]["repo"] for c in facts["candidates"]] == ["acme/Acme-Qwen4-Remix-30B"]
    assert facts["curated"][0]["findings"] == []
    w.release("Qwen/Qwen4-30B", "2026-08-21")
    w.gguf("unsloth/Qwen4-30B-GGUF", {"m-Q4_K_M.gguf": 17_000_000_000})
    assert _run(w)["curated"][0]["findings"] == ["a newer generation of the family exists, but not in this size class"]


def test_the_window_includes_the_day_it_opens_and_a_first_run_window_never_shortens_it() -> None:
    w = World()
    w.release("acme/Boundary-7B", "2026-07-22")  # published on the day the previous report was written
    w.release("acme/DayBefore-7B", "2026-07-21")
    assert [c["model"]["repo"] for c in _run(w)["screened"]] == ["acme/Boundary-7B"]

    from scripts.model_survey.__main__ import survey

    old = World()
    old.release("acme/Spring-7B", "2026-03-01")
    old.gguf("Qwen/Qwen3-8B-GGUF", {"Qwen3-8B-Q4_K_M.gguf": 5_030_000_000}, arch="qwen3", ctx=40960)
    hub, client = old.clients()
    facts = survey(_policy(), date(2026, 1, 1), hub, client, curated=[_curated()], known_orgs={"qwen"}, today=TODAY)
    assert [c["model"]["repo"] for c in facts["screened"]] == ["acme/Spring-7B"], "a window older than 90 days stays"


def test_the_cache_keys_on_the_query_not_only_the_path(tmp_path) -> None:
    w = World()
    w.release("Qwen/Qwen3.8-27B", "2026-08-05")
    w.release("acme/Thing-7B", "2026-08-05")
    hub, _ = w.clients(cache_dir=tmp_path)
    assert [r["id"] for r in hub.org_models("Qwen")] == ["Qwen/Qwen3.8-27B"]
    assert [r["id"] for r in hub.org_models("acme")] == ["acme/Thing-7B"], "every vendor got the first vendor's listing"


def test_the_cache_is_private_written_whole_and_survives_a_torn_file(tmp_path) -> None:
    import stat

    w = World()
    w.gguf("x/ok", {})
    cache = tmp_path / "cache"
    hub, _ = w.clients(cache_dir=cache)
    assert stat.S_IMODE(cache.stat().st_mode) == 0o700
    hub.model("x/ok")
    (entry,) = list(cache.iterdir())  # no partial file is left behind
    entry.write_text('{"id": "x/o')  # a run killed mid-write, before writes were atomic
    assert hub.model("x/ok")["id"] == "x/ok" and hub.requests == 2
    assert json.loads(entry.read_text())["id"] == "x/ok"


def test_the_default_pause_keeps_a_run_under_the_hubs_rate_limit() -> None:
    import inspect

    from scripts.model_survey.hf import Hub

    pause = inspect.signature(Hub).parameters["pause_s"].default
    assert 500 * pause >= 300, "the Hub allows 500 requests per 300 s"


def test_a_cache_write_that_fails_leaves_no_entry_behind(tmp_path, monkeypatch) -> None:
    from scripts.model_survey import hf

    w = World()
    w.gguf("x/ok", {})
    hub, _ = w.clients(cache_dir=tmp_path)

    def killed(src, dst):
        raise OSError("killed before the rename")

    monkeypatch.setattr(hf.os, "replace", killed)
    with pytest.raises(OSError, match="killed"):
        hub.model("x/ok")
    assert list(tmp_path.iterdir()) == [], "no torn entry to serve for six hours, and no partial file left lying"


def test_the_plans_warning_reaches_the_report(tmp_path, monkeypatch) -> None:
    from scripts.model_survey import __main__ as cli

    old = tmp_path / "2026-09-10-model-survey-r0.md"
    old.write_text("# a report from a tool that wrote no state\n")
    w = World()
    real_survey = cli.survey

    def fake_survey(policy, since, hub, client, **kw):
        fake_hub, fake_client = w.clients()
        w.gguf("Qwen/Qwen3-8B-GGUF", {"Qwen3-8B-Q4_K_M.gguf": 5_030_000_000}, arch="qwen3", ctx=40960)
        return real_survey(_policy(), since, fake_hub, fake_client, curated=[_curated()], today=kw["today"])

    monkeypatch.setattr(cli, "survey", fake_survey)
    out = tmp_path / "out.md"
    assert cli.main(["--out", str(out), "--previous", str(old), "--since", "2026-08-01"]) == 0
    header = out.read_text().split("## Reading")[0]
    assert "⚠ the state of `2026-09-10-model-survey-r0.md` could not be read" in header


# --- review round 4 -----------------------------------------------------------------------------------------------


def test_every_successor_is_screened_so_one_that_passes_is_never_hidden_behind_those_that_fail() -> None:
    """The newest successors fail the screen and the oldest passes. Cut to a
    few before screening, the audit said none passes."""
    from scripts.model_survey.__main__ import SUCCESSORS_EXAMINED

    w = World()
    w.release("Qwen/Qwen3.5-9B", "2026-02-27")
    w.gguf("unsloth/Qwen3.5-9B-GGUF", {"Qwen3.5-9B-Q4_K_M.gguf": 5_700_000_000})
    for i in range(6):
        w.release(f"Qwen/Qwen3.{6 + i}-9B", "2026-08-05", licence="other")
    tier = _run(w, policy=_policy(orgs=["acme"]))["curated"][0]
    assert len(tier["successors"]) == 7 and tier["successors"][0]["repo"] == "Qwen/Qwen3.5-9B"
    assert tier["findings"] == ["size-matched successor: Qwen/Qwen3.5-9B"]

    for i in range(SUCCESSORS_EXAMINED):
        w.release(f"Qwen/Qwen4.{i}-9B", "2026-09-01", licence="other")
    facts = _run(w, policy=_policy(orgs=["acme"]))
    assert len(facts["curated"][0]["successors"]) == SUCCESSORS_EXAMINED
    assert (
        f"`qwen3-8b` has {7 + SUCCESSORS_EXAMINED} size-matched successors; only the first {SUCCESSORS_EXAMINED} were screened"
        in facts["coverage"]
    )


def test_gap_fillers_past_the_number_screened_are_reported_and_those_that_pass_come_first() -> None:
    from scripts.model_survey.__main__ import GAP_FILLERS_EXAMINED

    w = World()
    for size in range(11, 32):
        w.release(f"Qwen/Qwen3-{size}B", "2025-04-27", licence="apache-2.0" if size == 19 else "other")
    w.gguf("unsloth/Qwen3-19B-GGUF", {"m-Q4_K_M.gguf": 11_000_000_000}, arch="qwen3")
    curated = [_curated(), _curated(id="big", size_bytes=20_000_000_000, params_b=None)]
    w.gguf("Qwen/Qwen3-8B-GGUF", {"Qwen3-8B-Q4_K_M.gguf": 5_030_000_000}, arch="qwen3", ctx=40960)
    facts = _run(w, curated=curated)
    assert len(facts["gap_fillers"]) == GAP_FILLERS_EXAMINED and facts["gap_fillers"][0]["repo"] == "Qwen/Qwen3-19B"
    assert f"`qwen` has 21 gap fillers; only the first {GAP_FILLERS_EXAMINED} were screened" in facts["coverage"]


def test_a_repo_published_after_it_was_created_is_found_by_the_overlap_and_nothing_is_examined_twice() -> None:
    """Gemma 3 was created eleven days before it was announced. A window that
    opens at the previous report misses such a repo for good."""
    from scripts.model_survey.__main__ import survey

    w = World()
    w.release("acme/Seen-7B", "2026-09-01")  # the previous run examined it
    w.release("acme/Late-7B", "2026-09-02")  # created before the previous report, private until after it
    w.release("acme/New-7B", "2026-09-12")
    w.release("acme/Old-Probe-7B", "2026-09-03")  # the previous report listed it as left out
    w.release("acme/Late-Probe-7B", "2026-09-03")  # the same day, but private then: no report has listed it
    w.release("acme/New-Probe-7B", "2026-09-12")
    seen = {"acme/Seen-7B": "2026-09-01", "acme/Ancient-7B": "2026-01-01"}
    left_out = {"acme/Old-Probe-7B": "2026-09-03", "acme/Ancient-Probe-7B": "2026-01-01"}
    hub, client = w.clients()
    w.gguf("Qwen/Qwen3-8B-GGUF", {"Qwen3-8B-Q4_K_M.gguf": 5_030_000_000}, arch="qwen3", ctx=40960)
    facts = survey(
        _policy(), date(2026, 8, 11), hub, client, curated=[_curated()], seen=seen, left_out=left_out, today=TODAY
    )
    assert sorted(c["model"]["repo"] for c in facts["screened"]) == ["acme/Late-7B", "acme/New-7B"]
    assert [e["repo"] for e in facts["excluded"]] == ["acme/New-Probe-7B", "acme/Late-Probe-7B"]
    assert facts["overlap_excluded"] == 1, (
        "what an earlier report listed is counted; that it was listed is a record, not a date"
    )
    acme = facts["vendors"][1]
    assert (acme["in_window"], acme["already_examined"], acme["examined"]) == (6, 1, 2)
    assert facts["examined_log"] == {
        "acme/seen-7b": "2026-09-01",
        "acme/late-7b": "2026-09-02",
        "acme/new-7b": "2026-09-12",
    }
    assert facts["left_out_log"] == {
        "acme/old-probe-7b": "2026-09-03",
        "acme/late-probe-7b": "2026-09-03",
        "acme/new-probe-7b": "2026-09-12",
    }, "the ancient entry is past any overlap and is dropped"


def test_a_release_blocked_on_llama_cpp_is_carried_like_one_waiting_for_a_gguf() -> None:
    w = World()
    w.release("acme/Future-7B", "2026-08-25", likes=2417)
    w.gguf("unsloth/Future-7B-GGUF", {"Future-7B-Q4_K_M.gguf": 4_000_000_000}, arch="glm5next")
    facts = _run(w)
    assert facts["screened"][0]["reasons"] == ["llama.cpp cannot load architecture 'glm5next', even at head"]
    assert [x["repo"] for x in facts["waiting"]] == ["acme/Future-7B"]


def test_the_plan_follows_a_report_with_an_overlap_and_repeats_the_one_it_replaces(tmp_path) -> None:
    from scripts.model_survey.__main__ import plan

    today = date(2026, 9, 24)
    first = plan(None, None, False, today)
    assert (first["since"], first["recheck"], first["known_orgs"], first["seen"]) == (date(2026, 6, 26), {}, None, {})
    assert first["left_out"] == {} and first["first_run_since"] == date(2026, 6, 26) and first["label"] is None
    assert first["since_source"] == "90 days, a first run's"
    assert plan(None, date(2026, 7, 22), False, today)["since_source"] == "given with --since"

    w = _soon_world()
    w.release("Qwen/Qwen3.8-Probe-7B", "2026-09-11")
    given = {
        "recheck": {"acme/Gone-7B": "2026-02-01"},
        "known_orgs": {"qwen"},
        "seen": {"acme/Before-7B": "2026-07-30"},
        "left_out": {"acme/Before-Probe-7B": "2026-07-30"},
    }
    last_week = _report(tmp_path, "2026-09-17-model-survey-r0.md", _run(w, first_run_since=date(2026, 6, 1), **given))

    follow = plan(last_week, None, False, today)
    assert follow["since"] == date(2026, 8, 18) and follow["since_source"] == "30 days before the report it follows"
    assert follow["first_run_since"] == date(2026, 6, 26)
    assert follow["recheck"] == {"acme/Soon-7B": "2026-09-10"}
    assert follow["known_orgs"] == {"qwen", "acme"}, "lower-cased, however the watchlist spells them"
    assert follow["seen"] == {"acme/before-7b": "2026-07-30", "acme/soon-7b": "2026-09-10"}
    assert follow["left_out"] == {"acme/before-probe-7b": "2026-07-30", "qwen/qwen3.8-probe-7b": "2026-09-11"}
    assert follow["label"] == "`2026-09-17-model-survey-r0.md`"

    # Replacing it, after a policy fix and a week later: everything it was given, nothing it left over.
    redo = plan(last_week, None, True, today)
    assert (redo["since"], redo["first_run_since"]) == (date(2026, 7, 22), date(2026, 6, 1))
    assert redo["since_source"] == "repeated from the replaced report, where it was given by the caller"
    assert redo["recheck"] == {"acme/Gone-7B": "2026-02-01"} and redo["known_orgs"] == {"qwen"}
    assert redo["seen"] == {"acme/before-7b": "2026-07-30"} and redo["left_out"] == {
        "acme/before-probe-7b": "2026-07-30"
    }
    assert "which this run replaces" in redo["label"]

    # A report dated today is one this run replaces. Following it would survey nothing.
    todays = _report(tmp_path, "2026-09-24-model-survey-r1.md", _run(w, first_run_since=date(2026, 6, 1), **given))
    same_day = plan(todays, None, False, today)
    assert (same_day["since"], same_day["recheck"]) == (date(2026, 7, 22), {"acme/Gone-7B": "2026-02-01"})

    override = plan(last_week, date(2026, 1, 1), False, today)
    assert (override["since"], override["since_source"]) == (date(2026, 1, 1), "given with --since")

    # Passed with --previous under a name that carries no date: the day its state was written.
    undated = tmp_path / "last-survey.md"
    undated.write_text(last_week.read_text())
    assert plan(undated, None, False, today)["since"] == date(2026, 8, 18)


def test_replacing_a_report_on_a_later_day_gives_a_new_vendor_the_same_first_run_window() -> None:
    from datetime import datetime

    from scripts.model_survey.__main__ import render, state_from_report, survey

    w = World()
    w.release("newco/Edge-7B", "2026-06-20")  # inside a 90-day window on 09-17, outside one on 09-24
    w.gguf("unsloth/Edge-7B-GGUF", {"Edge-7B-Q4_K_M.gguf": 4_000_000_000})
    w.gguf("Qwen/Qwen3-8B-GGUF", {"Qwen3-8B-Q4_K_M.gguf": 5_030_000_000}, arch="qwen3", ctx=40960)
    policy = _policy(orgs=["Qwen", "newco"])
    b = _run(w, policy=policy, known_orgs={"qwen"})
    assert [c["model"]["repo"] for c in b["candidates"]] == ["newco/Edge-7B"]
    inputs = state_from_report(render(b, policy, "b", datetime(2026, 9, 17)))["inputs"]
    hub, client = w.clients()
    c = survey(
        policy, SINCE, hub, client, curated=[_curated()], known_orgs=set(inputs["known_orgs"]),
        first_run_since=date.fromisoformat(inputs["first_run_since"]), today=date(2026, 9, 24),
    )  # fmt: skip
    assert [x["model"]["repo"] for x in c["candidates"]] == ["newco/Edge-7B"]


def test_a_bf16_repo_is_the_release_when_the_vendor_lists_no_other_and_a_copy_when_it_does() -> None:
    w = World()
    w.release("acme/Lightning-30B-A3B-BF16", "2026-08-20")  # NVIDIA publishes only this
    w.gguf("unsloth/Lightning-30B-A3B-GGUF", {"Lightning-30B-A3B-Q4_K_M.gguf": 18_000_000_000})
    w.release("acme/Plain-7B", "2026-08-20", licence="other")
    w.release("acme/Plain-7B-BF16", "2026-08-20", licence="other")
    facts = _run(w)
    assert [(c["model"]["repo"], c["gguf"]["repo"]) for c in facts["candidates"]] == [
        ("acme/Lightning-30B-A3B-BF16", "unsloth/Lightning-30B-A3B-GGUF")
    ]
    assert [c["model"]["repo"] for c in facts["screened"]] == ["acme/Plain-7B"]
    assert [(e["repo"], e["why"]) for e in facts["excluded"]] == [
        ("acme/Plain-7B-BF16", "full-precision copy of plain-7b")
    ]


def test_a_pretrained_base_in_the_window_is_left_out_when_its_instruct_sibling_is_listed() -> None:
    w = World()
    for repo in ("Qwen/Qwen4-9B", "Qwen/Qwen4-9B-it"):
        w.release(repo, "2026-08-20")
        w.gguf(f"unsloth/{repo.split('/')[1]}-GGUF", {"m-Q4_K_M.gguf": 5_500_000_000})
    facts = _run(w)
    assert [c["model"]["repo"] for c in facts["candidates"]] == ["Qwen/Qwen4-9B-it"]
    assert [(e["repo"], e["why"]) for e in facts["excluded"]] == [
        ("Qwen/Qwen4-9B", "pretrained base of its -it sibling")
    ]


def test_trending_outside_the_watchlist_leaves_out_builds_and_excluded_names() -> None:
    w = World()
    row = {"createdAt": "2026-09-01T00:00:00Z", "likes": 9}
    w.trending = [
        {"id": "Edge0/Edge0-35B-GGUF", **row},
        {"id": "Edge0/Edge0-35B-FP8", **row},
        {"id": "Edge0/Edge0-35B", **row},
    ]
    assert [r["repo"] for r in _run(w)["outside_watchlist"]] == ["Edge0/Edge0-35B"]


def test_a_failed_github_request_is_an_error_not_an_empty_answer() -> None:
    import httpx

    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503, json={"message": "unavailable"})))
    with pytest.raises(httpx.HTTPStatusError):
        llamacpp.architectures_at("master", client)
    with pytest.raises(httpx.HTTPStatusError):
        llamacpp.latest_release(client)


def test_the_cache_directory_must_be_ours_and_closed_to_others(tmp_path, monkeypatch) -> None:
    import os

    from scripts.model_survey import hf

    shared = tmp_path / "shared"
    shared.mkdir(mode=0o777)
    shared.chmod(0o777)
    with pytest.raises(hf.HubError, match="writable by others"):
        hf.Hub(token=None, cache_dir=shared)
    shared.chmod(0o700)
    hf.Hub(token=None, cache_dir=shared).close()
    monkeypatch.setattr(hf.os, "getuid", lambda: os.stat(shared).st_uid + 1)
    with pytest.raises(hf.HubError, match="another user"):
        hf.Hub(token=None, cache_dir=shared)


def test_an_answer_cached_without_a_token_is_not_served_to_a_run_that_has_one(tmp_path) -> None:
    w = World()
    anonymous, _ = w.clients(cache_dir=tmp_path)
    assert anonymous.model("acme/Gated-7B") is None  # 401 without a token: absent
    w.release("acme/Gated-7B", "2026-08-01")
    authenticated, _ = w.clients(cache_dir=tmp_path, token="t")
    assert authenticated.model("acme/Gated-7B")["id"] == "acme/Gated-7B"


def test_a_state_block_with_a_key_missing_is_unreadable_not_a_crash(tmp_path, capsys) -> None:
    from scripts.model_survey import __main__ as cli

    facts = _run(_soon_world())
    good = _report(tmp_path, "2026-09-10-model-survey-r0.md", facts).read_text()
    state = cli.state_from_report(good)
    for path in (("since",), ("outputs", "vendors"), ("inputs", "seen"), ("inputs", "first_run_since")):
        broken = json.loads(json.dumps(state))
        holder = broken
        for key in path[:-1]:
            holder = holder[key]
        del holder[path[-1]]
        assert cli.state_from_report(f"## State\n\n```json\n{json.dumps(broken)}\n```\n") is None, path
    newer = json.dumps({**state, "format": 99})  # complete in every key; only the format is one this tool does not know
    assert cli.state_from_report(f"## State\n\n```json\n{newer}\n```\n") is None
    assert cli.state_from_report(f"## State\n\n```json\n{json.dumps(state)}\n```\n") == state
    edited = tmp_path / "2026-09-11-model-survey-r1.md"
    edited.write_text('## State\n\n```json\n{"format": 1, "outputs": {"waiting": [], "over_cap": []}}\n```\n')
    assert cli.main(["--out", str(tmp_path / "out.md"), "--previous", str(edited)]) == 2
    assert "no state block this tool can read" in capsys.readouterr().err


def test_main_hands_the_plan_to_the_survey_and_writes_the_report_and_the_facts(tmp_path, monkeypatch) -> None:
    from datetime import UTC, datetime

    from scripts.model_survey import __main__ as cli

    reports = tmp_path / "reports"
    reports.mkdir()
    before = _soon_world()
    before.release("acme/Thing-Probe-7B", "2026-09-05")  # left out by name, and recorded as such
    _report(reports, "2026-09-10-model-survey-r0.md", _run(before), generated="2026-09-10T09:00:00+00:00")
    monkeypatch.setattr(cli, "REPORTS", reports)
    monkeypatch.setattr(cli, "utc_now", lambda: datetime(2026, 9, 17, 23, 30, tzinfo=UTC))
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
        "since": date(2026, 8, 11),
        "recheck": {"acme/Soon-7B": "2026-09-10"},
        "known_orgs": {"acme"},
        "seen": {"acme/soon-7b": "2026-09-10"},
        "left_out": {"acme/thing-probe-7b": "2026-09-05"},
        "first_run_since": date(2026, 6, 19),
        "today": date(2026, 9, 17),
    }
    written = (reports / "2026-09-17-model-survey-r1.md").read_text()
    assert "Previous survey:** `2026-09-10-model-survey-r0.md`" in written
    assert "window: releases since 2026-08-11 (30 days before the report it follows)" in written
    assert cli.state_from_report(written)["generated_at"] == "2026-09-17T23:30:00+00:00"
    assert json.loads(facts_path.read_text())["since"] == "2026-08-11"


def test_when_every_successor_fails_the_screen_the_audit_says_so_and_names_none() -> None:
    w = World()
    w.release("Qwen/Qwen3.8-9B", "2026-08-05", licence="other")
    w.release("Qwen/Qwen3.6-9B", "2026-04-20")  # permissive, but nobody trusted has built it
    tier = _run(w, policy=_policy(orgs=["acme"]))["curated"][0]
    assert [s["repo"] for s in tier["successors"]] == ["Qwen/Qwen3.8-9B", "Qwen/Qwen3.6-9B"]
    assert tier["findings"] == ["size-matched successors exist, but none passes the screen yet"]


# --- review round 5 -----------------------------------------------------------------------------------------------


def test_a_carried_release_that_cannot_be_read_for_one_run_is_named_and_carried_again() -> None:
    """A 401 that will pass, or a repo private for a re-upload. It was dropped
    in silence and stayed in the examined log, so every later run skipped it."""
    from datetime import datetime

    from scripts.model_survey.__main__ import render, state_from_report

    w = World()
    carried = {"acme/Soon-7B": "2026-09-10", "acme/Stale-7B": "2026-03-01", "acme/Undated-7B": ""}
    seen = {"acme/soon-7b": "2026-09-10", "acme/stale-7b": "2026-08-01"}
    facts = _run(w, recheck=carried, seen=seen)
    assert [(e["repo"], e["why"]) for e in facts["excluded"]] == [
        ("acme/Soon-7B", "not readable this run (renamed, private or gated); carried to the next"),
        ("acme/Stale-7B", "not readable this run (renamed, private or gated); dropped"),
        ("acme/Undated-7B", "not readable this run (renamed, private or gated); dropped"),
    ]
    assert facts["waiting"] == [{"repo": "acme/Soon-7B", "created": "2026-09-10"}] and facts["rechecked"] == []
    assert "acme/stale-7b" not in facts["examined_log"], "dropped, so a later window may find it again"

    state = state_from_report(render(facts, _policy(), "r2", datetime(2026, 9, 17), previous="`r1.md`"))
    w.release("acme/Soon-7B", "2026-09-10")  # back, and built
    w.gguf("unsloth/Soon-7B-GGUF", {"Soon-7B-Q4_K_M.gguf": 4_000_000_000})
    again = _run(w, recheck=state["outputs"]["waiting"], seen=state["outputs"]["examined"])
    assert [c["model"]["repo"] for c in again["candidates"]] == ["acme/Soon-7B"]


def test_a_carried_release_its_owner_renamed_is_followed_to_its_new_name() -> None:
    w = World()
    w.release("acme/Thing-7B", "2026-07-01")
    w.gguf("unsloth/Thing-7B-GGUF", {"Thing-7B-Q4_K_M.gguf": 4_000_000_000})
    w.redirects["acme/Thing-7B-preview"] = "acme/Thing-7B"
    w.redirects["acme/Moved-7B"] = "stranger/Moved-7B"
    w.release("stranger/Moved-7B", "2026-07-01")
    facts = _run(w, recheck={"acme/Thing-7B-preview": "2026-07-01", "acme/Moved-7B": "2026-07-01"})
    assert [c["model"]["repo"] for c in facts["candidates"]] == ["acme/Thing-7B"]
    assert [e["repo"] for e in facts["excluded"]] == ["acme/Moved-7B"], (
        "a transfer to another owner is not the vendor's repo"
    )


def test_a_renamed_release_the_window_already_examined_is_not_examined_twice() -> None:
    w = World()
    w.release("acme/Thing-7B", "2026-08-20")
    w.redirects["acme/Thing-7B-preview"] = "acme/Thing-7B"
    facts = _run(w, recheck={"acme/Thing-7B-preview": "2026-08-20"})
    assert [c["model"]["repo"] for c in facts["screened"]] == ["acme/Thing-7B"]


def test_a_followed_report_that_is_not_on_main_is_said_so(tmp_path, monkeypatch) -> None:
    from scripts.model_survey import __main__ as cli

    reports = tmp_path / "docs" / "reports"
    reports.mkdir(parents=True)
    monkeypatch.setattr(cli, "REPORTS", reports)
    report = _report(reports, "2026-09-10-model-survey-r0.md", _run(_soon_world()))
    asked: list[tuple] = []

    def git(*args):
        asked.append(args)
        return False

    monkeypatch.setattr(cli, "_git", git)
    run = cli.plan(report, None, False, date(2026, 9, 17))
    assert asked == [("cat-file", "-e", "origin/main:docs/reports/2026-09-10-model-survey-r0.md")]
    assert run["warnings"] == [
        "`2026-09-10-model-survey-r0.md` is not on origin/main (an open PR, or a file a closed one left behind), "
        "and this run follows it"
    ]
    monkeypatch.setattr(cli, "_git", lambda *a: True)
    assert cli.plan(report, None, False, date(2026, 9, 17))["warnings"] == []
    elsewhere = _report(tmp_path, "2026-09-10-model-survey-r0.md", _run(_soon_world()))
    assert cli.provenance_warning(elsewhere) is None, (
        "a report outside the repository cannot be on main and is not asked about"
    )


def test_a_state_block_with_a_value_of_the_wrong_form_is_unreadable_not_a_crash(tmp_path) -> None:
    from scripts.model_survey import __main__ as cli

    state = cli.state_from_report(_report(tmp_path, "2026-09-10-model-survey-r0.md", _run(_soon_world())).read_text())

    def with_value(path: tuple[str, ...], value) -> str:
        broken = json.loads(json.dumps(state))
        holder = broken
        for key in path[:-1]:
            holder = holder[key]
        holder[path[-1]] = value
        return f"## State\n\n```json\n{json.dumps(broken)}\n```\n"

    for path, value in (
        (("since",), "2026-9-1"),
        (("inputs", "left_out"), {"acme/x": "last week"}),
        (("inputs", "since_source"), None),
        (("inputs", "first_run_since"), None),
        (("outputs", "examined"), {"acme/x": None}),
        (("outputs", "waiting"), ["acme/x"]),
        (("outputs", "vendors"), [1]),
        (("inputs", "known_orgs"), "qwen"),
        (("generated_at",), "yesterday"),
    ):
        assert cli.state_from_report(with_value(path, value)) is None, path
    assert cli.state_from_report(with_value(("inputs", "carried"), {"acme/x": ""})) is not None, (
        "an unknown day is allowed"
    )
    conflict = "## State\n\n```json\n<<<<<<< HEAD\n{}\n=======\n{}\n>>>>>>> theirs\n```\n"
    assert cli.state_from_report(conflict) is None, "a merge conflict in the block"


def test_a_first_run_vendors_exclusions_are_all_listed_because_no_report_has_listed_them() -> None:
    w = World()
    w.release("newco/Old-Probe-7B", "2026-09-03")  # inside the overlap, but newco was not watched then
    w.release("acme/Old-Probe-7B", "2026-09-03")
    facts = _run(
        w, policy=_policy(orgs=["acme", "newco"]), known_orgs={"acme"}, left_out={"acme/Old-Probe-7B": "2026-09-03"}
    )
    assert [e["repo"] for e in facts["excluded"]] == ["newco/Old-Probe-7B"] and facts["overlap_excluded"] == 1


def test_a_newer_report_with_unreadable_state_is_still_the_newest_so_the_run_stops(tmp_path) -> None:
    from scripts.model_survey import __main__ as cli

    _report(tmp_path, "2026-09-10-model-survey-r0.md", _run(_soon_world()), generated="2026-09-10T09:00:00+00:00")
    (tmp_path / "2026-09-17-model-survey-r1.md").write_text("# a newer report whose state block was lost in a merge\n")
    newest = cli.previous_report(tmp_path)
    assert newest.name == "2026-09-17-model-survey-r1.md", (
        "following the older one would re-rank a week already surveyed"
    )
    with pytest.raises(cli.PlanError):
        cli.plan(newest, None, False, date(2026, 9, 24))


def test_find_gguf_falls_back_to_the_most_trusted_build_at_each_step() -> None:
    from scripts.model_survey.__main__ import find_gguf

    q4, q8 = {"m-Q4_K_M.gguf": 4_000_000_000}, {"m-Q8_0.gguf": 8_000_000_000}
    w = World()
    w.gguf("unsloth/Short-7B-GGUF", q4, ctx=8192)  # no build passes: the most trusted one with the quant
    w.gguf("bartowski/Short-7B-GGUF", q4, ctx=4096)
    w.gguf("unsloth/NoQuant-7B-GGUF", q8)  # no build has the quant: the most trusted one that exists
    w.gguf("bartowski/NoQuant-7B-GGUF", q8)
    hub, _ = w.clients()
    assert find_gguf(hub, "acme/Short-7B", POLICY)["context_length"] == 8192
    assert find_gguf(hub, "acme/NoQuant-7B", POLICY)["repo"] == "unsloth/NoQuant-7B-GGUF"


def test_a_release_without_a_readable_date_is_neither_carried_nor_a_crash() -> None:
    w = World()
    w.release("acme/Undated-7B", "2026-08-20")
    w.repos["acme/Undated-7B"]["createdAt"] = None
    facts = _run(w)
    assert [c["model"]["repo"] for c in facts["screened"]] == ["acme/Undated-7B"] and facts["waiting"] == []
    undated = {"model": _model("acme/Undated-7B", created=""), "gguf": _gguf("unsloth/X-GGUF")}
    assert score(undated, CURATED, date(2026, 9, 17))["recency"] == 0.0


def test_the_waiting_period_ends_the_day_after_wait_days() -> None:
    from datetime import timedelta

    from scripts.model_survey.__main__ import WAIT_DAYS

    w = World()
    w.release("acme/LastDay-7B", (TODAY - timedelta(days=WAIT_DAYS)).isoformat())
    w.release("acme/DayAfter-7B", (TODAY - timedelta(days=WAIT_DAYS + 1)).isoformat())
    facts = _run(w, recheck=["acme/LastDay-7B", "acme/DayAfter-7B"])
    assert [x["repo"] for x in facts["waiting"]] == ["acme/LastDay-7B"]


def test_popularity_is_capped_and_the_card_licence_beats_the_tag() -> None:
    huge = {"model": _model("acme/Hit-7B", likes=10**9), "gguf": _gguf("unsloth/X-GGUF")}
    assert score(huge, CURATED, date(2026, 9, 17))["popularity"] == 20.0
    info = {"id": "acme/Both-7B", "cardData": {"license": "mit"}, "tags": ["license:apache-2.0"]}
    assert summarize_model(info)["license"] == "mit"


def test_the_cache_directory_may_not_be_a_symlink(tmp_path) -> None:
    from scripts.model_survey import hf

    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(hf.HubError, match="symlink"):
        hf.Hub(token=None, cache_dir=link)


def test_a_first_run_says_so_instead_of_describing_an_overlap() -> None:
    from datetime import datetime

    from scripts.model_survey.__main__ import render

    header = render(_run(World()), _policy(), "r1", datetime(2026, 9, 17)).split("## Reading")[0]
    assert "- **Previous survey:** none; this is a first run" in header and "overlaps" not in header


# --- review round 6 -----------------------------------------------------------------------------------------------


def test_a_release_that_cannot_be_read_is_carried_whether_the_window_or_the_carried_list_reached_it() -> None:
    """The window files an unreadable release first, so the carried list's
    own handling never saw it: an over-cap release that read as absent for one
    run was dropped, and so was any window release."""
    from datetime import datetime

    from scripts.model_survey.__main__ import pending, render, state_from_report

    w = World()
    for repo in ("acme/Carried-7B", "acme/Fresh-7B"):
        w.release(repo, "2026-08-20")
        del w.repos[repo]  # listed in the window, and answering 401 this run
    facts = _run(w, recheck={"acme/Carried-7B": "2026-08-20"})
    assert {(e["repo"], e["why"]) for e in facts["excluded"]} == {
        ("acme/Carried-7B", "not readable this run (renamed, private or gated); carried to the next"),
        ("acme/Fresh-7B", "not readable this run (renamed, private or gated); carried to the next"),
    }
    assert facts["examined_log"] == {}, "neither was examined, so neither may be skipped as examined"
    assert len(facts["excluded"]) == 2 and len(facts["waiting"]) == 2, (
        "the window filed it; the carried list must not file it again"
    )
    state = state_from_report(render(facts, _policy(), "r1", datetime(2026, 9, 17)))
    assert pending(state) == {"acme/Carried-7B": "2026-08-20", "acme/Fresh-7B": "2026-08-20"}

    w.release("acme/Fresh-7B", "2026-08-20")  # readable again two weeks later, and built; the window has moved past it
    w.gguf("unsloth/Fresh-7B-GGUF", {"Fresh-7B-Q4_K_M.gguf": 4_000_000_000})
    from scripts.model_survey.__main__ import survey

    hub, client = w.clients()
    w.gguf("Qwen/Qwen3-8B-GGUF", {"Qwen3-8B-Q4_K_M.gguf": 5_030_000_000}, arch="qwen3", ctx=40960)
    later = survey(
        _policy(), date(2026, 8, 25), hub, client, curated=[_curated()], recheck=pending(state),
        seen=state["outputs"]["examined"], today=date(2026, 10, 1),
    )  # fmt: skip
    assert [c["model"]["repo"] for c in later["candidates"]] == ["acme/Fresh-7B"]


def test_a_carried_release_meets_every_exclusion_the_window_applies() -> None:
    w = World()
    for repo in ("acme/Thing-9B", "acme/Thing-9B-it", "acme/Plain-7B", "acme/Plain-7B-BF16"):
        w.release(repo, "2026-06-01")  # older than the window: only the carried list reaches them
        w.gguf(f"unsloth/{repo.split('/')[1]}-GGUF", {"m-Q4_K_M.gguf": 5_000_000_000})
    carried = {"acme/Thing-9B": "2026-06-01", "acme/Plain-7B-BF16": "2026-06-01", "acme/Thing-9B-it": "2026-06-01"}
    facts = _run(w, recheck=carried)
    assert [c["model"]["repo"] for c in facts["candidates"]] == ["acme/Thing-9B-it"]
    assert [(e["repo"], e["why"]) for e in facts["excluded"]] == [
        ("acme/Thing-9B", "pretrained base of its -it sibling"),
        ("acme/Plain-7B-BF16", "full-precision copy of plain-7b"),
    ]


def test_a_state_block_without_a_key_that_may_be_null_is_still_unreadable(tmp_path) -> None:
    """`None` is a value `known_orgs` allows. Absent is not `None`."""
    from scripts.model_survey import __main__ as cli

    state = cli.state_from_report(_report(tmp_path, "2026-09-10-model-survey-r0.md", _run(_soon_world())).read_text())
    assert state["inputs"]["known_orgs"] is None
    broken = json.loads(json.dumps(state))
    del broken["inputs"]["known_orgs"]
    assert cli.state_from_report(f"## State\n\n```json\n{json.dumps(broken)}\n```\n") is None


def test_on_one_day_a_report_with_unreadable_state_is_the_newest_so_the_run_stops(tmp_path) -> None:
    from scripts.model_survey import __main__ as cli

    _report(tmp_path, "2026-09-17-model-survey-a.md", _run(_soon_world()), generated="2026-09-17T09:00:00+00:00")
    (tmp_path / "2026-09-17-model-survey-b.md").write_text("# written later; its state block was lost in a merge\n")
    _report(tmp_path, "2026-09-17-model-survey-c.md", _run(_soon_world()), generated="2026-09-17T08:00:00+00:00")
    newest = cli.previous_report(tmp_path)
    assert newest.name == "2026-09-17-model-survey-b.md", "replacing `a` in silence would lose whatever `b` recorded"
    with pytest.raises(cli.PlanError):
        cli.plan(newest, None, False, date(2026, 9, 17))


def test_every_request_is_paced() -> None:
    w = World()
    w.gguf("x/ok", {})
    sleeps: list[float] = []
    hub, _ = w.clients(pause_s=0.65, sleep=sleeps.append)
    hub.model("x/ok")
    hub.model("x/missing")
    assert sleeps == [0.65, 0.65] and hub.requests == 2


@pytest.mark.parametrize("mode", [0o720, 0o702])
def test_a_cache_directory_writable_by_the_group_or_by_anyone_is_refused(tmp_path, mode: int) -> None:
    from scripts.model_survey import hf

    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(mode)
    with pytest.raises(hf.HubError, match="writable by others"):
        hf.Hub(token=None, cache_dir=shared)


def test_a_known_vendor_is_known_however_its_name_is_cased() -> None:
    w = World()
    w.release("Qwen/Qwen3.5-Earlier-7B", "2026-07-01")  # before the window; a first-run window would reach it
    facts = _run(w, known_orgs={"Qwen", "ACME"})
    assert [(x["org"], x["first_run"]) for x in facts["vendors"]] == [("Qwen", False), ("acme", False)]
    assert facts["screened"] == []


def test_git_that_cannot_run_means_not_on_main_rather_than_a_crash(monkeypatch) -> None:
    from scripts.model_survey import __main__ as cli

    def no_git(*a, **k):
        raise FileNotFoundError("git")

    monkeypatch.setattr(cli.subprocess, "run", no_git)
    assert cli._git("cat-file", "-e", "origin/main:x") is False


def test_a_carried_release_with_an_unreadable_day_is_dropped_not_kept_for_ever() -> None:
    from datetime import timedelta

    from scripts.model_survey.__main__ import WAIT_DAYS

    last_day = (TODAY - timedelta(days=WAIT_DAYS)).isoformat()
    day_after = (TODAY - timedelta(days=WAIT_DAYS + 1)).isoformat()
    facts = _run(
        World(), recheck={"acme/Garbled-7B": "soon", "acme/LastDay-7B": last_day, "acme/DayAfter-7B": day_after}
    )
    assert [w["repo"] for w in facts["waiting"]] == ["acme/LastDay-7B"]
    assert [e["why"].rsplit("; ", 1)[1] for e in facts["excluded"]] == ["dropped", "carried to the next", "dropped"]


def test_a_successor_that_cannot_be_read_is_screened_as_unknown_not_skipped() -> None:
    w = World()
    w.release("Qwen/Qwen3.5-9B", "2026-02-27")
    del w.repos["Qwen/Qwen3.5-9B"]
    successor = _run(w, policy=_policy(orgs=["acme"]))["curated"][0]["successors"][0]
    assert successor["repo"] == "Qwen/Qwen3.5-9B" and "licence is unknown" in successor["reasons"]


def test_the_successor_size_band_and_the_full_precision_rule_in_the_family_search() -> None:
    rows = _rows(
        "Qwen/Qwen3.5-4.8B", "Qwen/Qwen3.5-4.7B", "Qwen/Qwen3.5-12.8B", "Qwen/Qwen3.5-12.9B", tag="text-generation"
    )
    assert [f["repo"] for f in size_matched_successors(_curated(), rows, POLICY)] == [
        "Qwen/Qwen3.5-4.8B",
        "Qwen/Qwen3.5-12.8B",
    ]
    rows = _rows("Qwen/Qwen3.5-9B", "Qwen/Qwen3.5-9B-BF16", "Qwen/Qwen3.6-9B-BF16", tag="text-generation")
    found = [f["repo"] for f in size_matched_successors(_curated(), rows, POLICY)]
    assert found == ["Qwen/Qwen3.6-9B-BF16", "Qwen/Qwen3.5-9B"], "a BF16 copy is dropped; a BF16-only release is kept"


def test_a_401_with_a_good_token_means_absent_and_with_a_bad_one_ends_the_run(tmp_path) -> None:
    """What the Hub answers for a missing repo when a token is sent was an
    assumption. The token is checked instead, once."""
    from scripts.model_survey.hf import HubError

    w = World()
    good, _ = w.clients(token="t0ken", cache_dir=tmp_path / "good")
    assert good.model("acme/Missing-7B-GGUF") is None and good.model("acme/Other-7B-GGUF") is None
    asked = [r.url.path for r in w.requests]
    assert asked.count("/api/whoami-v2") == 1, asked
    bad, _ = w.clients(token="expired", cache_dir=tmp_path / "bad")
    with pytest.raises(HubError, match="HF_TOKEN"):
        bad.model("acme/Missing-7B-GGUF")
    assert list((tmp_path / "bad").iterdir()) == []


def test_an_old_report_with_unreadable_state_does_not_outrank_a_newer_readable_one(tmp_path) -> None:
    """Unreadable counts as the newest of its own day only. Otherwise one
    report from a tool that wrote no state would stop every run for ever."""
    from scripts.model_survey.__main__ import previous_report

    (tmp_path / "2026-09-03-model-survey-old.md").write_text("# from before reports carried state\n")
    _report(tmp_path, "2026-09-10-model-survey-r0.md", _run(_soon_world()), generated="2026-09-10T09:00:00+00:00")
    assert previous_report(tmp_path).name == "2026-09-10-model-survey-r0.md"


# --- review round 7 -----------------------------------------------------------------------------------------------


def test_the_report_shows_the_lists_the_skill_tells_the_operator_to_read() -> None:
    """The state block was asserted; the lists a person reads were not, and
    each could be deleted from the renderer with every test passing."""
    from datetime import datetime

    from scripts.model_survey import __main__ as cli

    w = World()
    w.release("acme/Soon-7B", "2026-09-10")  # may pass later: nobody has built it
    w.release("acme/Future-7B", "2026-08-25", likes=9999)  # a candidate that needs a llama.cpp upgrade
    w.gguf("unsloth/Future-7B-GGUF", {"Future-7B-Q4_K_M.gguf": 4_000_000_000}, arch="relarch")
    w.release("acme/Seen-7B", "2026-09-01")
    w.release("acme/Old-Probe-7B", "2026-09-03")
    for i in range(cli.PER_ORG):
        w.release(f"acme/Thing{i}-7B", "2026-08-10", likes=500 + i)
    facts = _run(w, seen={"acme/Seen-7B": "2026-09-01"}, left_out={"acme/Old-Probe-7B": "2026-09-03"})
    text = cli.render(facts, _policy(), "r1", datetime(2026, 9, 17), previous="`2026-09-10-model-survey-r0.md`")
    header = text.split("## Reading")[0]
    assert "the window overlaps earlier runs by 30 days, and 1 release(s) they examined were skipped" in header
    assert "(and 1 more that an earlier report listed)" in header
    assert (
        "⚠ architecture 'relarch' needs a llama.cpp upgrade past the pin (v9.9.9 has it)."
        in text.split("## The curated set")[0]
    )
    may_pass = text.split("## May pass later")[1].split("\n## ")[0]
    assert "- `acme/Thing11-7B` · 2026-08-10" in may_pass and "after its repo was created" in may_pass
    over_cap = text.split("### Over the per-vendor cap")[1].split("\n### ")[0]
    assert (
        "- `acme/Soon-7B` · 2026-09-10 · 100 likes" in over_cap
        and "- `acme/Thing0-7B` · 2026-08-10 · 500 likes" in over_cap
    )
    assert "- `acme` · 16 listed · 16 since 2026-07-22 · 12 examined · 1 examined by an earlier run" in text


def test_the_header_describes_an_overlap_only_when_the_run_followed_a_report(tmp_path) -> None:
    from datetime import datetime

    from scripts.model_survey import __main__ as cli

    facts = _run(_soon_world())
    for mode, overlaps in (("follows", True), ("replaces", False), ("fresh", False)):
        text = cli.render({**facts, "mode": mode}, _policy(), "r1", datetime(2026, 9, 17), previous="`r0.md`")
        assert ("overlaps earlier runs" in text.split("## Reading")[0]) is overlaps, mode
    first = _report(tmp_path, "2026-09-17-model-survey-a.md", facts)
    assert cli.plan(None, None, False, date(2026, 9, 17))["mode"] == "first"
    assert cli.plan(first, None, False, date(2026, 9, 17))["mode"] == "replaces"
    assert cli.plan(first, None, False, date(2026, 9, 24))["mode"] == "follows"
    stateless = tmp_path / "2026-09-18-model-survey-b.md"
    stateless.write_text("# no state\n")
    assert cli.plan(stateless, date(2026, 8, 1), False, date(2026, 9, 24))["mode"] == "fresh"


def test_replacing_a_replacement_does_not_nest_the_windows_source_and_the_warning_says_replaces(
    tmp_path, monkeypatch
) -> None:
    from scripts.model_survey import __main__ as cli

    monkeypatch.setattr(cli, "REPORTS", tmp_path / "docs" / "reports")
    (tmp_path / "docs" / "reports").mkdir(parents=True)
    monkeypatch.setattr(cli, "_git", lambda *a: False)
    today = date(2026, 9, 17)
    report = _report(cli.REPORTS, "2026-09-17-model-survey-a.md", {**_run(_soon_world()), "since_source": cli.GIVEN})
    expected = "repeated from the replaced report, where it was given with --since"
    for name in ("b", "c"):
        run = cli.plan(report, None, False, today)
        assert run["since_source"] == expected, name
        assert run["warnings"][0].endswith("and this run replaces it")
        facts = {**_run(_soon_world()), "since_source": run["since_source"]}
        report = _report(
            cli.REPORTS,
            f"2026-09-17-model-survey-{name}.md",
            facts,
            generated=f"2026-09-17T1{ord(name) - 97}:00:00+00:00",
        )
    assert cli.plan(report, None, False, date(2026, 9, 24))["warnings"][0].endswith("and this run follows it")


def test_a_vendor_taken_off_the_watchlist_takes_its_carried_releases_with_it() -> None:
    """Its own `-GGUF` repo was trusted because it was a watched vendor's."""
    w = World()
    w.release("gone/Soon-7B", "2026-09-10")
    w.gguf("gone/Soon-7B-GGUF", {"Soon-7B-Q4_K_M.gguf": 4_000_000_000})
    facts = _run(w, recheck={"gone/Soon-7B": "2026-09-10"})
    assert facts["candidates"] == [] and facts["waiting"] == []
    assert [(e["repo"], e["why"]) for e in facts["excluded"]] == [
        ("gone/Soon-7B", "its vendor is no longer on the watchlist")
    ]


def test_a_previous_report_that_is_not_there_is_a_message_not_a_traceback(tmp_path, capsys) -> None:
    from scripts.model_survey import __main__ as cli

    assert cli.main(["--out", str(tmp_path / "out.md"), "--previous", str(tmp_path / "absent.md")]) == 2
    assert "is not a file" in capsys.readouterr().err


def test_the_report_is_created_exclusively_because_the_first_check_was_minutes_ago(
    tmp_path, monkeypatch, capsys
) -> None:
    from scripts.model_survey import __main__ as cli

    out = tmp_path / "out.md"
    real_survey, w = cli.survey, World()

    def slow_survey(policy, since, hub, client, **kw):
        out.write_text("another run finished first")
        fake_hub, fake_client = w.clients()
        w.gguf("Qwen/Qwen3-8B-GGUF", {"Qwen3-8B-Q4_K_M.gguf": 5_030_000_000}, arch="qwen3", ctx=40960)
        return real_survey(_policy(), since, fake_hub, fake_client, curated=[_curated()], today=kw["today"])

    monkeypatch.setattr(cli, "survey", slow_survey)
    monkeypatch.setattr(cli, "previous_report", lambda: None)
    assert cli.main(["--out", str(out), "--since", "2026-08-01"]) == 2
    assert out.read_text() == "another run finished first" and "refusing to overwrite" in capsys.readouterr().err


def test_the_policys_limits_are_inclusive_where_it_says_at_least_and_at_most() -> None:
    arch = {"arch_at_pin": {"qwen35"}, "arch_at_head": {"qwen35"}}
    exactly = _gguf("unsloth/X-GGUF", context_length=POLICY.min_context, quant_bytes=int(POLICY.ceiling_gb * 1e9))
    assert screen(_model("acme/Edge-7B"), exactly, POLICY, **arch)["verdict"] == "candidate"
    over = _gguf("unsloth/X-GGUF", context_length=POLICY.min_context - 1, quant_bytes=int(POLICY.ceiling_gb * 1e9) + 1)
    assert len(screen(_model("acme/Edge-7B"), over, POLICY, **arch)["reasons"]) == 2


@pytest.mark.parametrize(
    ("size_gb", "fills"),
    [(8.0, True), (6.1, True), (5.9, False), (10.5, True), (10.7, False), (19.0, True), (21.5, False), (3.5, False)],
)
def test_the_gap_bonus_needs_a_wide_gap_and_a_gigabyte_of_room_on_each_side(size_gb: float, fills: bool) -> None:
    """The curated ladder is 2.5, 5.03, 11.6, 17 and 22.1 GB. Every step but the first is wider than 4 GB."""
    candidate = {"model": _model("acme/Mid-7B"), "gguf": _gguf("unsloth/X-GGUF", quant_bytes=int(size_gb * 1e9))}
    assert ("fills a gap in the size ladder" in score(candidate, CURATED, date(2026, 9, 17))) is fills


def test_the_family_search_leaves_out_builds_and_tasks_that_are_not_chat() -> None:
    rows = [
        {"id": "Qwen/Qwen3.5-9B", "createdAt": "2026-02-27T00:00:00Z", "pipeline_tag": "text-generation"},
        {"id": "Qwen/Qwen3.5-9B-GGUF", "createdAt": "2026-02-27T00:00:00Z", "pipeline_tag": "text-generation"},
        {
            "id": "Qwen/Qwen3.6-9B-Listener",
            "createdAt": "2026-04-01T00:00:00Z",
            "pipeline_tag": "automatic-speech-recognition",
        },
    ]
    assert excluded_fragment("Qwen/Qwen3.6-9B-Listener", POLICY) is None, "only its task keeps it out"
    assert [f["repo"] for f in size_matched_successors(_curated(), rows, POLICY)] == ["Qwen/Qwen3.5-9B"]


def test_equal_scores_are_ordered_by_name_and_screened_releases_by_likes() -> None:
    twins = [{"model": _model(f"acme/{n}-7B"), "gguf": _gguf("unsloth/X-GGUF")} for n in ("Zeta", "Alpha")]
    assert [c["model"]["repo"] for c in rank(twins, CURATED, date(2026, 9, 17))] == ["acme/Alpha-7B", "acme/Zeta-7B"]
    w = World()
    w.release("acme/Quiet-7B", "2026-08-01", likes=3)
    w.release("acme/Loud-7B", "2026-08-01", likes=900)
    assert [c["model"]["repo"] for c in _run(w)["screened"]] == ["acme/Loud-7B", "acme/Quiet-7B"]


def test_trending_outside_the_watchlist_keeps_the_twelve_most_liked() -> None:
    w = World()
    w.trending = [{"id": f"pub{i}/Model-7B", "createdAt": "2026-09-01T00:00:00Z", "likes": i} for i in range(1, 16)]
    outside = _run(w)["outside_watchlist"]
    assert len(outside) == 12 and outside[0]["repo"] == "pub15/Model-7B" and outside[-1]["repo"] == "pub4/Model-7B"


def test_the_owner_check_ignores_case_and_a_failed_git_describe_is_unknown(monkeypatch) -> None:
    import subprocess

    from scripts.model_survey import __main__ as cli

    w = World()
    w.gguf("Unsloth/Thing-7B-GGUF", {})
    w.redirects["unsloth/thing-7b-gguf"] = "Unsloth/Thing-7B-GGUF"
    hub, _ = w.clients()
    assert hub.model("unsloth/thing-7b-gguf")["id"] == "Unsloth/Thing-7B-GGUF"
    failed = subprocess.CompletedProcess([], 128, stdout="fatal: not a git repository", stderr="")
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: failed)
    assert cli.commit_id() == "unknown"

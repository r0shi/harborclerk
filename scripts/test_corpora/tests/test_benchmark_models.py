"""The sweep's model list is the registry's, and a model this instance cannot run is skipped with a reason."""

from __future__ import annotations

import re
from pathlib import Path

from scripts.test_corpora import conftest as cfg
from scripts.test_corpora.runner import sweep
from scripts.test_corpora.runner.state import StateFile, Status, Unit

REGISTRY = Path(__file__).resolve().parents[3] / "src/harbor_clerk/llm/models.py"


def test_the_model_list_is_the_registrys_in_the_registrys_order():
    """It was a hand-kept copy. Qwen3.5-9B and 4B were in the registry, and `--models qwen35-9b` was
    rejected as unknown."""
    ids = re.findall(r'^\s+id="([a-z0-9.-]+)",$', REGISTRY.read_text(), re.M)
    assert ids == cfg.ALL_MODELS and len(ids) == len(set(ids)) >= 7
    assert {"qwen35-9b", "qwen35-4b"} <= set(cfg.ALL_MODELS)
    assert set(cfg.TOP_MODELS) <= set(cfg.ALL_MODELS)


class _Instance:
    def __init__(self, models=None, fail=None):
        self._models, self._fail = models or [], fail

    def list_models(self):
        if self._fail:
            raise self._fail
        return self._models


def _listed(model: str, *, downloaded: bool, max_context_here: int, ram_gb: float, window: int = 262144) -> dict:
    """One row of GET /api/chat/models, with the fields `ModelOut` really has (api/schemas/chat.py).
    `fits_here` is computed as the API computes it: the FULL window fits."""
    return {
        "id": model,
        "name": model,
        "size_bytes": 1,
        "context_window": window,
        "supports_tools": True,
        "downloaded": downloaded,
        "active": False,
        "memory_bytes": 1,
        "min_ram_gb": 16,
        "max_context_here": max_context_here,
        "fits_here": max_context_here >= window,
        "system_ram_gb": ram_gb,
    }


def _state(tmp_path: Path, units: list[Unit]) -> StateFile:
    sf = StateFile(tmp_path / "state.json")
    sf.register(units)
    return sf


def _unit(phase: int, model: str, status: Status = Status.PENDING, qid: str = "q1") -> Unit:
    return Unit(phase=phase, corpus="cuad", model=model, question_id=qid, depth="standard", status=status)


def test_models_the_instance_cannot_run_are_skipped_with_a_reason(tmp_path, caplog):
    sf = _state(
        tmp_path,
        [
            _unit(0, "-"),
            _unit(1, "claude-baseline"),
            _unit(4, "qwen3-8b"),
            _unit(4, "qwen35-9b"),
            _unit(4, "qwen36-35b-a3b"),
            _unit(4, "brand-new"),
            _unit(4, "qwen35-9b", Status.DONE, qid="q2"),
            _unit(5, "qwen36-35b-a3b"),
        ],
    )
    instance = _Instance(
        [
            _listed("qwen3-8b", downloaded=True, max_context_here=32768, ram_gb=16.0),
            _listed("qwen35-9b", downloaded=False, max_context_here=83968, ram_gb=16.0),
            _listed("qwen36-35b-a3b", downloaded=True, max_context_here=0, ram_gb=16.0),
        ]
    )
    with caplog.at_level("WARNING"):
        reasons = sweep._skip_what_this_instance_cannot_run(instance, sf, phases={0, 1, 4})
    assert reasons == {
        "qwen35-9b": "not downloaded on this instance",
        "qwen36-35b-a3b": "does not fit in this machine's 16 GB at any context",
        "brand-new": "this instance's registry does not have it (an older build?)",
    }
    status = {(u.phase, u.model, u.question_id): u.status for u in sf.units()}
    assert status[(4, "qwen3-8b", "q1")] == Status.PENDING
    assert status[(4, "qwen35-9b", "q1")] == Status.SKIPPED
    assert status[(4, "qwen35-9b", "q2")] == Status.DONE, "a finished unit is never touched"
    assert status[(5, "qwen36-35b-a3b", "q1")] == Status.PENDING, "phase 5 is not being run"
    assert status[(0, "-", "q1")] == status[(1, "claude-baseline", "q1")] == Status.PENDING
    skipped = sf.get(4, "cuad", "qwen35-9b", "q1", "standard")
    assert skipped.error == "skipped before the run: not downloaded on this instance"
    assert "--rerun 'model=qwen35-9b,status=skipped'" in caplog.text
    # Saved: a crash after this point must not bring the units back as pending.
    reloaded = StateFile(tmp_path / "state.json")
    reloaded.load()
    assert reloaded.get(4, "cuad", "qwen35-9b", "q1", "standard").status == Status.SKIPPED


def test_a_model_the_app_loads_with_a_clamped_context_is_not_skipped(tmp_path):
    """Found in review. `fits_here` is false whenever the full window does not fit, and the app still loads
    the model: on the 32 GB mini the 35B runs at 239,616 of 262,144 tokens. Skipping on `fits_here` dropped
    it from every run there, with no way back, while the preflight said it fits."""
    sf = _state(tmp_path, [_unit(4, "qwen36-35b-a3b"), _unit(4, "gemma4-26b-a4b")])
    mini = _Instance(
        [
            _listed("qwen36-35b-a3b", downloaded=True, max_context_here=239_616, ram_gb=32.0),
            _listed("gemma4-26b-a4b", downloaded=True, max_context_here=262_144, ram_gb=32.0),
        ]
    )
    assert mini.list_models()[0]["fits_here"] is False, "the API's own word for it, and not a reason to skip"
    assert sweep._skip_what_this_instance_cannot_run(mini, sf, {4}) == {}
    assert {u.status for u in sf.units()} == {Status.PENDING}


def test_the_logged_way_back_revives_the_skipped_units_and_only_those(tmp_path):
    sf = _state(tmp_path, [_unit(4, "qwen35-9b"), _unit(4, "qwen35-9b", Status.DONE, qid="q2")])
    none = _Instance([_listed("qwen35-9b", downloaded=False, max_context_here=83968, ram_gb=16.0)])
    sweep._skip_what_this_instance_cannot_run(none, sf, {4})
    assert sf.rerun(sweep._parse_selectors("model=qwen35-9b,status=skipped")) == 1
    assert sf.get(4, "cuad", "qwen35-9b", "q1", "standard").status == Status.PENDING
    assert sf.get(4, "cuad", "qwen35-9b", "q2", "standard").status == Status.DONE


def test_an_instance_that_cannot_list_its_models_skips_nothing(tmp_path, caplog):
    sf = _state(tmp_path, [_unit(4, "qwen3-8b")])
    with caplog.at_level("WARNING"):
        assert sweep._skip_what_this_instance_cannot_run(_Instance(fail=RuntimeError("401")), sf, {4}) == {}
    assert sf.units()[0].status == Status.PENDING and "not skipping any" in caplog.text
    # An empty listing is "could not tell", not "nothing is downloaded".
    assert sweep._skip_what_this_instance_cannot_run(_Instance([]), sf, {4}) == {}
    assert sf.units()[0].status == Status.PENDING


def test_an_older_instance_without_the_fit_field_is_taken_at_its_word(tmp_path):
    sf = _state(tmp_path, [_unit(4, "qwen3-8b")])
    old = {"id": "qwen3-8b", "downloaded": True}  # before #655: no fits_here, no max_context_here
    assert sweep._skip_what_this_instance_cannot_run(_Instance([old]), sf, {4}) == {}


# ── the sweep wipes the instance before each corpus; only a disposable one ──


class _Folders:
    def __init__(self, folders):
        self._folders = folders

    def watch_folder_list(self):
        return self._folders


WORKDIR = Path("/work/test-corpora")


def _folder(path: str | Path) -> dict:
    """One row of GET /api/watch/folders, as `_folder_to_dict` builds it (api/routes/watch.py). There is no
    name the client chose: `display_name` is the path's last segment, "ingest" for every corpus."""
    return {
        "folder_id": "3f0e7c0a-0000-4000-8000-000000000001",
        "path": str(path),
        "recursive": True,
        "enabled": True,
        "last_event_id": None,
        "last_scan_at": None,
        "file_count": 510,
        "created_at": "2026-09-18T00:00:00+00:00",
        "display_name": Path(str(path)).name,
        "auto_discovered": False,
        "unavailable_reason": None,
        "skipped_count": 0,
        "skipped_extensions": [],
    }


OURS = [_folder(WORKDIR / "cuad" / "ingest")]
THEIRS = [*OURS, _folder("/Users/someone/Documents/clients")]


def _guard(folders, api_base="http://localhost:8100", workdir=WORKDIR):
    return sweep._refuse_to_wipe_a_real_instance(_Folders(folders), api_base, workdir)


def test_an_instance_nobody_called_disposable_is_never_wiped(monkeypatch):
    import pytest

    monkeypatch.delenv(sweep.DISPOSABLE_ENV, raising=False)
    with pytest.raises(sweep.NotDisposable, match="HC_EVAL_DISPOSABLE=1 is not set"):
        _guard(OURS)
    monkeypatch.setenv(sweep.DISPOSABLE_ENV, "true")  # only the literal 1
    with pytest.raises(sweep.NotDisposable):
        _guard(OURS)


def test_nothing_remote_is_ever_wiped(monkeypatch):
    import pytest

    monkeypatch.setenv(sweep.DISPOSABLE_ENV, "1")
    for remote in (
        "https://clerk.example.com",
        "http://192.168.1.20:8100",
        "http://ix.tail1234.ts.net:8100",
        "nonsense",
    ):
        with pytest.raises(sweep.NotDisposable, match="loopback"):
            _guard([], remote)
    for local in ("http://localhost:8100", "https://localhost", "http://127.0.0.1:8100", "http://[::1]:8100"):
        _guard(OURS, local)


def test_the_harness_knows_its_own_folders_as_the_server_describes_them(monkeypatch):
    """Found in review. The guard looked for a `name` starting `test-corpora-`; the server returns no such
    field, and `display_name` is "ingest". Every run after the first ingest was refused, and the second
    corpus of a multi-corpus run errored unit by unit."""
    monkeypatch.setenv(sweep.DISPOSABLE_ENV, "1")
    assert OURS[0]["display_name"] == "ingest" and "name" not in OURS[0]
    _guard([])  # a fresh instance
    _guard(OURS)  # after the first corpus: the state every later invocation finds
    _guard([_folder(WORKDIR / c / "ingest") for c in ("cuad", "enron", "synthetic", "unified")])
    _guard([_folder("/work/test-corpora/cuad/../cuad/ingest/")], workdir=Path("/work/test-corpora/"))


def test_a_watched_folder_that_is_not_the_harnesss_means_a_real_corpus(monkeypatch):
    import pytest

    monkeypatch.setenv(sweep.DISPOSABLE_ENV, "1")
    with pytest.raises(sweep.NotDisposable, match="/Users/someone/Documents/clients"):
        _guard(THEIRS)
    for not_ours in (
        WORKDIR,  # the workdir itself
        WORKDIR / "cuad",  # a corpus directory, not its ingest directory
        WORKDIR / "mine" / "ingest",  # under the workdir, but no corpus of the harness's
        "/elsewhere/cuad/ingest",  # the right shape under another workdir
    ):
        with pytest.raises(sweep.NotDisposable, match="real corpus"):
            _guard([_folder(not_ours)])
    with pytest.raises(sweep.NotDisposable):
        _guard([{**_folder("/x"), "path": None}])


def test_the_deletion_itself_is_behind_the_guard(monkeypatch):
    """Not only the check before the first unit: the function that deletes checks again, first."""
    import pytest

    monkeypatch.delenv(sweep.DISPOSABLE_ENV, raising=False)
    deleted = []

    class Instance(_Folders):
        def watch_folder_delete(self, folder_id):
            deleted.append(folder_id)

        def delete_all_documents(self, confirm=False):
            deleted.append("everything")

    with pytest.raises(sweep.NotDisposable):
        sweep._ingest_corpus(Instance(THEIRS), manifest=None, api_base="http://localhost:8100", workdir=WORKDIR)
    assert deleted == []


def test_a_second_corpus_is_ingested_over_the_firsts_folder(monkeypatch, tmp_path):
    """The multi-corpus case end to end: the instance holds the first corpus's folder when the second is
    ingested. The guard passes, and then the folder and the documents go."""
    monkeypatch.setenv(sweep.DISPOSABLE_ENV, "1")
    done = []

    class Instance(_Folders):
        def watch_folder_delete(self, folder_id):
            done.append(("folder", folder_id))

        def delete_all_documents(self, confirm=False):
            done.append(("documents", confirm))

        def watch_folder_add(self, path, name=None):
            raise StopIteration  # far enough

    import pytest

    manifest = sweep.CorpusManifest(
        corpus_id="enron",
        ingest_dir=tmp_path / "enron" / "ingest",
        doc_count=1,
        total_size_bytes=1,
        license="x",
        notes="",
    )
    with pytest.raises(StopIteration):
        sweep._ingest_corpus(
            Instance([_folder(tmp_path / "cuad" / "ingest")]), manifest, "http://localhost:8100", tmp_path
        )
    assert done == [("folder", "3f0e7c0a-0000-4000-8000-000000000001"), ("documents", True)]


def test_the_sweep_does_not_start_against_an_instance_it_may_not_wipe(tmp_path, monkeypatch, caplog):
    base = ["--run-id", "r1", "--workdir", str(tmp_path), "--phases", "1", "--corpora", "cuad"]
    monkeypatch.setattr(sweep.HarborClerkClient, "watch_folder_list", lambda self: THEIRS)
    monkeypatch.setattr(sweep.HarborClerkClient, "list_models", lambda self: [])
    made = []
    monkeypatch.setattr(sweep.spend, "anthropic_client", lambda kind: made.append(kind))

    monkeypatch.delenv(sweep.DISPOSABLE_ENV, raising=False)
    with caplog.at_level("ERROR"):
        assert sweep.main(base) == 4
    assert "HC_EVAL_DISPOSABLE=1 is not set" in caplog.text and made == []

    monkeypatch.setenv(sweep.DISPOSABLE_ENV, "1")
    with caplog.at_level("ERROR"):
        assert sweep.main([*base, "--resume"]) == 4
    assert "/Users/someone/Documents/clients" in caplog.text and made == []

    def unreachable(self):
        raise ConnectionError("refused")

    monkeypatch.setattr(sweep.HarborClerkClient, "watch_folder_list", unreachable)
    with caplog.at_level("ERROR"):
        assert sweep.main([*base, "--resume"]) == 4
    assert "could not check" in caplog.text and made == []

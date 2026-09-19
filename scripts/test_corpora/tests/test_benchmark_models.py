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
            {"id": "qwen3-8b", "downloaded": True, "fits_here": True},
            {"id": "qwen35-9b", "downloaded": False, "fits_here": True},
            {"id": "qwen36-35b-a3b", "downloaded": True, "fits_here": False, "system_ram_gb": 16.0},
        ]
    )
    with caplog.at_level("WARNING"):
        reasons = sweep._skip_what_this_instance_cannot_run(instance, sf, phases={0, 1, 4})
    assert reasons == {
        "qwen35-9b": "not downloaded on this instance",
        "qwen36-35b-a3b": "does not fit in this machine's 16 GB",
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
    assert "--rerun 'model=qwen35-9b'" in caplog.text
    # Saved: a crash after this point must not bring the units back as pending.
    reloaded = StateFile(tmp_path / "state.json")
    reloaded.load()
    assert reloaded.get(4, "cuad", "qwen35-9b", "q1", "standard").status == Status.SKIPPED


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
    assert sweep._skip_what_this_instance_cannot_run(_Instance([{"id": "qwen3-8b", "downloaded": True}]), sf, {4}) == {}


# ── the sweep wipes the instance before each corpus; only a disposable one ──


class _Folders:
    def __init__(self, folders):
        self._folders = folders

    def watch_folder_list(self):
        return self._folders


OURS = [{"name": "test-corpora-cuad", "path": "/work/cuad/ingest"}]
THEIRS = [*OURS, {"name": "Client files", "path": "/Users/someone/Documents/clients"}]


def test_an_instance_nobody_called_disposable_is_never_wiped(monkeypatch):
    import pytest

    monkeypatch.delenv(sweep.DISPOSABLE_ENV, raising=False)
    with pytest.raises(sweep.NotDisposable, match="HC_EVAL_DISPOSABLE=1 is not set"):
        sweep._refuse_to_wipe_a_real_instance(_Folders(OURS), "http://localhost:8100")
    monkeypatch.setenv(sweep.DISPOSABLE_ENV, "true")  # only the literal 1
    with pytest.raises(sweep.NotDisposable):
        sweep._refuse_to_wipe_a_real_instance(_Folders(OURS), "http://localhost:8100")


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
            sweep._refuse_to_wipe_a_real_instance(_Folders([]), remote)
    for local in ("http://localhost:8100", "https://localhost", "http://127.0.0.1:8100", "http://[::1]:8100"):
        sweep._refuse_to_wipe_a_real_instance(_Folders(OURS), local)


def test_a_watched_folder_the_harness_did_not_make_means_a_real_corpus(monkeypatch):
    import pytest

    monkeypatch.setenv(sweep.DISPOSABLE_ENV, "1")
    with pytest.raises(sweep.NotDisposable, match="Client files -> /Users/someone/Documents/clients"):
        sweep._refuse_to_wipe_a_real_instance(_Folders(THEIRS), "http://localhost:8100")
    with pytest.raises(sweep.NotDisposable, match="unnamed"):
        sweep._refuse_to_wipe_a_real_instance(_Folders([{"name": None, "path": "/x"}]), "http://localhost:8100")
    sweep._refuse_to_wipe_a_real_instance(_Folders([]), "http://localhost:8100")


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
        sweep._ingest_corpus(
            Instance([{"name": "Client files", "folder_id": "f1"}]), manifest=None, api_base="http://localhost:8100"
        )
    assert deleted == []


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
    assert "Client files" in caplog.text and made == []

    def unreachable(self):
        raise ConnectionError("refused")

    monkeypatch.setattr(sweep.HarborClerkClient, "watch_folder_list", unreachable)
    with caplog.at_level("ERROR"):
        assert sweep.main([*base, "--resume"]) == 4
    assert "could not check" in caplog.text and made == []

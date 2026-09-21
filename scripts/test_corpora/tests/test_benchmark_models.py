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
    assert "skipping qwen35-9b: not downloaded on this instance. Its units are reconsidered" in caplog.text
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


def test_what_an_earlier_start_skipped_is_decided_again_at_the_next(tmp_path):
    """Found in review. A sweep pointed at the wrong instance skipped models that instance had not
    downloaded; resumed against the right one, they stayed skipped, and the report stated the stale reason
    as fact. The same goes for a model downloaded since."""
    units = [
        _unit(4, "qwen35-9b"),
        _unit(4, "qwen35-9b", Status.DONE, qid="q2"),
        _unit(4, "qwen3-8b", Status.SKIPPED, qid="q3"),
    ]
    sf = _state(tmp_path, units)
    absent = _Instance([_listed("qwen35-9b", downloaded=False, max_context_here=83968, ram_gb=16.0)])
    assert sweep._skip_what_this_instance_cannot_run(absent, sf, {4}) == {
        "qwen35-9b": "not downloaded on this instance"
    }
    present = _Instance(
        [
            _listed("qwen35-9b", downloaded=True, max_context_here=83968, ram_gb=16.0),
            _listed("qwen3-8b", downloaded=True, max_context_here=32768, ram_gb=16.0),
        ]
    )
    assert sweep._skip_what_this_instance_cannot_run(present, sf, {4}) == {}
    assert sf.get(4, "cuad", "qwen35-9b", "q1", "standard").status == Status.PENDING
    assert sf.get(4, "cuad", "qwen35-9b", "q1", "standard").error is None
    assert sf.get(4, "cuad", "qwen35-9b", "q2", "standard").status == Status.DONE
    assert sf.get(4, "cuad", "qwen3-8b", "q3", "standard").status == Status.SKIPPED, "the operator's own --skip stays"
    reloaded = StateFile(tmp_path / "state.json")
    reloaded.load()
    assert reloaded.get(4, "cuad", "qwen35-9b", "q1", "standard").status == Status.PENDING, "the revival is saved"


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
    """The read-only calls the guard makes."""

    def __init__(self, folders, *, documents=0, mailboxes=()):
        self._folders, self._documents, self._mailboxes = folders, documents, list(mailboxes)

    def watch_folder_list(self):
        return self._folders

    def document_count(self):
        return self._documents

    def mail_accounts(self):
        return self._mailboxes


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


def _guard(folders, api_base="http://localhost:8100", workdir=WORKDIR, **holding):
    return sweep._refuse_to_wipe_a_real_instance(_Folders(folders, **holding), api_base, workdir)


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


def test_the_wipe_takes_uploads_and_mail_too_so_the_guard_looks_for_them(monkeypatch):
    """Found in review. delete-all-documents truncates documents, uploads and fetched mail alike, and the
    guard looked only at watched folders: an instance with no folders and a mailbox passed as fresh. Mail is
    the one source Harbor Clerk copies rather than reads in place, so it cannot be re-read."""
    import pytest

    monkeypatch.setenv(sweep.DISPOSABLE_ENV, "1")
    with pytest.raises(sweep.NotDisposable, match="1 connected mailbox"):
        _guard([], mailboxes=[{"account_id": "a1", "email": "someone@example.test"}])
    with pytest.raises(sweep.NotDisposable, match="1 connected mailbox"):
        _guard(OURS, documents=510, mailboxes=[{"account_id": "a1"}])
    with pytest.raises(sweep.NotDisposable, match="holds 42 document.s. and no folder"):
        _guard([], documents=42)
    _guard(OURS, documents=510)  # the harness's own corpus, from the last run
    _guard([], documents=0)  # a fresh instance


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


def _stub_instance(monkeypatch, *, folders, models=(), documents=0, mailboxes=()):
    monkeypatch.setattr(sweep.HarborClerkClient, "watch_folder_list", lambda self: folders)
    monkeypatch.setattr(sweep.HarborClerkClient, "document_count", lambda self: documents)
    monkeypatch.setattr(sweep.HarborClerkClient, "mail_accounts", lambda self: list(mailboxes))
    monkeypatch.setattr(sweep.HarborClerkClient, "list_models", lambda self: list(models))


def test_a_refused_start_leaves_nothing_behind_so_the_same_command_works_next_time(tmp_path, monkeypatch, caplog):
    """Found in review. The refusal came after state.json and spend.json were written, so the identical
    command, re-run with the flag set, died on the run-id guard ("already has units, use --resume")."""
    base = ["--run-id", "r1", "--workdir", str(tmp_path), "--phases", "1", "--corpora", "cuad"]
    run_dir = tmp_path / "results" / "r1"
    made = []
    monkeypatch.setattr(sweep.spend, "anthropic_client", lambda kind: made.append(kind))
    _stub_instance(monkeypatch, folders=THEIRS)

    # Without the flag the run is refused before it has touched the disk or the network. (The later check
    # would refuse too, and tidy up; by then it has written a run and logged in to an instance nobody
    # vouched for.)
    monkeypatch.setenv("HC_USERNAME", "someone@example.test")
    monkeypatch.setenv("HC_PASSWORD", "not-a-real-password")
    touched = []
    monkeypatch.setattr(sweep.HarborClerkClient, "login", lambda self, *a: touched.append("login"))
    monkeypatch.setattr(sweep.spend, "configure", lambda **kw: touched.append("ledger"))
    monkeypatch.delenv(sweep.DISPOSABLE_ENV, raising=False)
    with caplog.at_level("ERROR"):
        assert sweep.main(base) == 4
    assert "HC_EVAL_DISPOSABLE=1 is not set" in caplog.text and touched == []
    assert not (run_dir / "state.json").exists() and not (run_dir / "spend.json").exists()
    monkeypatch.undo()
    monkeypatch.setattr(sweep.spend, "anthropic_client", lambda kind: made.append(kind))
    _stub_instance(monkeypatch, folders=THEIRS)

    monkeypatch.setenv(sweep.DISPOSABLE_ENV, "1")
    with caplog.at_level("ERROR"):
        assert sweep.main(base) == 4  # the same command: no --resume needed
    assert "/Users/someone/Documents/clients" in caplog.text
    assert not (run_dir / "state.json").exists() and not (run_dir / "spend.json").exists()

    def unreachable(self):
        raise ConnectionError("refused")

    monkeypatch.setattr(sweep.HarborClerkClient, "watch_folder_list", unreachable)
    with caplog.at_level("ERROR"):
        assert sweep.main(base) == 4
    assert "could not check that it may be wiped" in caplog.text and made == []


def test_a_sweep_pointed_at_the_wrong_instance_leaves_no_judgements_in_a_run_that_existed(tmp_path, monkeypatch):
    """The ordering bug itself: skip first, refuse second, and the wrong instance's "not downloaded" was
    saved into the run before the guard spoke."""
    monkeypatch.setenv(sweep.DISPOSABLE_ENV, "1")
    base = ["--run-id", "r1", "--workdir", str(tmp_path), "--phases", "4", "--corpora", "cuad", "--models", "qwen3-8b"]
    _stub_instance(monkeypatch, folders=[])
    assert sweep.main([*base, "--spend-cap-usd", "0.01"]) == 3  # the run exists now: state.json, spend.json
    state = tmp_path / "results" / "r1" / "state.json"
    before = state.read_text()

    wrong = [_listed("qwen3-8b", downloaded=False, max_context_here=32768, ram_gb=32.0)]
    _stub_instance(monkeypatch, folders=THEIRS, models=wrong)
    assert sweep.main([*base, "--resume", "--no-judge"]) == 4
    assert state.exists(), "a run that was there before is not taken back"
    assert '"skipped"' not in state.read_text() and state.read_text().count('"pending"') == before.count('"pending"')


def test_a_run_that_wipes_nothing_needs_no_flag(tmp_path, monkeypatch):
    monkeypatch.delenv(sweep.DISPOSABLE_ENV, raising=False)
    base = [
        "--run-id",
        "r1",
        "--workdir",
        str(tmp_path),
        "--phases",
        "1",
        "--corpora",
        "cuad",
        "--spend-cap-usd",
        "0.01",
    ]
    assert sweep.main([*base, "--no-ingest"]) == 3, "stopped by the spend estimate, not by the wipe guard"
    assert {1, 4, 5, 6} == sweep.INGESTING_PHASES


def test_a_refusal_beside_the_deletion_ends_the_run_with_the_same_code():
    """After the run has started, the instance can change under it. Both _ingest_corpus call sites are
    outside the per-unit handlers, so NotDisposable reaches main. Checked on the parsed source."""
    import ast

    tree = ast.parse(Path(sweep.__file__).read_text())
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    outer = [t for t in main.body if isinstance(t, ast.Try)][-1]
    handler = next(h for h in outer.handlers if ast.unparse(h.type) == "NotDisposable")
    assert ast.unparse(handler.body[-1]) == "return 4"


# ── third review ──


def test_a_refused_start_takes_back_only_what_it_made_never_a_ledger_with_money_in_it(tmp_path, monkeypatch):
    """Found in review. answer-eval writes spend.json to the same run directory and no state.json, so "no
    state.json" did not mean "nothing here". The refused sweep deleted a ledger recording real dollars, and
    the cap's accounting for that run id started again from zero."""
    import json

    monkeypatch.setenv(sweep.DISPOSABLE_ENV, "1")
    run_dir = tmp_path / "results" / "r1"
    run_dir.mkdir(parents=True)
    ledger = run_dir / "spend.json"
    ledger.write_text(json.dumps({"run": {"mode": "answer-eval"}, "total_usd": 1.25, "calls": 300}))
    _stub_instance(monkeypatch, folders=THEIRS)
    base = ["--run-id", "r1", "--workdir", str(tmp_path), "--phases", "1", "--corpora", "cuad"]
    assert sweep.main(base) == 4
    assert json.loads(ledger.read_text())["total_usd"] == 1.25, "the money is still on record"
    assert not (run_dir / "state.json").exists(), "the state this invocation made is taken back"


def test_a_refusal_to_wipe_cannot_be_caught_as_one_bad_unit():
    """Both ingest calls sit outside the run loop's per-unit handlers today. This holds if one is moved."""
    assert not issubclass(sweep.NotDisposable, Exception)


def test_a_unit_the_operator_skips_stays_skipped_even_if_the_sweep_had_skipped_it_first(tmp_path):
    sf = _state(tmp_path, [_unit(4, "qwen35-9b")])
    absent = _Instance([_listed("qwen35-9b", downloaded=False, max_context_here=83968, ram_gb=16.0)])
    sweep._skip_what_this_instance_cannot_run(absent, sf, {4})
    assert sf.skip(sweep._parse_selectors("model=qwen35-9b")) == 1  # the operator's own --skip
    present = _Instance([_listed("qwen35-9b", downloaded=True, max_context_here=83968, ram_gb=16.0)])
    sweep._skip_what_this_instance_cannot_run(present, sf, {4})
    unit = sf.get(4, "cuad", "qwen35-9b", "q1", "standard")
    assert unit.status == Status.SKIPPED and unit.error == "skipped by the operator"

"""The watcher and a database that is not there yet, or goes away (#904, #892).

At start the watcher waits for a reachable database at its Alembic head instead
of crashing (#904). A live event dropped during an outage, or a pass that ended
early, makes its folder pass once when the database is next seen reachable
(#892). Every wait is bounded.
"""

import logging
import socket
import threading
import time
import uuid
from types import SimpleNamespace

import psycopg2
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy import event as sa_event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import sessionmaker

import harbor_clerk.watcher.main as watcher_main
from harbor_clerk.models import Base
from harbor_clerk.models.watched import ROOT_EMPTY
from harbor_clerk.schema_version import schema_head
from harbor_clerk.watcher.events import EventKind, FileEvent
from harbor_clerk.watcher.main import WatcherDaemon
from tests.watcher.test_lifecycle import (
    ACTIVE,
    REMOVED,
    WAIT_SECS,
    _add_folder,
    _outage,
    _root,
    _Running,
    _sha,
    _state,
    _truncate,
    _wait_for,
)

EMPTY_SCHEMA = "hc_watcher_unmigrated"


@pytest.fixture
def factory(_sync_engine):
    return sessionmaker(bind=_sync_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _quiet_daemon(monkeypatch, factory):
    """As in test_lifecycle: no discovery, no periodic pass, a fast root check, clean tables."""
    monkeypatch.setenv("WATCH_ROOT", "")
    monkeypatch.setenv("WATCH_RESCAN_INTERVAL_SECS", "0")
    monkeypatch.setattr(watcher_main, "ROOT_CHECK_INTERVAL_SECS", 0.2)
    _truncate(factory)
    yield
    _truncate(factory)


def _url(engine) -> str:
    return engine.url.render_as_string(hide_password=False)


class _Switch:
    """A session factory whose database the test changes under the daemon: down, unmigrated, then the real one."""

    def __init__(self, factory):
        self.current = factory

    def __call__(self):
        return self.current()


@pytest.fixture
def unmigrated(_sync_engine):
    """A session factory on an empty schema: a database no migration has run on.

    The drops are bounded (lock_timeout): a session the code under test leaked holds a lock on the
    schema, and the drop must then fail the test rather than wait for good.
    """
    with _sync_engine.begin() as conn:
        conn.execute(text("SET LOCAL lock_timeout = '5s'"))
        conn.execute(text(f"DROP SCHEMA IF EXISTS {EMPTY_SCHEMA} CASCADE"))
        conn.execute(text(f"CREATE SCHEMA {EMPTY_SCHEMA}"))
    engine = create_engine(_url(_sync_engine), connect_args={"options": f"-c search_path={EMPTY_SCHEMA}"})
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()
    with _sync_engine.begin() as conn:
        conn.execute(text("SET LOCAL lock_timeout = '5s'"))
        conn.execute(text(f"DROP SCHEMA IF EXISTS {EMPTY_SCHEMA} CASCADE"))


@pytest.fixture
def unreachable():
    """A session factory on a port nothing listens on: connecting is refused at once."""
    engine = create_engine("postgresql+psycopg2://nobody@127.0.0.1:1/nothing", connect_args={"connect_timeout": 2})
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture
def outage(_sync_engine):
    """A daemon's own engine, and a switch that makes every statement fail as an outage does.

    On every engine but the test's own, so the daemon's recovery probe (its own engine) fails too.
    """
    engine = create_engine(_url(_sync_engine))
    down = threading.Event()

    def refuse(conn, cursor, statement, parameters, context, executemany):
        if down.is_set() and conn.engine is not _sync_engine:
            raise psycopg2.OperationalError("server closed the connection unexpectedly")

    sa_event.listen(Engine, "before_cursor_execute", refuse)
    yield SimpleNamespace(factory=sessionmaker(bind=engine, expire_on_commit=False), down=down)
    sa_event.remove(Engine, "before_cursor_execute", refuse)
    engine.dispose()


@pytest.fixture
def stamped(unmigrated):
    """The empty schema with an `alembic_version` table, and a function that stamps it at a revision."""
    engine = unmigrated.kw["bind"]
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE alembic_version (version_num varchar(64) PRIMARY KEY)"))

    def stamp(revision: str) -> None:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM alembic_version"))
            conn.execute(text("INSERT INTO alembic_version VALUES (:rev)"), {"rev": revision})

    return SimpleNamespace(factory=unmigrated, stamp=stamp)


def _wait_in_thread(daemon: WatcherDaemon) -> tuple[threading.Thread, list]:
    """Run `_wait_for_schema` in a thread; the list gets its result, or the exception it raised."""
    out: list = []

    def run():
        try:
            out.append(daemon._wait_for_schema())
        except BaseException as exc:  # noqa: BLE001 - handed to the test
            out.append(exc)

    thread = threading.Thread(target=run, daemon=True, name="test-wait")
    thread.start()
    return thread, out


def _start_in_thread(daemon: WatcherDaemon) -> tuple[threading.Thread, list[BaseException]]:
    errors: list[BaseException] = []

    def run():
        try:
            daemon.start()
        except BaseException as exc:  # noqa: BLE001 - handed to the test
            errors.append(exc)

    thread = threading.Thread(target=run, daemon=True, name="test-start")
    thread.start()
    return thread, errors


def _tries(caplog, needle: str) -> int:
    return sum(needle in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# #904: wait for the schema at start
# ---------------------------------------------------------------------------


def test_missing_tables_is_empty_on_a_migrated_database_and_everything_on_an_unmigrated_one(factory, unmigrated):
    sess = factory()
    try:
        assert watcher_main._missing_tables(sess) == [], "every mapped table is created by the migrations"
    finally:
        sess.close()
    sess = unmigrated()
    try:
        assert watcher_main._missing_tables(sess) == sorted(t.name for t in Base.metadata.sorted_tables)
    finally:
        sess.close()


def test_start_waits_for_a_database_that_is_down_then_unmigrated(
    factory, unmigrated, unreachable, tmp_path, monkeypatch, caplog
):
    """A fresh Docker install: Postgres not accepting yet, then up while `app` has not migrated.

    The watcher logs one line per try, no traceback, and carries on once the
    tables are there. Before #904 `start()` raised UndefinedTable.
    """
    monkeypatch.setattr(watcher_main, "SCHEMA_WAIT_FIRST_SECS", 0.05)
    monkeypatch.setattr(watcher_main, "SCHEMA_WAIT_MAX_SECS", 0.1)
    root = _root(tmp_path)
    (root / "a.txt").write_text("alpha")
    folder_id = _add_folder(factory, root)
    switch = _Switch(unreachable)
    daemon = WatcherDaemon(switch)
    with caplog.at_level(logging.DEBUG, logger="harbor_clerk.watcher"):
        thread, errors = _start_in_thread(daemon)
        try:
            _wait_for("two tries at a database that is down", lambda: _tries(caplog, "waiting for the database (") >= 2)
            switch.current = unmigrated
            _wait_for("two tries at an unmigrated database", lambda: _tries(caplog, "to be migrated") >= 2)
            assert thread.is_alive() and not errors, errors
            assert unmigrated.kw["bind"].pool.checkedout() == 0, "no connection held between tries"
            switch.current = factory
            thread.join(WAIT_SECS)
            assert not thread.is_alive(), "start() never returned once the schema was there"
            assert not errors, errors
            _wait_for("the attach-time pass", lambda: _state(factory, folder_id).get("a.txt"))
        finally:
            daemon.stop()
    watcher_records = [r for r in caplog.records if r.name.startswith("harbor_clerk.watcher")]
    assert not [r for r in watcher_records if r.exc_info], "no traceback while waiting"
    assert not [r for r in watcher_records if r.levelno >= logging.ERROR]
    waiting = [r for r in watcher_records if "waiting for the database" in r.getMessage()]
    assert all(r.levelno in (logging.INFO, logging.WARNING) for r in waiting)
    assert any("ready after" in r.getMessage() for r in watcher_records)


def test_start_does_not_wait_out_a_schema_error_that_is_not_a_missing_table(factory, monkeypatch):
    """A permission refused (or any error but the database down) surfaces at once, as before."""
    refused = type("Coded42501", (psycopg2.ProgrammingError,), {"pgcode": "42501"})("permission denied")

    def failing(_session):
        raise ProgrammingError("SELECT ...", {}, refused)

    monkeypatch.setattr(watcher_main, "_missing_tables", failing)
    monkeypatch.setattr(watcher_main, "_schema_wait", lambda session, _head: failing(session))
    monkeypatch.setattr(watcher_main, "SCHEMA_WAIT_FIRST_SECS", 0.05)
    daemon = WatcherDaemon(factory)
    try:
        thread, errors = _start_in_thread(daemon)
        thread.join(WAIT_SECS)
        assert not thread.is_alive(), "start() waited out an error that is not the database down"
        assert len(errors) == 1 and isinstance(errors[0], ProgrammingError), errors
        assert not daemon._threads, "nothing started"
    finally:
        daemon.stop()


def test_stop_while_waiting_for_the_schema_returns_promptly(unmigrated, monkeypatch):
    monkeypatch.setattr(watcher_main, "SCHEMA_WAIT_FIRST_SECS", 30.0)
    daemon = WatcherDaemon(unmigrated)
    thread, errors = _start_in_thread(daemon)
    time.sleep(0.2)
    daemon.stop()
    thread.join(WAIT_SECS)
    assert not thread.is_alive(), "start() kept waiting after stop()"
    assert not errors and not daemon._threads


@pytest.fixture
def fast_wait(monkeypatch):
    monkeypatch.setattr(watcher_main, "SCHEMA_WAIT_FIRST_SECS", 0.05)
    monkeypatch.setattr(watcher_main, "SCHEMA_WAIT_MAX_SECS", 0.1)


def test_a_pending_migration_is_waited_for_until_the_database_is_at_head(stamped, fast_wait, caplog):
    """An upgrade: every table exists but `app` has not run the new migration (a new column would
    fail every query). The watcher waits while the stored revision is an ancestor of its head."""
    head = schema_head()
    assert head is not None, "the migrations directory is beside the code in the repo"
    older = sorted(head.ancestors - {head.revision})
    if not older:
        pytest.skip("a one-migration chain has no ancestor to stamp")
    stamped.stamp(older[-1])
    daemon = WatcherDaemon(stamped.factory)
    with caplog.at_level(logging.INFO, logger="harbor_clerk.watcher"):
        thread, out = _wait_in_thread(daemon)
        try:
            _wait_for(
                "two tries at a database behind the head", lambda: _tries(caplog, f"head is {head.revision}") >= 2
            )
            assert thread.is_alive() and not out, out
            stamped.stamp(head.revision)
            thread.join(WAIT_SECS)
            assert not thread.is_alive() and out == [True], out
        finally:
            daemon.stop()
            thread.join(WAIT_SECS)
    assert not [r for r in caplog.records if r.exc_info]


def test_a_database_at_head_is_not_waited_for(factory, caplog):
    daemon = WatcherDaemon(factory)
    with caplog.at_level(logging.INFO, logger="harbor_clerk.watcher"):
        assert daemon._wait_for_schema() is True
    assert not [r for r in caplog.records if "waiting" in r.getMessage()]


def test_a_database_at_a_revision_this_build_does_not_know_is_warned_about_once_and_used(stamped, fast_wait, caplog):
    stamped.stamp("not_a_revision_of_this_build")
    daemon = WatcherDaemon(stamped.factory)
    with caplog.at_level(logging.INFO, logger="harbor_clerk.watcher"):
        thread, out = _wait_in_thread(daemon)
        thread.join(WAIT_SECS)
        daemon.stop()
    assert out == [True], "a newer database is not waited out"
    loud = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(loud) == 1 and "does not know" in loud[0].getMessage(), [r.getMessage() for r in loud]


def test_without_a_readable_head_the_tables_are_waited_for(factory, unmigrated, fast_wait, monkeypatch, caplog):
    """No migrations directory beside the code: fall back to every mapped table existing."""
    monkeypatch.setattr(watcher_main, "schema_head", lambda: None)
    switch = _Switch(unmigrated)
    daemon = WatcherDaemon(switch)
    with caplog.at_level(logging.INFO, logger="harbor_clerk.watcher"):
        thread, out = _wait_in_thread(daemon)
        try:
            _wait_for("two tries at missing tables", lambda: _tries(caplog, "table(s) missing") >= 2)
            assert thread.is_alive() and not out, out
            switch.current = factory
            thread.join(WAIT_SECS)
            assert not thread.is_alive() and out == [True], out
        finally:
            daemon.stop()
            thread.join(WAIT_SECS)


# ---------------------------------------------------------------------------
# #892: catch up after an outage
# ---------------------------------------------------------------------------


def test_live_events_during_an_outage_are_caught_up_on_recovery(factory, outage, tmp_path):
    """A delete, a modify and a create while the database is down are applied once it is back,
    with WATCH_RESCAN_INTERVAL_SECS=0 (nothing periodic would ever correct them)."""
    root = _root(tmp_path)
    (root / "gone.txt").write_text("gone")
    (root / "edit.txt").write_text("before")
    folder_id = _add_folder(factory, root)
    run = _Running(factory, folder_id)
    run.daemon = WatcherDaemon(outage.factory)
    with run:
        assert set(run.state()) == {"gone.txt", "edit.txt"}
        outage.down.set()
        (root / "gone.txt").unlink()
        (root / "edit.txt").write_text("after")
        (root / "new.txt").write_text("new")
        _wait_for("a dropped event to queue a catch-up", lambda: folder_id in run.daemon._catch_up)
        time.sleep(0.5)  # several probes fail meanwhile
        st = run.state()
        assert st["gone.txt"][0] == ACTIVE and "new.txt" not in st, "nothing applied while down"

        outage.down.clear()
        run.wait_for(
            "the catch-up pass",
            lambda s: (
                s.get("gone.txt", (None,))[0] == REMOVED
                and s.get("new.txt", (None,))[0] == ACTIVE
                and s.get("edit.txt", (None,) * 4)[3] == _sha("after")
            ),
        )
        assert not run.daemon._catch_up


@pytest.mark.parametrize("ended_at", ["start", "midway"])
def test_a_pass_that_ended_in_the_outage_runs_at_recovery_not_at_its_backoff(factory, tmp_path, monkeypatch, ended_at):
    """A long outage: the aborted pass's retry has backed off far ahead; recovery runs it now, once.

    Ended at its start (the first query fails) or midway (a dispatch fails: `_DatabaseUnavailable`).
    """
    root = _root(tmp_path)
    (root / "a.txt").write_text("alpha")
    folder_id = _add_folder(factory, root)
    daemon = WatcherDaemon(factory)

    def down(*_args):
        raise _outage()

    if ended_at == "start":
        monkeypatch.setattr(daemon, "_active_paths", down)
    else:
        monkeypatch.setattr(watcher_main, "handle_event", down)
    for _ in range(6):
        daemon._reconcile_folder(folder_id, str(root))
    assert daemon._retry_delay[folder_id] == watcher_main.RETRY_MAX_SECS
    assert daemon._retry_at[folder_id] > time.monotonic() + 100
    assert daemon._catch_up == {folder_id}

    requested = []
    monkeypatch.setattr(daemon, "_request_reconcile", requested.append)
    daemon._note_db_ok()
    assert requested == [folder_id], "recovery passes the folder at once"
    assert folder_id not in daemon._retry_at, "the catch-up pass is the retry"
    daemon._note_db_ok()
    assert requested == [folder_id], "once per recovery"


def test_a_flapping_database_requests_at_most_one_pass_per_recovery(factory, monkeypatch):
    daemon = WatcherDaemon(factory)
    folder_id = uuid.uuid4()
    requested = []
    monkeypatch.setattr(daemon, "_request_reconcile", requested.append)

    # Many drops in one outage: one pass.
    for _ in range(5):
        daemon._note_db_failure(_outage())
        daemon._need_catch_up(folder_id)
    daemon._note_db_ok()
    assert requested == [folder_id]

    # Flaps with nothing dropped: no pass.
    for _ in range(10):
        daemon._note_db_failure(_outage())
        daemon._note_db_ok()
    assert requested == [folder_id]

    # A drop in each of three outages: one pass per recovery, never more.
    for _ in range(3):
        daemon._note_db_failure(_outage())
        daemon._need_catch_up(folder_id)
        daemon._need_catch_up(folder_id)
        daemon._note_db_ok()
    assert requested == [folder_id] * 4


def test_recovery_during_a_running_pass_makes_exactly_one_more(factory, tmp_path):
    """The running pass may have walked past a file whose event was dropped: it runs once more, after."""
    folder_id = uuid.uuid4()
    daemon = WatcherDaemon(factory)
    daemon._observers[folder_id] = SimpleNamespace(root=str(tmp_path))
    release, entered = threading.Event(), threading.Event()
    calls = []

    def fake_reconcile(fid, root):
        calls.append(fid)
        if len(calls) == 1:
            entered.set()
            assert release.wait(WAIT_SECS), "the test never released the first pass"

    daemon._reconcile_folder = fake_reconcile
    daemon._request_reconcile(folder_id)
    assert entered.wait(WAIT_SECS), "the first pass never started"
    daemon._note_db_failure(_outage())
    daemon._need_catch_up(folder_id)
    daemon._note_db_ok()
    daemon._note_db_failure(_outage())  # and it flaps while the pass still runs
    daemon._need_catch_up(folder_id)
    daemon._note_db_ok()
    release.set()
    _wait_for("the worker to finish", lambda: folder_id not in daemon._reconciling)
    assert calls == [folder_id, folder_id], calls


def test_an_event_dropped_as_recovery_is_seen_is_not_lost(factory, monkeypatch):
    """A drop recorded after another thread already saw the database back gets its pass now;
    one recorded in a new outage waits for that outage's recovery."""
    daemon = WatcherDaemon(factory)
    first, second = uuid.uuid4(), uuid.uuid4()
    requested = []
    monkeypatch.setattr(daemon, "_request_reconcile", requested.append)

    daemon._note_db_failure(_outage())
    daemon._note_db_ok()  # another thread's success ends the outage first
    daemon._need_catch_up(first)  # then this thread records its drop
    assert requested == [first], "no recovery is coming for it: passed now"

    daemon._note_db_failure(_outage())
    daemon._need_catch_up(second)
    assert requested == [first] and daemon._catch_up == {second}
    daemon._note_db_ok()
    assert requested == [first, second]


def test_the_monitor_probes_only_while_a_folder_waits_for_recovery(factory, outage, monkeypatch):
    daemon = WatcherDaemon(outage.factory)
    requested = []
    monkeypatch.setattr(daemon, "_request_reconcile", requested.append)
    statements = []
    real = outage.factory

    def counting():
        statements.append(1)
        return real()

    daemon._session_factory = counting
    daemon._monitor_tick(interval=0, next_rescan=0.0)
    assert statements == [], "no outage, no probe"

    folder_id = uuid.uuid4()
    outage.down.set()
    daemon._note_db_failure(_outage())
    daemon._need_catch_up(folder_id)
    for _ in range(3):
        daemon._monitor_tick(interval=0, next_rescan=0.0)
    assert len(statements) == 3 and requested == [], "probed each tick, still down"

    outage.down.clear()
    daemon._monitor_tick(interval=0, next_rescan=0.0)
    assert requested == [folder_id]
    daemon._monitor_tick(interval=0, next_rescan=0.0)
    assert len(statements) == 4, "recovered: no more probes"


def test_a_root_empty_clear_lost_to_an_outage_is_caught_up(outage):
    """A live event indexed a file in a `root_empty` folder; clearing the reason fails in the outage."""
    daemon = WatcherDaemon(outage.factory)
    folder_id = uuid.uuid4()
    daemon._reasons[folder_id] = ROOT_EMPTY
    outage.down.set()
    daemon._clear_root_empty_if_indexed(FileEvent(EventKind.created, folder_id, "a.txt", "/nowhere/a.txt"))
    assert daemon._catch_up == {folder_id}, "the catch-up pass clears it once the database is back"


def test_an_observer_sync_is_a_recovery(factory, monkeypatch):
    """The listener's resync after it reconnects (or discovery's) sees the database back first."""
    daemon = WatcherDaemon(factory)
    folder_id = uuid.uuid4()
    requested = []
    monkeypatch.setattr(daemon, "_request_reconcile", requested.append)
    daemon._note_db_failure(_outage())
    daemon._need_catch_up(folder_id)
    daemon._sync_observers()  # no folder rows: starts nothing, requests nothing of its own
    assert requested == [folder_id] and not daemon._catch_up


def test_a_folder_removed_during_the_outage_gets_no_catch_up_pass(factory, monkeypatch):
    daemon = WatcherDaemon(factory)
    folder_id = uuid.uuid4()
    daemon._observers[folder_id] = SimpleNamespace(stop=lambda: None, root="/nowhere")
    requested = []
    monkeypatch.setattr(daemon, "_request_reconcile", requested.append)
    daemon._note_db_failure(_outage())
    daemon._need_catch_up(folder_id)
    daemon._sync_observers()  # its row is gone: the observer is stopped, then the recovery is noted
    assert folder_id not in daemon._observers
    assert requested == [] and not daemon._catch_up


def test_the_probe_gives_up_on_a_host_that_never_answers(monkeypatch):
    """A blackholed host: the TCP connect completes (the kernel's backlog) and nothing ever answers.

    Without a connect timeout the probe, and with it the monitor thread, waits for good."""
    monkeypatch.setattr(watcher_main, "PROBE_CONNECT_TIMEOUT_SECS", 2)
    silent = socket.socket()
    silent.bind(("127.0.0.1", 0))
    silent.listen(8)
    port = silent.getsockname()[1]
    engine = create_engine(f"postgresql+psycopg2://nobody@127.0.0.1:{port}/nothing")
    daemon = WatcherDaemon(sessionmaker(bind=engine))
    daemon._note_db_failure(_outage())
    daemon._need_catch_up(uuid.uuid4())
    started = time.monotonic()
    tick = threading.Thread(target=daemon._monitor_tick, args=(0, 0.0), daemon=True, name="test-tick")
    try:
        tick.start()
        tick.join(15)
        assert not tick.is_alive(), "the probe was still connecting after 15 s"
        assert time.monotonic() - started < 10
        assert daemon._db_streak.failures == 2, "the timeout counts as the database still unreachable"
        assert daemon._catch_up, "still waiting for a recovery"
    finally:
        silent.close()
        tick.join(WAIT_SECS)
        engine.dispose()


def test_an_unexpected_probe_error_is_contained_and_the_tick_goes_on(factory, monkeypatch, caplog):
    daemon = WatcherDaemon(factory)
    waiting, retried = uuid.uuid4(), uuid.uuid4()
    daemon._note_db_failure(_outage())
    daemon._need_catch_up(waiting)

    def broken(_url):
        raise RuntimeError("the probe broke")

    monkeypatch.setattr(daemon, "_probe_engine", broken)
    requested, synced = [], []
    monkeypatch.setattr(daemon, "_request_reconcile", requested.append)
    monkeypatch.setattr(daemon, "_sync_observers", lambda: synced.append(1))
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="harbor_clerk.watcher"):
        for _ in range(4):
            daemon._retry_at[retried] = 0.0
            daemon._monitor_tick(interval=0.001, next_rescan=0.0)
    assert requested == [retried] * 4, "the due retries ran on every tick"
    assert len(synced) == 4, "and the periodic pass"
    assert daemon._catch_up == {waiting}, "a failed probe is not a recovery"
    assert not any("monitor pass failed" in r.getMessage() for r in caplog.records)
    loud = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(loud) == 1 and "probe failed" in loud[0].getMessage(), [r.getMessage() for r in loud]

"""The watcher and a database that is not there yet, or goes away (#904, #892).

At start the watcher waits for a reachable, migrated database instead of
crashing (#904). A live event dropped during an outage, or a pass that ended
early, makes its folder pass once when the database is next seen reachable
(#892). Every wait is bounded.
"""

import logging
import threading
import time
import uuid
from types import SimpleNamespace

import psycopg2
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy import event as sa_event
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.orm import sessionmaker

import harbor_clerk.watcher.main as watcher_main
from harbor_clerk.models import Base
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
    """A session factory on an empty schema: a database no migration has run on."""
    with _sync_engine.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {EMPTY_SCHEMA} CASCADE"))
        conn.execute(text(f"CREATE SCHEMA {EMPTY_SCHEMA}"))
    engine = create_engine(_url(_sync_engine), connect_args={"options": f"-c search_path={EMPTY_SCHEMA}"})
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()
    with _sync_engine.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {EMPTY_SCHEMA} CASCADE"))


@pytest.fixture
def unreachable():
    """A session factory on a port nothing listens on: connecting is refused at once."""
    engine = create_engine("postgresql+psycopg2://nobody@127.0.0.1:1/nothing", connect_args={"connect_timeout": 2})
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture
def outage(_sync_engine):
    """A daemon's own engine, and a switch that makes every statement on it fail as an outage does."""
    engine = create_engine(_url(_sync_engine))
    down = threading.Event()

    def refuse(conn, cursor, statement, parameters, context, executemany):
        if down.is_set():
            raise psycopg2.OperationalError("server closed the connection unexpectedly")

    sa_event.listen(engine, "before_cursor_execute", refuse)
    yield SimpleNamespace(factory=sessionmaker(bind=engine, expire_on_commit=False), down=down)
    sa_event.remove(engine, "before_cursor_execute", refuse)
    engine.dispose()


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

import threading
import time
import uuid

from harbor_clerk.watcher.db_listener import listen_for_folder_changes
from harbor_clerk.watcher.notify import notify_folder_change


def test_notify_received_by_listener(sync_session):
    received: list[dict] = []
    stop = threading.Event()

    def loop():
        for payload in listen_for_folder_changes(stop_event=stop, poll_interval=0.05):
            received.append(payload)

    t = threading.Thread(target=loop, daemon=True)
    t.start()

    # Give the LISTEN time to install on its own connection before firing
    time.sleep(0.3)

    folder_id = uuid.uuid4()
    notify_folder_change(sync_session, folder_id, action="added")
    sync_session.commit()  # NOTIFY delivered on commit

    deadline = time.time() + 3.0
    while not received and time.time() < deadline:
        time.sleep(0.05)

    stop.set()
    t.join(timeout=2.0)

    assert received, "listener did not receive any payload within deadline"
    assert received[0]["folder_id"] == str(folder_id)
    assert received[0]["action"] == "added"


def test_listener_yields_parsed_dict(sync_session):
    """Multiple actions in sequence are received in order."""
    received: list[dict] = []
    stop = threading.Event()

    def loop():
        for payload in listen_for_folder_changes(stop_event=stop, poll_interval=0.05):
            received.append(payload)

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    time.sleep(0.3)

    fid_a = uuid.uuid4()
    fid_b = uuid.uuid4()
    notify_folder_change(sync_session, fid_a, action="added")
    sync_session.commit()
    notify_folder_change(sync_session, fid_b, action="disabled")
    sync_session.commit()

    deadline = time.time() + 3.0
    while len(received) < 2 and time.time() < deadline:
        time.sleep(0.05)

    stop.set()
    t.join(timeout=2.0)

    assert len(received) >= 2
    actions = [p["action"] for p in received[:2]]
    assert actions == ["added", "disabled"]


def test_on_listening_runs_once_listen_is_in_place(sync_session):
    """A reconnecting caller resyncs in `on_listening`; a NOTIFY sent from inside
    it must still arrive, or a change made during the resync would be lost."""
    received: list[dict] = []
    stop = threading.Event()
    folder_id = uuid.uuid4()

    def _notify_from_callback():
        notify_folder_change(sync_session, folder_id, action="enabled")
        sync_session.commit()

    def loop():
        for payload in listen_for_folder_changes(
            stop_event=stop, poll_interval=0.05, on_listening=_notify_from_callback
        ):
            received.append(payload)

    t = threading.Thread(target=loop, daemon=True)
    t.start()

    deadline = time.time() + 3.0
    while not received and time.time() < deadline:
        time.sleep(0.05)
    stop.set()
    t.join(timeout=2.0)

    assert received and received[0]["folder_id"] == str(folder_id)


def test_connection_is_closed_when_setup_fails(monkeypatch, _sync_engine):
    """The watcher now reconnects after a failure, so a failure between
    connecting and the first wait must not leak the connection each retry."""
    import pytest

    from harbor_clerk.watcher import db_listener

    opened = []
    real_open = db_listener._open_listen_connection

    def _tracking_open():
        conn = real_open()
        opened.append(conn)
        return conn

    def _resync_fails():
        raise RuntimeError("database went away mid-resync")

    monkeypatch.setattr(db_listener, "_open_listen_connection", _tracking_open)
    with pytest.raises(RuntimeError):
        next(listen_for_folder_changes(stop_event=threading.Event(), on_listening=_resync_fails))

    assert opened and opened[0].closed, "listener connection leaked when setup failed"


def test_connection_is_closed_when_listen_is_refused(monkeypatch):
    """LISTEN itself refused on a half-dead connection: the listener retries,
    so each refusal must close the connection rather than leak it."""
    import pytest

    from harbor_clerk.watcher import db_listener

    class _Cursor:
        def execute(self, sql):
            if sql.startswith("LISTEN"):
                raise RuntimeError("server closed the connection unexpectedly")

        def close(self):
            pass

    class _Conn:
        closed = False

        def cursor(self):
            return _Cursor()

        def close(self):
            self.closed = True

    conn = _Conn()
    monkeypatch.setattr(db_listener, "_open_listen_connection", lambda: conn)
    with pytest.raises(RuntimeError, match="server closed"):
        next(listen_for_folder_changes(stop_event=threading.Event()))

    assert conn.closed, "a refused LISTEN leaked the connection"

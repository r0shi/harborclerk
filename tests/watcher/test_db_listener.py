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

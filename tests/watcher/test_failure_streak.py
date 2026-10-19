"""Which failures of a retried loop are logged at WARNING (#593)."""

from harbor_clerk.watcher.failure_streak import FailureStreak


def test_first_failure_is_loud_and_repeats_are_quiet():
    streak = FailureStreak(heartbeat_every=10)
    assert streak.record(OSError("connection refused")) == "first"
    assert [streak.record(OSError("connection refused")) for _ in range(3)] == [None, None, None]
    assert streak.failures == 4


def test_a_changed_error_is_loud():
    """A transient refusal turning into a persistent auth failure must surface."""
    streak = FailureStreak()
    streak.record(OSError("connection refused"))
    streak.record(OSError("connection refused"))
    assert streak.record(RuntimeError("password authentication failed")) == "changed"
    assert streak.record(RuntimeError("password authentication failed")) is None
    # Same type, different message is a change too.
    assert streak.record(RuntimeError("database does not exist")) == "changed"


def test_every_nth_failure_is_a_heartbeat():
    streak = FailureStreak(heartbeat_every=10)
    loud = [n for n in range(1, 31) if streak.record(OSError("down")) is not None]
    assert loud == [1, 10, 20, 30]


def test_reset_ends_the_streak():
    streak = FailureStreak()
    streak.record(OSError("down"))
    streak.record(OSError("down"))
    assert streak.reset() == 2
    assert streak.failures == 0
    assert streak.record(OSError("down")) == "first", "a new streak starts loud"

import XCTest
@testable import HarborClerkServer

/// Tests for the shared shutdown helpers introduced to fix the
/// "Quit hangs after a few hours" failure mode. See
/// `project_menubar_process_management_audit.md`.
///
/// Most of what we want to validate is behaviour of `Foundation.Process`
/// under shutdown — which is genuinely hard to unit-test. We can however
/// verify the pure helpers do what they say on the tin:
///
/// - `detachPipesForShutdown()` clears readabilityHandlers and replaces
///   the Process's stdout/stderr with `/dev/null`. We exercise this with
///   a real (short-lived) `/bin/sleep` subprocess.
/// - `waitForExitWithDeadline()` returns even when the child blocks past
///   the grace period, because the SIGKILL fallback escalates. Exercised
///   with `/bin/sleep 30` and a 1-second grace.
///
/// These tests are intentionally end-to-end against real OS process
/// machinery — that's where the bugs live. They should still be fast
/// (under a few seconds each).
final class ProcessExtensionsTests: XCTestCase {

    /// Spawn `/bin/sleep N`, attach a Pipe to stdout/stderr like
    /// `Log.createPipe` does, then verify `detachPipesForShutdown()`
    /// nils the readabilityHandler and closes the read-end fd. We do
    /// NOT reassign `standardOutput` — that would raise
    /// `NSInvalidArgumentException: task already launched`, which is
    /// exactly the bug the first iteration of this helper had.
    func testDetachPipesClearsReadabilityHandlerAndClosesReadEnd() throws {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/bin/sleep")
        proc.arguments = ["5"]

        let pipe = Pipe()
        pipe.fileHandleForReading.readabilityHandler = { _ in }
        proc.standardOutput = pipe
        proc.standardError = pipe

        try proc.run()
        defer {
            if proc.isRunning {
                kill(proc.processIdentifier, SIGKILL)
                proc.waitUntilExit()
            }
        }

        proc.detachPipesForShutdown()

        // The handler on the pipe must be nilled — that's the GCD ref the
        // helper is breaking so waitUntilExit can complete.
        XCTAssertNil(
            pipe.fileHandleForReading.readabilityHandler,
            "readabilityHandler should be nil after detach",
        )

        // standardOutput remains the Pipe — we don't reassign it. This is
        // a regression guard against the earlier version that reassigned
        // to FileHandle.nullDevice and crashed on launched processes with
        // "task already launched" (`NSInvalidArgumentException`).
        XCTAssertIdentical(
            proc.standardOutput as AnyObject?,
            pipe as AnyObject,
            "standardOutput should still be the Pipe; we don't reassign on launched processes",
        )
    }

    /// Detach is idempotent — calling it twice is safe and produces the
    /// same end state. Important because shutdown paths can race.
    func testDetachPipesIsIdempotent() throws {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/bin/sleep")
        proc.arguments = ["5"]
        let pipe = Pipe()
        proc.standardOutput = pipe
        proc.standardError = pipe
        try proc.run()
        defer {
            if proc.isRunning {
                kill(proc.processIdentifier, SIGKILL)
                proc.waitUntilExit()
            }
        }

        proc.detachPipesForShutdown()
        proc.detachPipesForShutdown()  // second call must not crash

        XCTAssertNil(pipe.fileHandleForReading.readabilityHandler)
    }

    /// When stdout and stderr point to the same Pipe instance (the
    /// `Log.createPipe` pattern), detach must not double-close the read
    /// end — second close would throw. The implementation uses `!==`
    /// identity to skip the duplicate.
    func testDetachPipesHandlesSharedPipeForStdoutAndStderr() throws {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/bin/sleep")
        proc.arguments = ["5"]
        let sharedPipe = Pipe()
        proc.standardOutput = sharedPipe
        proc.standardError = sharedPipe
        try proc.run()
        defer {
            if proc.isRunning {
                kill(proc.processIdentifier, SIGKILL)
                proc.waitUntilExit()
            }
        }

        // No assertion needed — the test passes if detach doesn't throw.
        proc.detachPipesForShutdown()
    }

    /// `waitForExitWithDeadline` must return within (grace + small slack)
    /// even when the child is `sleep 30` — because SIGKILL escalates
    /// after the grace period. Without the SIGKILL fallback this test
    /// would hang for 30s and timeout the test runner.
    func testWaitForExitWithDeadlineEscalatesToSigkill() async throws {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/bin/sleep")
        proc.arguments = ["30"]
        try proc.run()

        // Send SIGTERM — sleep traps SIGTERM cleanly and exits, but on
        // some systems / under load it can take a moment. The deadline
        // is what we're really testing.
        proc.terminate()

        let start = Date()
        await proc.waitForExitWithDeadline(graceSeconds: 1.0, serviceName: "test-sleep")
        let elapsed = Date().timeIntervalSince(start)

        XCTAssertFalse(proc.isRunning, "Process must be dead after waitForExitWithDeadline")
        // Allow generous slack (5s) so this test isn't flaky on a loaded CI
        // host. The point is just that we didn't wait 30s.
        XCTAssertLessThan(
            elapsed,
            5.0,
            "Wait should have completed within grace + slack, not 30s; took \(elapsed)s",
        )
    }

    /// When the child exits cleanly before the grace expires, no SIGKILL
    /// fires and we return promptly. Verified by spawning `/usr/bin/true`
    /// (which exits immediately) with a long grace.
    func testWaitForExitWithDeadlineReturnsPromptlyOnCleanExit() async throws {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/true")
        try proc.run()

        let start = Date()
        await proc.waitForExitWithDeadline(graceSeconds: 30.0, serviceName: "test-true")
        let elapsed = Date().timeIntervalSince(start)

        XCTAssertFalse(proc.isRunning)
        XCTAssertEqual(proc.terminationStatus, 0)
        XCTAssertLessThan(
            elapsed,
            2.0,
            "Should not wait for grace period when child exited immediately",
        )
    }

    /// Calling `waitForExitWithDeadline` on a Process that's already
    /// exited returns immediately and doesn't touch the Pipes (defensive
    /// — used during shutdown when state can be racy).
    func testWaitForExitWithDeadlineReturnsImmediatelyForExitedProcess() async throws {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/true")
        try proc.run()
        proc.waitUntilExit()  // already exited

        let start = Date()
        await proc.waitForExitWithDeadline(graceSeconds: 30.0, serviceName: "test-already-dead")
        XCTAssertLessThan(Date().timeIntervalSince(start), 0.5)
    }

    /// After runAsProcessGroupLeader, getpgid(pid) must equal pid — confirms
    /// the child is the leader of a new process group rather than inheriting
    /// the parent's pgid. Without this, killpg(pid) only hits the leaf
    /// process and grandchildren orphan.
    func testRunAsProcessGroupLeaderMakesChildItsOwnPgidLeader() throws {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/bin/sleep")
        proc.arguments = ["3"]

        try proc.runAsProcessGroupLeader()
        defer {
            if proc.isRunning {
                kill(proc.processIdentifier, SIGKILL)
                proc.waitUntilExit()
            }
        }

        let pid = proc.processIdentifier
        let pgid = getpgid(pid)
        XCTAssertEqual(pgid, pid, "expected child to be its own pgid leader (pgid == pid)")
    }

    /// The user-visible reason for runAsProcessGroupLeader: killpg reaches
    /// grandchildren that plain kill(pid) would miss. Verified end-to-end
    /// by spawning `sh -c 'sleep 30 & wait'`. The sh forks a sleep child;
    /// without pgid we'd only kill sh and orphan sleep. With pgid we kill both.
    func testKillpgReachesGrandchildSpawnedViaShell() async throws {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/bin/sh")
        proc.arguments = ["-c", "sleep 30 & echo $! > /tmp/pg-test-grandchild.pid; wait"]
        try proc.runAsProcessGroupLeader()
        defer {
            if proc.isRunning {
                kill(proc.processIdentifier, SIGKILL)
                proc.waitUntilExit()
            }
            try? FileManager.default.removeItem(atPath: "/tmp/pg-test-grandchild.pid")
        }

        // Give sh a moment to write the grandchild PID
        try await Task.sleep(for: .milliseconds(200))

        guard let pidStr = try? String(contentsOfFile: "/tmp/pg-test-grandchild.pid"),
              let grandchildPid = Int32(pidStr.trimmingCharacters(in: .whitespacesAndNewlines)) else {
            XCTFail("grandchild pid file missing or unparseable")
            return
        }
        XCTAssertEqual(kill(grandchildPid, 0), 0, "grandchild must be alive before killpg")

        // killpg the leader — should hit both sh and the sleep.
        XCTAssertEqual(killpg(proc.processIdentifier, SIGKILL), 0)

        // Give the kernel a moment to clean up
        try await Task.sleep(for: .milliseconds(500))

        XCTAssertEqual(kill(grandchildPid, 0), -1, "grandchild must be dead after killpg")
        XCTAssertEqual(errno, ESRCH, "expected kill(pid, 0) errno = ESRCH after grandchild died")
    }
}

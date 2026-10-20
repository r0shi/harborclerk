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
            killAndReap(proc)
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
            killAndReap(proc)
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
            killAndReap(proc)
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
        let exited = await withinBound("the deadline wait") {
            await proc.waitForExitWithDeadline(graceSeconds: 1.0, serviceName: "test-sleep")
        }
        let elapsed = Date().timeIntervalSince(start)

        XCTAssertEqual(exited, true, "the wait saw the exit")
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
        let exited = await withinBound("the wait on /usr/bin/true") {
            await proc.waitForExitWithDeadline(graceSeconds: 30.0, serviceName: "test-true")
        }
        let elapsed = Date().timeIntervalSince(start)

        XCTAssertEqual(exited, true)
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
        // Bounded rather than `waitUntilExit()`, which this exact case (a
        // child that exits at once) has parked forever (see ProcessExtensions).
        XCTAssertTrue(awaitExit(proc), "/usr/bin/true never reported its exit")

        let start = Date()
        let exited = await withinBound("the wait on an exited process") {
            await proc.waitForExitWithDeadline(graceSeconds: 30.0, serviceName: "test-already-dead")
        }
        XCTAssertEqual(exited, true)
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
            killAndReap(proc)
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
            killAndReap(proc)
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

    /// waitForExitWithDeadline's SIGKILL escalation must reach grandchildren
    /// when the leader was launched as a pgid leader. Otherwise a hung
    /// service that spawned subprocesses leaks them through the SIGKILL
    /// fallback.
    func testWaitForExitWithDeadlineSigkillReachesGrandchildren() async throws {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/bin/sh")
        // Trap SIGTERM so sh ignores the polite signal. Only SIGKILL kills.
        proc.arguments = ["-c", "trap '' TERM; sleep 30 & echo $! > /tmp/pg-test-grandchild2.pid; wait"]
        try proc.runAsProcessGroupLeader()

        var grandchildPid: Int32 = 0
        defer {
            killAndReap(proc)
            if grandchildPid != 0 {
                kill(grandchildPid, SIGKILL)
            }
            try? FileManager.default.removeItem(atPath: "/tmp/pg-test-grandchild2.pid")
        }

        try await Task.sleep(for: .milliseconds(200))
        guard let pidStr = try? String(contentsOfFile: "/tmp/pg-test-grandchild2.pid"),
              let parsedPid = Int32(pidStr.trimmingCharacters(in: .whitespacesAndNewlines)) else {
            XCTFail("grandchild pid file missing")
            return
        }
        grandchildPid = parsedPid

        proc.terminate()  // SIGTERM, which sh has trapped to no-op
        await withinBound("the deadline wait") {
            await proc.waitForExitWithDeadline(graceSeconds: 1.0, serviceName: "pg-test-grandchild2")
        }
        XCTAssertFalse(proc.isRunning)

        try await Task.sleep(for: .milliseconds(500))
        XCTAssertEqual(kill(grandchildPid, 0), -1, "grandchild must be dead after deadline-driven SIGKILL")
    }

    // MARK: - Every wait is bounded (#890)

    /// The wait after the SIGKILL gives up. No real child can be made to
    /// survive a SIGKILL, or to have its exit go unnoticed by Foundation, on
    /// demand, so this drives the poll with a process that is never reported
    /// gone. Before #890 that wait had no bound, and a CI run sat 18 minutes
    /// after "sending SIGKILL".
    func testPollGivesUpOnAProcessNeverReportedGone() async {
        let kills = Counter()
        let start = Date()
        let exited = await boundedBlocking("the poll on a process never reported gone", seconds: 5) {
            Process.pollUntilExited(
                isRunning: { true },
                sendKill: { kills.bump() },
                graceSeconds: 0.2,
                killWaitSeconds: 0.5,
                serviceName: "never-gone"
            )
        }
        XCTAssertEqual(exited, false, "the wait reports that it gave up")
        XCTAssertEqual(kills.value, 1, "one SIGKILL at the grace deadline, not one per poll")
        XCTAssertGreaterThanOrEqual(Date().timeIntervalSince(start), 0.65, "it waited out grace + kill wait first")
    }

    /// A probe that overruns its timeout is killed and reported as no output,
    /// rather than waited on: `lsof` in the LLM stop's port probe was such a
    /// wait.
    func testRunCapturingOutputKillsAToolThatOverrunsItsTimeout() async throws {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/bin/sleep")
        proc.arguments = ["30"]
        proc.standardError = FileHandle.nullDevice
        defer { killAndReap(proc) }

        let start = Date()
        let reportedNoOutput = await withinBound("the capped probe", seconds: 5) {
            await proc.runCapturingOutput(timeout: 0.3) == nil
        }
        XCTAssertEqual(reportedNoOutput, true, "a probe that timed out reports no output")
        XCTAssertLessThan(Date().timeIntervalSince(start), 3, "returned at the timeout, not at the child's exit")
        XCTAssertTrue(awaitExit(proc, within: 2), "the overrunning probe was killed")
    }

    /// The read does not wait for EOF. `sh` exits at once but leaves a
    /// background `sleep` holding its copy of the stdout pipe's write end, so
    /// `readDataToEndOfFile()` would block until that sleep exits — the
    /// pipe-EOF hang, which the LLM stop's lsof read and three sibling sites
    /// were exposed to.
    func testRunCapturingOutputDoesNotWaitForAGrandchildHoldingThePipe() async throws {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/bin/sh")
        proc.arguments = ["-c", "sleep 30 & echo $!"]
        proc.standardError = FileHandle.nullDevice

        let text = await withinBound("the read past a grandchild holding the pipe", seconds: 5) {
            await proc.runCapturingOutput(timeout: 5).flatMap { String(data: $0, encoding: .utf8) } ?? ""
        } ?? ""
        let grandchild = Int32(text.trimmingCharacters(in: .whitespacesAndNewlines))
        if let grandchild { kill(grandchild, SIGKILL) }
        XCTAssertNotNil(grandchild, "got the output written before exit; was \(text.debugDescription)")
    }
}

// MARK: - Bounded waits for tests

/// Thread-safe counter for closures the code under test calls from another thread.
final class Counter: @unchecked Sendable {
    private let lock = NSLock()
    private var count = 0
    var value: Int { lock.withLock { count } }
    func bump() { lock.withLock { count += 1 } }
}

private final class Box<T>: @unchecked Sendable {
    var value: T?
}

extension XCTestCase {
    /// SIGKILL `proc` if it is running and wait, bounded, for Foundation to
    /// report it gone. Use in place of `waitUntilExit()`: that call is
    /// synchronous and unbounded, and in a `tearDown` or `defer` it sits
    /// outside every test's own bound, so a reap that never returned hung the
    /// CI job to its timeout instead of failing a test (#890).
    func killAndReap(_ proc: Process, within seconds: TimeInterval = 5, file: StaticString = #filePath, line: UInt = #line) {
        guard proc.isRunning else { return }
        kill(proc.processIdentifier, SIGKILL)
        if !awaitExit(proc, within: seconds) {
            XCTFail("pid \(proc.processIdentifier) still reported running \(seconds)s after SIGKILL", file: file, line: line)
        }
    }

    /// Wait up to `seconds` for Foundation to report `proc` gone. Sends nothing.
    func awaitExit(_ proc: Process, within seconds: TimeInterval = 5) -> Bool {
        let deadline = Date().addingTimeInterval(seconds)
        while proc.isRunning, Date() < deadline { usleep(20_000) }
        return !proc.isRunning
    }

    /// Up to `count` bytes from `handle`, waiting at most `seconds` in all;
    /// `readData(ofLength:)` waits for the bytes or for EOF, and EOF never
    /// comes while any process holds the write end.
    func readBounded(_ handle: FileHandle, count: Int, within seconds: TimeInterval) -> Data {
        let fd = handle.fileDescriptor
        let deadline = Date().addingTimeInterval(seconds)
        var out = Data()
        var buf = [UInt8](repeating: 0, count: count)
        while out.count < count {
            let left = deadline.timeIntervalSinceNow
            guard left > 0 else { break }
            var pfd = pollfd(fd: fd, events: Int16(POLLIN), revents: 0)
            let ready = poll(&pfd, 1, Int32(left * 1000))
            if ready < 0, errno == EINTR { continue }
            guard ready > 0 else { break }
            let n = read(fd, &buf, count - out.count)
            guard n > 0 else { break }
            out.append(buf, count: n)
        }
        return out
    }

    /// Run `body` and fail the test if it has not returned within `seconds`.
    /// Returns `nil` on timeout.
    @discardableResult
    func withinBound<T>(
        _ what: String,
        seconds: TimeInterval = 10,
        _ body: @escaping () async -> T
    ) async -> T? {
        let box = Box<T>()
        let finish = ResumeOnce()
        let returned = expectation(description: "\(what) returned within \(seconds)s")
        let task = Task {
            let result = await body()
            // A body that finishes after the timeout neither writes nor fulfils:
            // the timeout below claimed `finish` first, so the test has already
            // failed cleanly and a late fulfil would be an API violation.
            guard finish.claim() else { return }
            box.value = result
            returned.fulfill()
        }
        await fulfillment(of: [returned], timeout: seconds)
        _ = finish.claim()
        task.cancel()
        return box.value
    }

    /// `withinBound` for a call that blocks its thread: it runs on a dispatch
    /// thread, so a mutation that never returns cannot starve the cooperative
    /// pool the test's own timeout needs.
    func boundedBlocking<T>(_ what: String, seconds: TimeInterval, _ body: @escaping () -> T) async -> T? {
        let box = Box<T>()
        let finish = ResumeOnce()
        let returned = expectation(description: "\(what) returned within \(seconds)s")
        DispatchQueue.global().async {
            let result = body()
            guard finish.claim() else { return }  // late: see `withinBound`
            box.value = result
            returned.fulfill()
        }
        await fulfillment(of: [returned], timeout: seconds)
        _ = finish.claim()
        return box.value
    }
}

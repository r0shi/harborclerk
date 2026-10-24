import XCTest
@testable import HarborClerkServer

/// `LlamaService.stop()` against real child processes: it is resource-cleanup
/// code, and the two defects it closes (#689) were only ever visible with a
/// process to lose. `/bin/sleep` stands in for llama-server; the port the stop
/// probes for stragglers is one nothing on this machine holds, because the
/// default is the live app's llama-server and the probe kills what it finds.
final class LlamaServiceStopTests: XCTestCase {

    private var spawned: [Process] = []

    override func tearDown() {
        // Bounded, not `waitUntilExit()`: tearDown is synchronous and outside
        // every test's own bound, so a reap that never returned here hung the
        // CI job to its 20-minute timeout instead of failing (#890).
        for proc in spawned { killAndReap(proc) }
        spawned = []
        super.tearDown()
    }

    /// A child in its own process group, as `LlamaService.start()` launches
    /// llama-server. `ignoringSigterm` keeps it alive through the grace period
    /// so the stop is measurably in flight (SIG_IGN survives the exec).
    private func spawnSleeper(ignoringSigterm: Bool = false) throws -> Process {
        let proc = Process()
        proc.standardError = FileHandle.nullDevice
        if ignoringSigterm {
            // The trap must be installed before the test's SIGTERM arrives, or
            // the shell dies to it like any other child; "ready" says it is.
            proc.executableURL = URL(fileURLWithPath: "/bin/sh")
            proc.arguments = ["-c", "trap '' TERM; echo ready; exec /bin/sleep 30"]
            let ready = Pipe()
            proc.standardOutput = ready
            try proc.runAsProcessGroupLeader()
            spawned.append(proc)
            let banner = readBounded(ready.fileHandleForReading, count: 6, within: 5)
            XCTAssertEqual(String(data: banner, encoding: .utf8), "ready\n", "the SIGTERM-ignoring fixture never said ready")
        } else {
            proc.executableURL = URL(fileURLWithPath: "/bin/sleep")
            proc.arguments = ["30"]
            proc.standardOutput = FileHandle.nullDevice
            try proc.runAsProcessGroupLeader()
            spawned.append(proc)
        }
        return proc
    }

    /// Run `body` and fail the test if it has not returned within `seconds`.
    /// A stop that never returns is what a broken coalescing or cleanup line
    /// looks like from here; without the bound, a mutation of one of those
    /// lines hung the run for over an hour instead of failing it.
    private func bounded(_ seconds: TimeInterval = 10, _ body: @escaping @Sendable () async -> Void) async {
        let returned = expectation(description: "returned within \(seconds)s")
        let finish = ResumeOnce()
        let task = Task {
            await body()
            // Late, after the timeout below claimed `finish`: the test has
            // already failed, and a fulfil now would be an API violation.
            if finish.claim() { returned.fulfill() }
        }
        await fulfillment(of: [returned], timeout: seconds)
        _ = finish.claim()
        task.cancel()
    }

    private func makeService() throws -> LlamaService {
        let svc = LlamaService()
        svc.portOverride = try freePort()
        svc.stopGraceSeconds = 1
        svc.stopKillWaitSeconds = 2
        return svc
    }

    // MARK: - Coalescing

    func testConcurrentStopsRunOneStopAndBothReturn() async throws {
        let svc = try makeService()
        let proc = try spawnSleeper()
        svc.process = proc
        svc.state = .running

        await bounded {
            async let first: Void = svc.stop()
            async let second: Void = svc.stop()
            _ = await (first, second)
        }

        XCTAssertEqual(svc.stopsPerformed, 1, "the second caller joins the stop in flight")
        XCTAssertFalse(proc.isRunning)
        XCTAssertNil(svc.process)
        XCTAssertEqual(svc.state, .stopped)
    }

    func testStopAfterAFinishedStopRunsAgain() async throws {
        let svc = try makeService()
        svc.process = try spawnSleeper()
        svc.state = .running
        await bounded { await svc.stop() }

        svc.process = try spawnSleeper()
        svc.state = .running
        await bounded { await svc.stop() }

        XCTAssertEqual(svc.stopsPerformed, 2, "a finished stop is not joined; the next caller gets its own")
        XCTAssertNil(svc.process)
        XCTAssertEqual(svc.state, .stopped)
    }

    // MARK: - A child assigned during the stop is not the stop's to clear

    func testStopClearsOnlyTheProcessItWaitedOn() async throws {
        let svc = try makeService()
        let old = try spawnSleeper(ignoringSigterm: true)
        svc.process = old
        svc.state = .running

        let stopping = Task { await svc.stop() }
        // Let the stop send SIGTERM and settle into its grace wait, then
        // model a start that spawned meanwhile.
        try await Task.sleep(for: .milliseconds(300))
        XCTAssertTrue(old.isRunning, "the fixture must still be alive for the race to exist")
        let successor = try spawnSleeper()
        svc.process = successor

        await bounded { await stopping.value }

        XCTAssertFalse(old.isRunning, "the stop still kills the child it waited on")
        XCTAssertTrue(successor.isRunning)
        XCTAssertIdentical(svc.process, successor, "the successor stays tracked; clearing it would orphan a live server")
        XCTAssertTrue(svc.holdsLiveProcess)
    }

    // MARK: - A stop whose wait gave up (#890)

    /// A wait that gives up leaves a process still reported running. The stop
    /// keeps it tracked and does not call itself `stopped`: untracked, the next
    /// start would launch a second server onto the port it may still hold.
    func testStopWhoseWaitGaveUpKeepsTheProcessTrackedAndIsNotStopped() async throws {
        let svc = try makeService()
        svc.waitForExit = { _, _, _, _ in false }
        let proc = try spawnSleeper(ignoringSigterm: true)
        svc.process = proc
        svc.state = .running

        await bounded { await svc.stop() }

        XCTAssertIdentical(svc.process, proc, "a process the wait gave up on stays tracked")
        XCTAssertTrue(svc.holdsLiveProcess)
        XCTAssertEqual(svc.state, .errored, "a stop that did not see the exit is not a clean stop")
    }

    /// After a stop gave up its wait, the old process's exit can land while a
    /// successor is running. It must not mark the successor errored or restart
    /// it; only the tracked process's exit counts.
    @MainActor
    func testExitOfAProcessNoLongerTrackedIsIgnored() throws {
        let svc = try makeService()
        let old = try spawnSleeper()
        let successor = try spawnSleeper()
        svc.process = successor
        svc.state = .running
        var unexpectedExits = 0
        svc.onUnexpectedExit = { unexpectedExits += 1 }

        killAndReap(old)  // its exit has landed
        svc.processExited(old)
        XCTAssertEqual(svc.state, .running, "a stale exit leaves the healthy successor alone")
        XCTAssertEqual(unexpectedExits, 0, "and does not restart it")

        // Control: the tracked process's exit still counts.
        killAndReap(successor)
        svc.processExited(successor)
        XCTAssertEqual(svc.state, .errored)
        XCTAssertEqual(unexpectedExits, 1)
    }

    @MainActor
    func testPythonServiceIgnoresTheExitOfAProcessNoLongerTracked() async throws {
        // A concrete subclass, so a restart that wrongly ran fails an assertion
        // below rather than the base class's "Subclasses must override" trap.
        let svc = WorkerService(queue: "probe", index: 0)
        svc.launcher = { _ in }  // a restart, if one wrongly ran, spawns nothing
        let old = try spawnSleeper()
        let successor = try spawnSleeper()
        svc.process = successor
        svc.state = .running

        killAndReap(old)  // its exit has landed
        await svc.processExited(old, logger: Log.logger("probe"))

        XCTAssertIdentical(svc.process, successor, "no restart replaced the healthy successor")
        XCTAssertEqual(svc.state, .running)
    }

    @MainActor
    func testGatewayServiceIgnoresTheExitOfAProcessNoLongerTracked() throws {
        let svc = GatewayService()
        let old = try spawnSleeper()
        let successor = try spawnSleeper()
        svc.process = successor
        svc.state = .running
        var unexpectedExits = 0
        svc.onUnexpectedExit = { unexpectedExits += 1 }

        killAndReap(old)  // its exit has landed
        svc.processExited(old)
        XCTAssertEqual(svc.state, .running)
        XCTAssertEqual(unexpectedExits, 0)

        killAndReap(successor)
        svc.processExited(successor)
        XCTAssertEqual(svc.state, .errored, "control: the tracked process's exit still counts")
        XCTAssertEqual(unexpectedExits, 1)
    }

    func testPythonServiceStopWhoseWaitGaveUpKeepsTheProcessTrackedAndIsNotStopped() async throws {
        let svc = WorkerService(queue: "probe", index: 0)
        svc.waitForExit = { _, _, _, _ in false }
        let proc = try spawnSleeper(ignoringSigterm: true)
        svc.process = proc
        svc.state = .running

        await bounded { await svc.stop() }

        XCTAssertIdentical(svc.process, proc, "a process the wait gave up on stays tracked")
        XCTAssertEqual(svc.state, .errored, "a stop that did not see the exit is not a clean stop")
    }

    func testGatewayServiceStopWhoseWaitGaveUpKeepsTheProcessTrackedAndIsNotStopped() async throws {
        let svc = GatewayService()
        svc.waitForExit = { _, _, _, _ in false }
        let proc = try spawnSleeper(ignoringSigterm: true)
        svc.process = proc
        svc.state = .running

        await bounded { await svc.stop() }

        XCTAssertIdentical(svc.process, proc, "a process the wait gave up on stays tracked")
        XCTAssertEqual(svc.state, .errored, "a stop that did not see the exit is not a clean stop")
    }

    /// A process Foundation still calls running but the kernel says is gone is
    /// not a live process: otherwise the service that tracks it refuses every
    /// start for good, and only quitting the app recovers.
    func testHoldsLiveProcessIgnoresAnExitFoundationNeverNoticed() throws {
        let pid = try XCTUnwrap(posixSpawn("/usr/bin/true"))
        var st: Int32 = 0
        XCTAssertEqual(waitpid(pid, &st, 0), pid)
        let gone = UnnoticedExitProcess(pid: pid)

        let llama = try makeService()
        llama.process = gone
        XCTAssertFalse(llama.holdsLiveProcess, "LLM")
        let python = WorkerService(queue: "probe", index: 0)
        python.process = gone
        XCTAssertFalse(python.holdsLiveProcess, "Python")
        let gateway = GatewayService()
        gateway.process = gone
        XCTAssertFalse(gateway.holdsLiveProcess, "Gateway")
    }

    /// The give-up policy at the Python services and the parallel worker stops.
    func testPythonServiceSettleStopKeepsAProcessTheWaitGaveUpOn() throws {
        let svc = PythonService(name: "probe")
        let proc = try spawnSleeper()
        svc.process = proc
        svc.state = .stopping

        svc.settleStop(exited: false)
        XCTAssertIdentical(svc.process, proc)
        XCTAssertEqual(svc.state, .errored)

        svc.settleStop(exited: true)
        XCTAssertNil(svc.process)
        XCTAssertEqual(svc.state, .stopped)
    }

    /// Foundation's `Process.run()` already spawns its child as a group
    /// leader on macOS, so `performStop`'s `terminate()` fallback for a
    /// non-leader cannot be reached through `Process`: a test that removed
    /// the fallback stayed green because `killpg` succeeded anyway. Pinned
    /// here so the next reader does not spend the same hour, and so a
    /// Foundation change that stops doing this shows up as a failure.
    func testPlainRunChildAlreadyLeadsItsGroupSoTheFallbackIsUnreachable() async throws {
        let svc = try makeService()
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/bin/sleep")
        proc.arguments = ["30"]
        try proc.run()
        spawned.append(proc)
        XCTAssertEqual(getpgid(proc.processIdentifier), proc.processIdentifier, "Process.run() child leads its own group")
        svc.process = proc
        svc.state = .running

        await bounded { await svc.stop() }

        XCTAssertFalse(proc.isRunning)
        XCTAssertEqual(proc.terminationStatus, SIGTERM, "the group SIGTERM reached it; no SIGKILL escalation")
        XCTAssertNil(svc.process)
    }

    // MARK: - holdsLiveProcess tracks the process across services

    func testPythonServiceHoldsLiveProcessTracksIsRunning() throws {
        let svc = PythonService(name: "probe")
        XCTAssertFalse(svc.holdsLiveProcess)
        let proc = try spawnSleeper()
        svc.process = proc
        XCTAssertTrue(svc.holdsLiveProcess)
        killAndReap(proc)
        XCTAssertFalse(svc.holdsLiveProcess)
    }

    func testGatewayServiceHoldsLiveProcessTracksIsRunning() throws {
        let svc = GatewayService()
        XCTAssertFalse(svc.holdsLiveProcess)
        let proc = try spawnSleeper()
        svc.process = proc
        XCTAssertTrue(svc.holdsLiveProcess)
        killAndReap(proc)
        XCTAssertFalse(svc.holdsLiveProcess)
    }
}

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
        for proc in spawned where proc.isRunning {
            kill(proc.processIdentifier, SIGKILL)
            proc.waitUntilExit()
        }
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
            let banner = ready.fileHandleForReading.readData(ofLength: 6)
            XCTAssertEqual(String(data: banner, encoding: .utf8), "ready\n")
        } else {
            proc.executableURL = URL(fileURLWithPath: "/bin/sleep")
            proc.arguments = ["30"]
            proc.standardOutput = FileHandle.nullDevice
            try proc.runAsProcessGroupLeader()
            spawned.append(proc)
        }
        return proc
    }

    /// A TCP port nothing holds right now: bind an ephemeral one, read it back, close it.
    private func freePort() throws -> Int {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        XCTAssertGreaterThanOrEqual(fd, 0)
        defer { close(fd) }
        var addr = sockaddr_in()
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = 0
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")
        var len = socklen_t(MemoryLayout<sockaddr_in>.size)
        let bound = withUnsafeMutablePointer(to: &addr) { ptr in
            ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { sa in
                Darwin.bind(fd, sa, len) == 0 && getsockname(fd, sa, &len) == 0
            }
        }
        XCTAssertTrue(bound, "bind/getsockname on an ephemeral port")
        return Int(UInt16(bigEndian: addr.sin_port))
    }

    /// Run `body` and fail the test if it has not returned within `seconds`.
    /// A stop that never returns is what a broken coalescing or cleanup line
    /// looks like from here; without the bound, a mutation of one of those
    /// lines hung the run for over an hour instead of failing it.
    private func bounded(_ seconds: TimeInterval = 10, _ body: @escaping @Sendable () async -> Void) async {
        let returned = expectation(description: "returned within \(seconds)s")
        let task = Task {
            await body()
            returned.fulfill()
        }
        await fulfillment(of: [returned], timeout: seconds)
        task.cancel()
    }

    private func makeService() throws -> LlamaService {
        let svc = LlamaService()
        svc.portOverride = try freePort()
        svc.stopGraceSeconds = 1
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
        kill(proc.processIdentifier, SIGKILL)
        proc.waitUntilExit()
        XCTAssertFalse(svc.holdsLiveProcess)
    }

    func testGatewayServiceHoldsLiveProcessTracksIsRunning() throws {
        let svc = GatewayService()
        XCTAssertFalse(svc.holdsLiveProcess)
        let proc = try spawnSleeper()
        svc.process = proc
        XCTAssertTrue(svc.holdsLiveProcess)
        kill(proc.processIdentifier, SIGKILL)
        proc.waitUntilExit()
        XCTAssertFalse(svc.holdsLiveProcess)
    }
}

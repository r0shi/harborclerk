import XCTest
@testable import HarborClerkServer

/// A mock service for testing state transitions without real subprocesses.
final class MockService: ManagedService {
    var name: String
    var state: ServiceState
    var healthCheckResult: Bool
    var isLaunchdManaged: Bool = false

    init(name: String, state: ServiceState, healthCheckResult: Bool = true) {
        self.name = name
        self.state = state
        self.healthCheckResult = healthCheckResult
    }

    func start() async throws {}
    func stop() async {}

    func healthCheck() async -> Bool {
        return healthCheckResult
    }
}

/// A mock service whose health check always fails. Used for driving
/// HealthChecker through the consecutive-failure threshold without
/// involving real subprocesses.
final class MockServiceWithFailingHealth: ManagedService {
    var name: String
    var state: ServiceState = .stopped
    var isLaunchdManaged: Bool = false

    init(name: String) {
        self.name = name
    }

    func start() async throws {}
    func stop() async {}

    func healthCheck() async -> Bool {
        return false
    }
}

/// A ServiceManager subclass whose `attemptAutoRestart` is observable
/// and long-running on demand. Lets HealthChecker tests inspect the
/// in-flight task table without spinning up real subprocesses.
@MainActor
final class MockServiceManager: ServiceManager {
    /// Names of services for which `attemptAutoRestart` was invoked, in order.
    var attemptAutoRestartCalled: [String] = []
    /// How long `attemptAutoRestart` should sleep before returning. The
    /// sleep is cancellation-aware (uses `try? await Task.sleep`) so
    /// callers can observe `cancelInFlightRestarts()` behaviour.
    var attemptAutoRestartDelay: Duration = .seconds(0)

    /// The pid file in a scratch location: a service reaching `running`
    /// writes it, and the default path is the live app's data folder.
    override init() {
        super.init()
        pidFileURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("HealthCheckerTests-\(UUID().uuidString)-child-pids.txt")
    }

    override func attemptAutoRestart(_ service: any ManagedService) async {
        // Honor the shutdown guard so tests can exercise the real
        // early-return path. Mirrors the production guard at the top of
        // ServiceManager.attemptAutoRestart.
        guard !isShuttingDown else { return }
        attemptAutoRestartCalled.append(service.name)
        try? await Task.sleep(for: attemptAutoRestartDelay)
    }
}

// Since HealthChecker requires a full ServiceManager, we test the health-check
// state transition logic directly: if a running service fails its health check,
// it should transition to errored. This mirrors what HealthChecker.checkAll() does.

final class HealthCheckerTests: XCTestCase {

    func testRunningServiceFailingHealthCheckBecomesErrored() async {
        let service = MockService(name: "test-svc", state: .running, healthCheckResult: false)
        XCTAssertEqual(service.state, .running)

        // Simulate what HealthChecker.checkAll() does
        if service.state == .running {
            let healthy = await service.healthCheck()
            if !healthy {
                service.state = .errored
            }
        }

        XCTAssertEqual(service.state, .errored)
    }

    func testRunningServicePassingHealthCheckStaysRunning() async {
        let service = MockService(name: "test-svc", state: .running, healthCheckResult: true)

        if service.state == .running {
            let healthy = await service.healthCheck()
            if !healthy {
                service.state = .errored
            }
        }

        XCTAssertEqual(service.state, .running)
    }

    func testStoppedServiceSkipsHealthCheck() async {
        let service = MockService(name: "test-svc", state: .stopped, healthCheckResult: false)

        // HealthChecker skips non-running services
        if service.state == .running {
            let healthy = await service.healthCheck()
            if !healthy {
                service.state = .errored
            }
        }

        XCTAssertEqual(service.state, .stopped, "Stopped service should not be affected")
    }

    // Test that the overall state reflects health check transitions
    func testOverallStateAfterHealthFailure() async {
        let services: [MockService] = [
            MockService(name: "pg", state: .running, healthCheckResult: true),
            MockService(name: "api", state: .running, healthCheckResult: false),
            MockService(name: "tika", state: .running, healthCheckResult: true),
        ]

        // Simulate health check loop
        for service in services {
            guard service.state == .running else { continue }
            let healthy = await service.healthCheck()
            if !healthy {
                service.state = .errored
            }
        }

        let states = services.map(\.state)
        let overall = ServiceManager.computeOverallState(states)
        XCTAssertEqual(overall, .errored)
    }

    // MARK: - In-flight restart-task tracking

    @MainActor
    func testTrackingStoresHandleWhenCheckAllTriggersRestart() async throws {
        let mockServices = MockServiceManager()
        mockServices.attemptAutoRestartDelay = .seconds(2)
        let svc = MockServiceWithFailingHealth(name: "test-svc")
        mockServices.services = [svc]
        let hc = HealthChecker(serviceManager: mockServices)
        svc.state = .running
        for _ in 0..<6 {
            await hc.tickForTesting()
        }
        XCTAssertEqual(hc.inFlightTaskCount, 1)

        // Drain the in-flight Task so it doesn't outlive the test. Cancel
        // is fast (Task.sleep is cancellation-aware) and cancelInFlightRestarts
        // awaits completion. Without this XCTest can complain about orphan
        // Tasks under Xcode 16+ async-teardown.
        await hc.cancelInFlightRestarts()
    }

    @MainActor
    func testCancelInFlightRestartsCancelsAndAwaits() async throws {
        let mockServices = MockServiceManager()
        mockServices.attemptAutoRestartDelay = .seconds(5)
        let svc = MockServiceWithFailingHealth(name: "test-svc")
        mockServices.services = [svc]
        let hc = HealthChecker(serviceManager: mockServices)
        svc.state = .running
        for _ in 0..<6 { await hc.tickForTesting() }
        XCTAssertEqual(hc.inFlightTaskCount, 1)

        let start = Date()
        await hc.cancelInFlightRestarts()
        let elapsed = Date().timeIntervalSince(start)

        XCTAssertEqual(hc.inFlightTaskCount, 0)
        XCTAssertLessThan(elapsed, 1.0, "Task.cancel should break the sleep, not wait the full 5s")
    }

    /// Source-text guard: catches regressions where someone accidentally
    /// removes the cancellation hook from ServiceManager. The real
    /// integration test is "run the menubar and click Quit while an
    /// auto-restart is in flight" which we can't drive from XCTest.
    func testStopAllCancelsInFlightRestarts() throws {
        let thisFile = URL(fileURLWithPath: #file)
        let serviceManagerURL = thisFile
            .deletingLastPathComponent()          // → HarborClerkServerTests/
            .deletingLastPathComponent()          // → HarborClerkServer/ (project root)
            .appendingPathComponent("HarborClerkServer")
            .appendingPathComponent("ServiceManager.swift")
        let source = try String(contentsOf: serviceManagerURL, encoding: .utf8)
        XCTAssertTrue(
            source.contains("await healthChecker?.cancelInFlightRestarts()"),
            "ServiceManager.swift must call healthChecker.cancelInFlightRestarts() (likely in stopAll or restartForChangedSettings)"
        )
    }

    /// Closes the orphan-restart race that `cancelInFlightRestarts()`
    /// alone can't fix: onUnexpectedExit-dispatched auto-restart Tasks
    /// are NOT tracked in HealthChecker.taskHandles, so they bypass
    /// cancellation. The `isShuttingDown` flag ensures they early-return
    /// from `attemptAutoRestart` instead of resurrecting a service whose
    /// state was already advanced past `.shutdownPending` by stopAll.
    @MainActor
    func testAttemptAutoRestartIsNoOpWhileShuttingDown() async throws {
        let mockServices = MockServiceManager()
        let svc = MockServiceWithFailingHealth(name: "test-svc")
        svc.state = .running
        mockServices.services = [svc]

        // Simulate stopAll having set isShuttingDown true:
        mockServices.isShuttingDown = true

        await mockServices.attemptAutoRestart(svc)

        XCTAssertEqual(mockServices.attemptAutoRestartCalled.count, 0,
            "attemptAutoRestart must early-return when isShuttingDown == true")
    }

    /// PR-4: a launchd-managed service whose health check fails should still
    /// transition to .errored after consecutive failures, but HealthChecker
    /// must NOT dispatch its own auto-restart Task. launchd's KeepAlive
    /// handles crash recovery for these services; double-restarting via both
    /// layers would be a foot-gun.
    @MainActor
    func testCheckAllSkipsAutoRestartForLaunchdManagedService() async throws {
        let mockServices = MockServiceManager()
        let svc = MockServiceWithFailingHealth(name: "test-launchd-svc")
        svc.isLaunchdManaged = true
        mockServices.services = [svc]
        let hc = HealthChecker(serviceManager: mockServices)

        svc.state = .running
        for _ in 0..<6 { await hc.tickForTesting() }

        XCTAssertEqual(hc.inFlightTaskCount, 0, "must not dispatch auto-restart Task for launchd-managed service")
        XCTAssertTrue(mockServices.attemptAutoRestartCalled.isEmpty, "must not call attemptAutoRestart")
        // Service still moves to .errored — only the restart path is skipped.
        XCTAssertEqual(svc.state, .errored)
    }

    // MARK: - An errored service whose process serves is promoted (#767)

    /// Run `body` and fail the test if it has not returned within `seconds`,
    /// so a mutation that makes a tick or a restart hang fails instead of
    /// stalling the run.
    @MainActor
    private func bounded(_ what: String, seconds: TimeInterval = 10, _ body: @escaping @MainActor () async -> Void) async {
        let returned = expectation(description: "\(what) returned within \(seconds)s")
        let task = Task { @MainActor in
            await body()
            returned.fulfill()
        }
        await fulfillment(of: [returned], timeout: seconds)
        task.cancel()
    }

    /// A manager and checker around one service, with the pid file removed
    /// afterwards.
    @MainActor
    private func makeChecker(for svc: any ManagedService) -> (MockServiceManager, HealthChecker) {
        let sm = MockServiceManager()
        sm.services = [svc]
        let url = sm.pidFileURL
        addTeardownBlock { try? FileManager.default.removeItem(at: url) }
        return (sm, HealthChecker(serviceManager: sm))
    }

    /// The #767 case: the start-up wait timed out (120 s for the LLM) and
    /// marked the service errored, keeping the process; the model finished
    /// loading and serves. The next tick must say so.
    @MainActor
    func testErroredServiceWhoseProcessAnswersIsPromotedToRunning() async {
        let svc = MockProcessService(name: "LLM", state: .errored, holdsLiveProcess: true)
        svc.healthy = true
        let (sm, hc) = makeChecker(for: svc)

        await bounded("one tick") { await hc.tickForTesting() }

        XCTAssertEqual(svc.state, .running, "a process that answers its health check is running, whatever the start-up wait concluded")
        XCTAssertEqual(svc.healthCalls, 1)
        XCTAssertTrue(FileManager.default.fileExists(atPath: sm.pidFileURL.path),
                      "promoted through markReady, the same ready path as a start: its savePidFile ran and wrote the pid file (empty here, since the mock has no pid)")
    }

    @MainActor
    func testErroredServiceWhoseProcessDoesNotAnswerStaysErroredAndIsNotRestarted() async {
        let svc = MockProcessService(name: "LLM", state: .errored, holdsLiveProcess: true)
        svc.healthy = false
        let (sm, hc) = makeChecker(for: svc)

        await bounded("seven ticks") { for _ in 0..<7 { await hc.tickForTesting() } }

        XCTAssertEqual(svc.state, .errored)
        XCTAssertEqual(svc.healthCalls, 7, "probed on every tick while the process is alive")
        XCTAssertEqual(hc.inFlightTaskCount, 0, "an errored service is not counted towards a restart; it is already errored")
        XCTAssertTrue(sm.attemptAutoRestartCalled.isEmpty)
    }

    @MainActor
    func testErroredServiceWithoutALiveProcessIsNotProbed() async {
        let svc = MockProcessService(name: "LLM", state: .errored, holdsLiveProcess: false)
        svc.healthy = true  // e.g. something else answering on the port
        let (_, hc) = makeChecker(for: svc)

        await bounded("one tick") { await hc.tickForTesting() }

        XCTAssertEqual(svc.state, .errored, "nothing of ours is serving; a crash or a refusal stays errored")
        XCTAssertEqual(svc.healthCalls, 0)
    }

    @MainActor
    func testErroredServicePausedForARestartIsLeftToTheRestart() async {
        let svc = MockProcessService(name: "LLM", state: .errored, holdsLiveProcess: true)
        svc.healthy = true
        let (_, hc) = makeChecker(for: svc)
        hc.pausedServices.insert(svc.name)

        await bounded("one tick") { await hc.tickForTesting() }

        XCTAssertEqual(svc.state, .errored)
        XCTAssertEqual(svc.healthCalls, 0)
    }

    @MainActor
    func testErroredLaunchdManagedServiceIsNotPromoted() async {
        let svc = MockProcessService(name: "Tika", state: .errored, holdsLiveProcess: true)
        svc.isLaunchdManaged = true
        svc.healthy = true
        let (_, hc) = makeChecker(for: svc)

        await bounded("one tick") { await hc.tickForTesting() }

        XCTAssertEqual(svc.state, .errored)
        XCTAssertEqual(svc.healthCalls, 0)
    }

    /// A stop or relaunch that lands while the probe is in flight owns the
    /// state; a late answer must not drag it back to `running`.
    @MainActor
    func testAStateChangeDuringTheProbeIsNotOverwritten() async {
        let svc = MockProcessService(name: "LLM", state: .errored, holdsLiveProcess: true)
        svc.healthy = true
        svc.stateDuringHealthCheck = .stopping
        let (_, hc) = makeChecker(for: svc)

        await bounded("one tick") { await hc.tickForTesting() }

        XCTAssertEqual(svc.state, .stopping)
    }

    /// An auto-restart that pauses the service while the probe is in flight
    /// owns it from then on, even though the state still reads `errored`
    /// (attemptAutoRestart pauses before it stops anything); a late answer
    /// must not promote it under the restart.
    @MainActor
    func testAServicePausedDuringTheProbeIsNotPromoted() async {
        let svc = MockProcessService(name: "LLM", state: .errored, holdsLiveProcess: true)
        svc.healthy = true
        let (_, hc) = makeChecker(for: svc)
        svc.duringHealthCheck = { [unowned svc] in hc.pausedServices.insert(svc.name) }

        await bounded("one tick") { await hc.tickForTesting() }

        XCTAssertEqual(svc.healthCalls, 1, "probed: the pause landed after the pre-check")
        XCTAssertEqual(svc.state, .errored)
    }

    /// An answer from the port after our child has exited is not our child
    /// serving (a stale server from an earlier run, say); it stays errored.
    @MainActor
    func testAProcessThatExitsDuringTheProbeIsNotPromoted() async {
        let svc = MockProcessService(name: "LLM", state: .errored, holdsLiveProcess: true)
        svc.healthy = true
        svc.holdsLiveProcessDuringHealthCheck = false
        let (_, hc) = makeChecker(for: svc)

        await bounded("one tick") { await hc.tickForTesting() }

        XCTAssertEqual(svc.state, .errored)
    }

    /// Once promoted, the service is an ordinary running one: six failed
    /// probes error it and dispatch the health-driven restart that #767 lost
    /// for the life of the server.
    @MainActor
    func testAPromotedServiceGetsTheUsualHealthDrivenRestart() async {
        let svc = MockProcessService(name: "LLM", state: .errored, holdsLiveProcess: true)
        svc.healthy = true
        let (sm, hc) = makeChecker(for: svc)
        // Keeps the dispatched restart in flight to be counted; cancelled below.
        sm.attemptAutoRestartDelay = .seconds(30)

        await bounded("promotion tick") { await hc.tickForTesting() }
        XCTAssertEqual(svc.state, .running)

        svc.healthy = false
        await bounded("six failing ticks") { for _ in 0..<6 { await hc.tickForTesting() } }

        XCTAssertEqual(svc.state, .errored)
        XCTAssertEqual(hc.inFlightTaskCount, 1, "the sixth consecutive failure dispatches an auto-restart")
        await bounded("the dispatched restart") {
            // Stops on cancellation too, so a timed-out wait does not spin on.
            while sm.attemptAutoRestartCalled.isEmpty && !Task.isCancelled { await Task.yield() }
        }
        XCTAssertEqual(sm.attemptAutoRestartCalled, ["LLM"])
        await bounded("draining the restart") { await hc.cancelInFlightRestarts() }
    }
}

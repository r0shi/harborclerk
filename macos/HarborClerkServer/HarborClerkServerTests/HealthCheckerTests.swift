import XCTest
@testable import HarborClerkServer

/// A mock service for testing state transitions without real subprocesses.
final class MockService: ManagedService {
    var name: String
    var state: ServiceState
    var healthCheckResult: Bool

    init(name: String, state: ServiceState, healthCheckResult: Bool = true) {
        self.name = name
        self.state = state
        self.healthCheckResult = healthCheckResult
    }

    func start() async throws {}
    func stop() {}

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

    init(name: String) {
        self.name = name
    }

    func start() async throws {}
    func stop() {}

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

    override func attemptAutoRestart(_ service: any ManagedService) async {
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
        let url = URL(fileURLWithPath: "/Users/alex/mcp-gateway/macos/HarborClerkServer/HarborClerkServer/ServiceManager.swift")
        let source = try String(contentsOf: url, encoding: .utf8)
        XCTAssertTrue(
            source.contains("await healthChecker?.cancelInFlightRestarts()"),
            "ServiceManager.swift must call healthChecker.cancelInFlightRestarts() (likely in stopAll or restartForChangedSettings)"
        )
    }
}

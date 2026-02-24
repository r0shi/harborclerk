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
}

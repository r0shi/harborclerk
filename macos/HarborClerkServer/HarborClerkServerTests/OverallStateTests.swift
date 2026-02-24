import XCTest
@testable import HarborClerkServer

final class OverallStateTests: XCTestCase {

    func testAllRunning() {
        let result = ServiceManager.computeOverallState([.running, .running, .running])
        XCTAssertEqual(result, .running)
    }

    func testAllStopped() {
        let result = ServiceManager.computeOverallState([.stopped, .stopped, .stopped])
        XCTAssertEqual(result, .stopped)
    }

    func testEmptyReturnsStopped() {
        let result = ServiceManager.computeOverallState([])
        XCTAssertEqual(result, .stopped)
    }

    func testAnyErroredReturnsErrored() {
        let result = ServiceManager.computeOverallState([.running, .errored, .stopped])
        XCTAssertEqual(result, .errored)
    }

    func testErroredTakesPriorityOverStopping() {
        let result = ServiceManager.computeOverallState([.stopping, .errored, .running])
        XCTAssertEqual(result, .errored)
    }

    func testStoppingWithoutErrored() {
        let result = ServiceManager.computeOverallState([.running, .stopping, .stopped])
        XCTAssertEqual(result, .stopping)
    }

    func testMixOfRunningAndStarting() {
        let result = ServiceManager.computeOverallState([.running, .starting, .running])
        XCTAssertEqual(result, .starting)
    }

    func testMixOfStoppedAndStarting() {
        let result = ServiceManager.computeOverallState([.stopped, .starting])
        XCTAssertEqual(result, .starting)
    }

    func testSingleRunning() {
        let result = ServiceManager.computeOverallState([.running])
        XCTAssertEqual(result, .running)
    }

    func testSingleStopped() {
        let result = ServiceManager.computeOverallState([.stopped])
        XCTAssertEqual(result, .stopped)
    }

    func testSingleErrored() {
        let result = ServiceManager.computeOverallState([.errored])
        XCTAssertEqual(result, .errored)
    }
}

import XCTest
@testable import HarborClerkServer

final class WorkerCountsTests: XCTestCase {

    // MARK: - Quiet preset (always 1, 1)

    func testQuiet1Core() {
        let result = ServiceManager.workerCounts(preset: "quiet", cores: 1)
        XCTAssertEqual(result.io, 1)
        XCTAssertEqual(result.cpu, 1)
    }

    func testQuiet16Cores() {
        let result = ServiceManager.workerCounts(preset: "quiet", cores: 16)
        XCTAssertEqual(result.io, 1)
        XCTAssertEqual(result.cpu, 1)
    }

    // MARK: - Balanced preset: io=min(8, max(2, cores/4)), cpu=1

    func testBalanced1Core() {
        let result = ServiceManager.workerCounts(preset: "balanced", cores: 1)
        XCTAssertEqual(result.io, 2) // max(2, 0) = 2
        XCTAssertEqual(result.cpu, 1)
    }

    func testBalanced8Cores() {
        let result = ServiceManager.workerCounts(preset: "balanced", cores: 8)
        XCTAssertEqual(result.io, 2) // max(2, 2) = 2
        XCTAssertEqual(result.cpu, 1)
    }

    func testBalanced16Cores() {
        let result = ServiceManager.workerCounts(preset: "balanced", cores: 16)
        XCTAssertEqual(result.io, 4) // max(2, 4) = 4
        XCTAssertEqual(result.cpu, 1)
    }

    func testBalanced40Cores() {
        let result = ServiceManager.workerCounts(preset: "balanced", cores: 40)
        XCTAssertEqual(result.io, 8) // min(8, max(2, 10)) = 8
        XCTAssertEqual(result.cpu, 1)
    }

    // MARK: - Fast preset: io=min(8, max(2, cores/2)), cpu=min(2, max(1, cores/4))

    func testFast1Core() {
        let result = ServiceManager.workerCounts(preset: "fast", cores: 1)
        XCTAssertEqual(result.io, 2)  // max(2, 0) = 2
        XCTAssertEqual(result.cpu, 1) // max(1, 0) = 1
    }

    func testFast8Cores() {
        let result = ServiceManager.workerCounts(preset: "fast", cores: 8)
        XCTAssertEqual(result.io, 4) // max(2, 4) = 4
        XCTAssertEqual(result.cpu, 2) // min(2, max(1, 2)) = 2
    }

    func testFast16Cores() {
        let result = ServiceManager.workerCounts(preset: "fast", cores: 16)
        XCTAssertEqual(result.io, 8) // min(8, max(2, 8)) = 8
        XCTAssertEqual(result.cpu, 2) // min(2, max(1, 4)) = 2
    }

    func testFast32Cores() {
        let result = ServiceManager.workerCounts(preset: "fast", cores: 32)
        XCTAssertEqual(result.io, 8) // min(8, 16) = 8 (capped)
        XCTAssertEqual(result.cpu, 2) // min(2, 8) = 2 (capped)
    }

    // MARK: - Unknown preset falls through to balanced default

    func testUnknownPresetFallsToBalanced() {
        let result = ServiceManager.workerCounts(preset: "turbo", cores: 8)
        let balanced = ServiceManager.workerCounts(preset: "balanced", cores: 8)
        XCTAssertEqual(result.io, balanced.io)
        XCTAssertEqual(result.cpu, balanced.cpu)
    }

    func testEmptyPresetFallsToBalanced() {
        let result = ServiceManager.workerCounts(preset: "", cores: 16)
        let balanced = ServiceManager.workerCounts(preset: "balanced", cores: 16)
        XCTAssertEqual(result.io, balanced.io)
        XCTAssertEqual(result.cpu, balanced.cpu)
    }
}

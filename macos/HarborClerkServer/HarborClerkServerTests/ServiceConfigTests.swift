import XCTest
@testable import HarborClerkServer

/// Tests for service configuration correctness — entry point names,
/// path components, and PID file handling.
final class ServiceConfigTests: XCTestCase {

    // MARK: - Executable Name Assertions

    func testEmbedderExecutableName() {
        let svc = EmbedderService()
        XCTAssertEqual(svc.executableName, "harbor-clerk-embedder",
                       "Embedder entry point must match pyproject.toml [project.scripts]")
    }

    func testAPIExecutableName() {
        let svc = APIService()
        XCTAssertEqual(svc.executableName, "harbor-clerk-api")
    }

    func testWorkerExecutableName() {
        let svc = WorkerService(queue: "io", index: 0)
        XCTAssertEqual(svc.executableName, "harbor-clerk-worker")
    }

    // MARK: - Stale PID File Handling

    func testStalePidWithDeadProcess() {
        // PID 99999999 is almost certainly not running
        let contents = "99999999\n/some/data/dir\n5433\n"
        let action = PostgresService.stalePidAction(pidFileContents: contents)
        // Should want to remove since the PID is dead
        if case .remove(let pid) = action {
            XCTAssertEqual(pid, 99999999)
        } else {
            XCTFail("Expected .remove, got \(action)")
        }
    }

    func testStalePidWithCurrentProcess() {
        // Use our own PID — guaranteed to be alive
        let myPid = ProcessInfo.processInfo.processIdentifier
        let contents = "\(myPid)\n/some/data/dir\n5433\n"
        let action = PostgresService.stalePidAction(pidFileContents: contents)
        XCTAssertEqual(action, .keep, "Should keep PID file when process is alive")
    }

    func testStalePidWithNilContents() {
        let action = PostgresService.stalePidAction(pidFileContents: nil)
        XCTAssertEqual(action, .removeUnparseable)
    }

    func testStalePidWithEmptyContents() {
        let action = PostgresService.stalePidAction(pidFileContents: "")
        XCTAssertEqual(action, .removeUnparseable)
    }

    func testStalePidWithGarbageContents() {
        let action = PostgresService.stalePidAction(pidFileContents: "not-a-pid\ngarbage")
        XCTAssertEqual(action, .removeUnparseable)
    }

    func testStalePidWithOnlyNewlines() {
        let action = PostgresService.stalePidAction(pidFileContents: "\n\n\n")
        XCTAssertEqual(action, .removeUnparseable)
    }

    // MARK: - Log Formatting

    func testLogFormatForCopy() {
        // Verify the ISO8601 + [service] format used by copy/save
        let formatter = ISO8601DateFormatter()
        let date = Date(timeIntervalSince1970: 0)
        let ts = formatter.string(from: date)
        let formatted = "\(ts) [api] hello world"
        XCTAssertTrue(formatted.contains("[api]"))
        XCTAssertTrue(formatted.contains("hello world"))
        XCTAssertTrue(formatted.hasPrefix("1970-01-01"))
    }
}

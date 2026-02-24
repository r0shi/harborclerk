import XCTest
@testable import HarborClerkServer

final class LogManagerTests: XCTestCase {

    private var logManager: LogManager!

    override func setUp() {
        super.setUp()
        logManager = LogManager()
    }

    func testAppendSingleLine() {
        logManager.append(service: "test", text: "hello world")
        XCTAssertEqual(logManager.lines.count, 1)
        XCTAssertEqual(logManager.lines.first?.service, "test")
        XCTAssertEqual(logManager.lines.first?.text, "hello world")
    }

    func testAppendMultipleLines() {
        logManager.append(service: "api", text: "line one\nline two\nline three")
        XCTAssertEqual(logManager.lines.count, 3)
        XCTAssertEqual(logManager.lines[0].text, "line one")
        XCTAssertEqual(logManager.lines[1].text, "line two")
        XCTAssertEqual(logManager.lines[2].text, "line three")
    }

    func testEmptyLinesFiltered() {
        logManager.append(service: "worker", text: "first\n\n\nsecond\n")
        XCTAssertEqual(logManager.lines.count, 2)
        XCTAssertEqual(logManager.lines[0].text, "first")
        XCTAssertEqual(logManager.lines[1].text, "second")
    }

    func testClear() {
        logManager.append(service: "test", text: "some text")
        XCTAssertFalse(logManager.lines.isEmpty)
        logManager.clear()
        XCTAssertTrue(logManager.lines.isEmpty)
    }

    func testMaxLinesCapEnforced() {
        // LogManager has maxLines = 2000. Add more and verify trimming.
        for i in 0..<2100 {
            logManager.append(service: "load", text: "line \(i)")
        }
        XCTAssertEqual(logManager.lines.count, 2000)
        // The oldest lines should have been trimmed; last line should be "line 2099"
        XCTAssertEqual(logManager.lines.last?.text, "line 2099")
        // First remaining line should be "line 100"
        XCTAssertEqual(logManager.lines.first?.text, "line 100")
    }

    func testTimestampIsRecent() {
        let before = Date()
        logManager.append(service: "test", text: "timestamped")
        let after = Date()
        guard let ts = logManager.lines.first?.timestamp else {
            XCTFail("No log line")
            return
        }
        XCTAssertTrue(ts >= before && ts <= after)
    }
}

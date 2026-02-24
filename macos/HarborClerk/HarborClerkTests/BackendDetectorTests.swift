import XCTest
@testable import HarborClerk

final class BackendDetectorTests: XCTestCase {

    private var tempDir: URL!

    override func setUp() {
        super.setUp()
        tempDir = FileManager.default.temporaryDirectory
            .appendingPathComponent("BackendDetectorTests-\(UUID().uuidString)")
        try? FileManager.default.createDirectory(at: tempDir, withIntermediateDirectories: true)
    }

    override func tearDown() {
        try? FileManager.default.removeItem(at: tempDir)
        super.tearDown()
    }

    // Note: BackendDetector.readPort() reads from a hardcoded Application Support path,
    // so we test the port-reading logic indirectly by verifying the default behavior
    // and the URL construction.

    @MainActor
    func testDefaultBaseURL() {
        let detector = BackendDetector()
        // Default port is 8100 (from readPort fallback or actual config)
        let port = detector.baseURL.port ?? 0
        // Should be a valid localhost URL
        XCTAssertEqual(detector.baseURL.scheme, "http")
        XCTAssertEqual(detector.baseURL.host, "localhost")
        XCTAssertTrue(port > 0, "Port should be positive")
    }

    @MainActor
    func testInitiallyUnavailable() {
        let detector = BackendDetector()
        XCTAssertFalse(detector.isAvailable)
    }

    func testReadPortWithValidConfig() throws {
        // Write a config file to the expected Application Support location
        let appSupport = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first!
        let configDir = appSupport.appendingPathComponent("Harbor Clerk")
        let configURL = configDir.appendingPathComponent("config.json")

        // Read current config to restore later
        let originalData = try? Data(contentsOf: configURL)

        // Write test config
        try? FileManager.default.createDirectory(at: configDir, withIntermediateDirectories: true)
        let testConfig: [String: Any] = ["api_port": 9999]
        let data = try JSONSerialization.data(withJSONObject: testConfig)
        try data.write(to: configURL)

        let port = BackendDetector.readPort()
        XCTAssertEqual(port, 9999)

        // Restore original config
        if let originalData = originalData {
            try originalData.write(to: configURL)
        } else {
            try? FileManager.default.removeItem(at: configURL)
        }
    }

    func testReadPortDefaultsTo8100WhenNoConfig() throws {
        // Temporarily move config if it exists
        let appSupport = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first!
        let configURL = appSupport
            .appendingPathComponent("Harbor Clerk")
            .appendingPathComponent("config.json")
        let backupURL = configURL.appendingPathExtension("backup")

        let hadConfig = FileManager.default.fileExists(atPath: configURL.path)
        if hadConfig {
            try FileManager.default.moveItem(at: configURL, to: backupURL)
        }

        let port = BackendDetector.readPort()
        XCTAssertEqual(port, 8100)

        // Restore
        if hadConfig {
            try FileManager.default.moveItem(at: backupURL, to: configURL)
        }
    }
}

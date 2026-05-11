import XCTest
@testable import HarborClerkServer

final class LaunchdAgentTests: XCTestCase {
    private var tempDir: URL!
    override func setUp() async throws {
        tempDir = FileManager.default.temporaryDirectory.appendingPathComponent("launchd-agent-tests-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: tempDir, withIntermediateDirectories: true)
    }
    override func tearDown() async throws {
        try? FileManager.default.removeItem(at: tempDir)
    }

    func testEnsureInstalledNoOpsWhenPlistMatches() async throws {
        let plistURL = tempDir.appendingPathComponent("a.plist")
        try "<plist>match</plist>".write(to: plistURL, atomically: true, encoding: .utf8)
        let fake = FakeLaunchctlClient()
        let agent = LaunchdAgent(label: "com.test.a", plistURL: plistURL, launchctl: fake)

        try await agent.ensureInstalled(plistContent: "<plist>match</plist>")

        // No launchctl invocations because content matched.
        XCTAssertEqual(fake.invocations, [])
    }

    func testEnsureInstalledBootoutRewritesBootstrapWhenDrifted() async throws {
        let plistURL = tempDir.appendingPathComponent("a.plist")
        try "<plist>old</plist>".write(to: plistURL, atomically: true, encoding: .utf8)
        let fake = FakeLaunchctlClient()
        let agent = LaunchdAgent(label: "com.test.a", plistURL: plistURL, launchctl: fake)

        try await agent.ensureInstalled(plistContent: "<plist>new</plist>")

        XCTAssertEqual(fake.invocations.count, 2)
        XCTAssertTrue(fake.invocations[0].hasPrefix("bootout "))
        XCTAssertTrue(fake.invocations[1].hasPrefix("bootstrap "))
        // Verify the plist on disk got rewritten:
        XCTAssertEqual(try String(contentsOf: plistURL, encoding: .utf8), "<plist>new</plist>")
    }

    func testEnsureInstalledHandlesMissingPlistFile() async throws {
        let plistURL = tempDir.appendingPathComponent("never-existed.plist")
        let fake = FakeLaunchctlClient()
        let agent = LaunchdAgent(label: "com.test.a", plistURL: plistURL, launchctl: fake)

        try await agent.ensureInstalled(plistContent: "<plist>first</plist>")

        XCTAssertTrue(FileManager.default.fileExists(atPath: plistURL.path))
        XCTAssertEqual(fake.invocations.count, 2)
    }

    func testEnsureInstalledThrowsOnBootstrapFailure() async throws {
        let plistURL = tempDir.appendingPathComponent("a.plist")
        let fake = FakeLaunchctlClient()
        fake.nextBootstrapResult = LaunchctlResult(exitCode: 5, stdout: "", stderr: "bootstrap failed")
        let agent = LaunchdAgent(label: "com.test.a", plistURL: plistURL, launchctl: fake)

        do {
            try await agent.ensureInstalled(plistContent: "<plist>first</plist>")
            XCTFail("expected bootstrap failure")
        } catch let LaunchdAgentError.bootstrapFailed(r) {
            XCTAssertEqual(r.exitCode, 5)
        }
    }

    func testStartCallsKickstart() async throws {
        let fake = FakeLaunchctlClient()
        let agent = LaunchdAgent(label: "com.test.a", plistURL: tempDir.appendingPathComponent("a.plist"), launchctl: fake)
        try await agent.start()
        XCTAssertEqual(fake.invocations, ["kickstart gui/\(getuid())/com.test.a"])
    }

    func testStopCallsBootout() async throws {
        let fake = FakeLaunchctlClient()
        let agent = LaunchdAgent(label: "com.test.a", plistURL: tempDir.appendingPathComponent("a.plist"), launchctl: fake)
        try await agent.stop()
        XCTAssertEqual(fake.invocations, ["bootout gui/\(getuid())/com.test.a"])
    }

    func testStopTreatsNotFoundAsSuccess() async throws {
        let fake = FakeLaunchctlClient()
        fake.nextBootoutResult = LaunchctlResult(exitCode: 113, stdout: "", stderr: "Could not find service \"com.test.a\" in domain")
        let agent = LaunchdAgent(label: "com.test.a", plistURL: tempDir.appendingPathComponent("a.plist"), launchctl: fake)
        // Should NOT throw — the goal "service is not running" is met.
        try await agent.stop()
    }

    func testStopThrowsOnOtherBootoutErrors() async throws {
        let fake = FakeLaunchctlClient()
        fake.nextBootoutResult = LaunchctlResult(exitCode: 22, stdout: "", stderr: "EINVAL")
        let agent = LaunchdAgent(label: "com.test.a", plistURL: tempDir.appendingPathComponent("a.plist"), launchctl: fake)
        do {
            try await agent.stop()
            XCTFail("expected throw")
        } catch is LaunchdAgentError {
            // ok
        }
    }

    func testStatusReturnsLoadedWithPidWhenPrintShowsPid() async throws {
        let fake = FakeLaunchctlClient()
        fake.nextPrintResult = LaunchctlResult(
            exitCode: 0,
            stdout: "com.test.a = {\n\tpid = 12345\n\tstate = running\n}\n",
            stderr: "",
        )
        let agent = LaunchdAgent(label: "com.test.a", plistURL: tempDir.appendingPathComponent("a.plist"), launchctl: fake)
        let state = await agent.status()
        XCTAssertEqual(state, .loaded(pid: 12345))
    }

    func testStatusReturnsLoadedNilWhenPrintShowsNoPid() async throws {
        let fake = FakeLaunchctlClient()
        fake.nextPrintResult = LaunchctlResult(
            exitCode: 0,
            stdout: "com.test.a = {\n\tstate = waiting\n}\n",
            stderr: "",
        )
        let agent = LaunchdAgent(label: "com.test.a", plistURL: tempDir.appendingPathComponent("a.plist"), launchctl: fake)
        let state = await agent.status()
        XCTAssertEqual(state, .loaded(pid: nil))
    }

    func testStatusReturnsNotLoadedOnPrintFailure() async throws {
        let fake = FakeLaunchctlClient()
        fake.nextPrintResult = LaunchctlResult(exitCode: 113, stdout: "", stderr: "Could not find service")
        let agent = LaunchdAgent(label: "com.test.a", plistURL: tempDir.appendingPathComponent("a.plist"), launchctl: fake)
        let state = await agent.status()
        XCTAssertEqual(state, .notLoaded)
    }

    func testUninstallStopsAndDeletesPlist() async throws {
        let plistURL = tempDir.appendingPathComponent("a.plist")
        try "<plist>x</plist>".write(to: plistURL, atomically: true, encoding: .utf8)
        let fake = FakeLaunchctlClient()
        let agent = LaunchdAgent(label: "com.test.a", plistURL: plistURL, launchctl: fake)
        await agent.uninstall()
        XCTAssertFalse(FileManager.default.fileExists(atPath: plistURL.path))
        XCTAssertTrue(fake.invocations.contains(where: { $0.hasPrefix("bootout ") }))
    }
}

import XCTest
@testable import HarborClerkServer

final class LaunchctlClientTests: XCTestCase {
    func testFakeRecordsInvocations() async throws {
        let fake = FakeLaunchctlClient()
        _ = await fake.bootstrap(domain: "gui/501", plistPath: URL(fileURLWithPath: "/tmp/test.plist"))
        _ = await fake.bootout(serviceTarget: "gui/501/com.example")
        _ = await fake.kickstart(serviceTarget: "gui/501/com.example")
        _ = await fake.print_(serviceTarget: "gui/501/com.example")

        XCTAssertEqual(fake.invocations, [
            "bootstrap gui/501 /tmp/test.plist",
            "bootout gui/501/com.example",
            "kickstart gui/501/com.example",
            "print gui/501/com.example",
        ])
    }

    func testFakeReturnsScriptedResult() async throws {
        let fake = FakeLaunchctlClient()
        fake.nextBootstrapResult = LaunchctlResult(exitCode: 5, stdout: "", stderr: "bootstrap failed: domain not loaded")
        let r = await fake.bootstrap(domain: "gui/501", plistPath: URL(fileURLWithPath: "/x"))
        XCTAssertEqual(r.exitCode, 5)
        XCTAssertFalse(r.success)
        XCTAssertEqual(r.stderr, "bootstrap failed: domain not loaded")
    }

    func testFakeDefaultsToSuccess() async throws {
        let fake = FakeLaunchctlClient()
        let r = await fake.bootout(serviceTarget: "anything")
        XCTAssertTrue(r.success)
    }
}

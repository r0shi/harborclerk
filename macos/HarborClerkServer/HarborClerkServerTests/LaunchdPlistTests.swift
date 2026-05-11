import XCTest
@testable import HarborClerkServer

final class LaunchdPlistTests: XCTestCase {
    private let testBundle = URL(fileURLWithPath: "/Applications/HarborClerkServer.app/Contents/Resources")
    private let testDataDir = URL(fileURLWithPath: "/Users/test/Library/Application Support/Harbor Clerk/postgres-data")
    private let testLogsDir = URL(fileURLWithPath: "/Users/test/Library/Application Support/Harbor Clerk/logs")

    func testPostgresPlistIsValidXMLPropertyList() throws {
        let xml = LaunchdPlist.postgres(bundle: testBundle, dataDir: testDataDir, logsDir: testLogsDir, port: 5433)
        let data = xml.data(using: .utf8)!
        // PropertyListSerialization will throw if the XML doesn't parse
        // as a property list. That's our basic correctness check.
        let parsed = try PropertyListSerialization.propertyList(from: data, options: [], format: nil) as? [String: Any]
        XCTAssertNotNil(parsed)
        XCTAssertEqual(parsed?["Label"] as? String, "com.harborclerk.postgres")
    }

    func testPostgresPlistHasCorrectKeys() throws {
        let xml = LaunchdPlist.postgres(bundle: testBundle, dataDir: testDataDir, logsDir: testLogsDir, port: 5433)
        let parsed = try PropertyListSerialization.propertyList(from: xml.data(using: .utf8)!, options: [], format: nil) as! [String: Any]

        XCTAssertEqual(parsed["Label"] as? String, "com.harborclerk.postgres")
        XCTAssertEqual(parsed["Program"] as? String, testBundle.appendingPathComponent("postgres/bin/postgres").path)
        XCTAssertEqual(parsed["RunAtLoad"] as? Bool, false)
        let keepAlive = parsed["KeepAlive"] as? [String: Any]
        XCTAssertEqual(keepAlive?["SuccessfulExit"] as? Bool, false)
        let env = parsed["EnvironmentVariables"] as? [String: String]
        XCTAssertEqual(env?["PGDATA"], testDataDir.path)
    }

    func testPostgresPlistArgsIncludePortAndDataDir() throws {
        let xml = LaunchdPlist.postgres(bundle: testBundle, dataDir: testDataDir, logsDir: testLogsDir, port: 5433)
        let parsed = try PropertyListSerialization.propertyList(from: xml.data(using: .utf8)!, options: [], format: nil) as! [String: Any]
        let args = parsed["ProgramArguments"] as! [String]
        XCTAssertTrue(args.contains("5433"))
        XCTAssertTrue(args.contains(testDataDir.path))
        XCTAssertTrue(args.contains("-D"))
        XCTAssertTrue(args.contains("-p"))
    }

    func testTikaPlistIsValidPropertyList() throws {
        let xml = LaunchdPlist.tika(bundle: testBundle, logsDir: testLogsDir, port: 9998)
        let parsed = try PropertyListSerialization.propertyList(from: xml.data(using: .utf8)!, options: [], format: nil) as! [String: Any]
        XCTAssertEqual(parsed["Label"] as? String, "com.harborclerk.tika")
        let env = parsed["EnvironmentVariables"] as? [String: String]
        XCTAssertEqual(env?["JAVA_HOME"], testBundle.appendingPathComponent("java/Contents/Home").path)
    }

    func testTikaPlistArgsIncludePort() throws {
        let xml = LaunchdPlist.tika(bundle: testBundle, logsDir: testLogsDir, port: 9998)
        let parsed = try PropertyListSerialization.propertyList(from: xml.data(using: .utf8)!, options: [], format: nil) as! [String: Any]
        let args = parsed["ProgramArguments"] as! [String]
        XCTAssertTrue(args.contains("9998"))
        XCTAssertTrue(args.contains("--host"))
        XCTAssertTrue(args.contains("127.0.0.1"))
    }
}

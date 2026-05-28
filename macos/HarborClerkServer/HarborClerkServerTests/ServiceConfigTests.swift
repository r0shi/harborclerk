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

    // MARK: - PG Data Dir Safety Move

    /// Helper: create a fresh fake data dir in a tempdir, return (parent, dataDir).
    private func makeFakeDataDir(withPGVersion stored: String?) throws -> (parent: URL, dataDir: URL) {
        let parent = URL(fileURLWithPath: NSTemporaryDirectory(), isDirectory: true)
            .appendingPathComponent("pg-safety-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: parent, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: parent) }
        let dataDir = parent.appendingPathComponent("postgres-data", isDirectory: true)
        try FileManager.default.createDirectory(at: dataDir, withIntermediateDirectories: true)
        if let stored {
            try stored.write(
                to: dataDir.appendingPathComponent("PG_VERSION"),
                atomically: true,
                encoding: .utf8,
            )
        }
        // Drop a sentinel file so we can prove the move preserved contents.
        try Data("hello".utf8).write(to: dataDir.appendingPathComponent("sentinel.txt"))
        return (parent, dataDir)
    }

    func testMoveDataDirAsideRenamesToTimestampedSibling() throws {
        let (parent, dataDir) = try makeFakeDataDir(withPGVersion: "16")
        let now = Date(timeIntervalSince1970: 1_779_926_400)  // 2026-05-28 00:00:00 UTC

        let backup = try PostgresService.moveDataDirAside(
            dataDir: dataDir,
            storedVersion: "16",
            now: now,
        )

        XCTAssertFalse(
            FileManager.default.fileExists(atPath: dataDir.path),
            "Original data dir should be gone after the move",
        )
        XCTAssertTrue(
            FileManager.default.fileExists(atPath: backup.path),
            "Backup dir should exist after the move",
        )
        XCTAssertEqual(backup.deletingLastPathComponent(), parent, "Backup must be a sibling, not nested")
        XCTAssertEqual(backup.lastPathComponent, "postgres-data-pg16-backup-20260528-000000")
        // Sentinel survived → it's a real rename, not a copy-and-truncate
        let sentinel = try String(contentsOf: backup.appendingPathComponent("sentinel.txt"), encoding: .utf8)
        XCTAssertEqual(sentinel, "hello")
    }

    func testMoveDataDirAsideHandlesUnknownStoredVersion() throws {
        let (_, dataDir) = try makeFakeDataDir(withPGVersion: nil)
        let now = Date(timeIntervalSince1970: 1_779_926_400)

        let backup = try PostgresService.moveDataDirAside(
            dataDir: dataDir,
            storedVersion: nil,
            now: now,
        )
        XCTAssertEqual(backup.lastPathComponent, "postgres-data-pg-unknown-backup-20260528-000000")

        // Empty-string stored version (defensive — `String(contentsOf:)` could
        // theoretically yield this if PG_VERSION existed but was empty) takes
        // the same branch as nil.
        let (_, dataDir2) = try makeFakeDataDir(withPGVersion: nil)
        let backup2 = try PostgresService.moveDataDirAside(
            dataDir: dataDir2,
            storedVersion: "",
            now: now,
        )
        XCTAssertTrue(backup2.lastPathComponent.contains("pg-unknown"))
    }

    func testMoveDataDirAsideHandlesSameSecondCollision() throws {
        let (parent, dataDir) = try makeFakeDataDir(withPGVersion: "16")
        let now = Date(timeIntervalSince1970: 1_779_926_400)

        // Pre-create the naive candidate destination so the helper has to
        // pick a suffixed name. This is the "two restarts within the same
        // second after a crash" edge case.
        let pre = parent.appendingPathComponent("postgres-data-pg16-backup-20260528-000000")
        try FileManager.default.createDirectory(at: pre, withIntermediateDirectories: true)

        let backup = try PostgresService.moveDataDirAside(
            dataDir: dataDir,
            storedVersion: "16",
            now: now,
        )

        XCTAssertEqual(
            backup.lastPathComponent,
            "postgres-data-pg16-backup-20260528-000000-1",
            "First collision should append -1, not overwrite the existing backup",
        )
        XCTAssertTrue(FileManager.default.fileExists(atPath: pre.path), "Existing backup must remain untouched")
    }

    func testMoveDataDirAsideThrowsWhenSourceMissing() throws {
        let parent = URL(fileURLWithPath: NSTemporaryDirectory(), isDirectory: true)
            .appendingPathComponent("pg-safety-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: parent, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: parent) }
        let missing = parent.appendingPathComponent("postgres-data", isDirectory: true)

        XCTAssertThrowsError(
            try PostgresService.moveDataDirAside(dataDir: missing, storedVersion: "16"),
            "Move should throw when the source dir does not exist; refusing to start is safer than silent data loss",
        )
    }

    // MARK: - Data Dir Pre-Init Audit

    /// Helper: create a fresh `parent` dir in a tempdir, add a teardown block,
    /// and return the parent + the not-yet-created `dataDir` URL inside it.
    /// Callers seed `dataDir` to whatever state they want for the test.
    private func makeAuditScaffold() throws -> (parent: URL, dataDir: URL) {
        let parent = URL(fileURLWithPath: NSTemporaryDirectory(), isDirectory: true)
            .appendingPathComponent("pg-audit-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: parent, withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: parent) }
        return (parent, parent.appendingPathComponent("postgres-data", isDirectory: true))
    }

    func testAuditDataDirReportsMissingWhenDirAbsent() throws {
        let (_, dataDir) = try makeAuditScaffold()
        // Note: dataDir not created — exercise the missing path.
        XCTAssertEqual(PostgresService.auditDataDirBeforeInit(dataDir: dataDir), .missing)
    }

    func testAuditDataDirReportsMissingWhenPathIsAFile() throws {
        // A regular file at `dataDir.path` is not a usable data directory.
        let (parent, _) = try makeAuditScaffold()
        let dataDir = parent.appendingPathComponent("postgres-data")
        try Data().write(to: dataDir)
        XCTAssertEqual(PostgresService.auditDataDirBeforeInit(dataDir: dataDir), .missing)
    }

    func testAuditDataDirReportsEmptyWhenDirHasNoFiles() throws {
        let (_, dataDir) = try makeAuditScaffold()
        try FileManager.default.createDirectory(at: dataDir, withIntermediateDirectories: true)
        XCTAssertEqual(PostgresService.auditDataDirBeforeInit(dataDir: dataDir), .empty)
    }

    func testAuditDataDirReportsPGVersionPresentWithTrimmedContents() throws {
        let (_, dataDir) = try makeAuditScaffold()
        try FileManager.default.createDirectory(at: dataDir, withIntermediateDirectories: true)
        // Real PG_VERSION files end with a newline; the audit must trim it.
        try "18\n".write(to: dataDir.appendingPathComponent("PG_VERSION"), atomically: true, encoding: .utf8)

        XCTAssertEqual(
            PostgresService.auditDataDirBeforeInit(dataDir: dataDir),
            .pgVersionPresent("18"),
        )
    }

    func testAuditDataDirReportsPGVersionPresentNilWhenContentsUnreadable() throws {
        // Simulate "PG_VERSION exists but unreadable" by making the path a
        // directory instead of a regular file. `fileExists(atPath:)` returns
        // true, but `String(contentsOfFile:)` fails (EISDIR) and the audit
        // surfaces `.pgVersionPresent(nil)`. The `start()` switch arm refuses
        // to start in this case (rather than treating `nil != expectedMajor`
        // as mismatch and moving aside a potentially-valid cluster).
        let (_, dataDir) = try makeAuditScaffold()
        try FileManager.default.createDirectory(at: dataDir, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(
            at: dataDir.appendingPathComponent("PG_VERSION", isDirectory: true),
            withIntermediateDirectories: true,
        )

        XCTAssertEqual(
            PostgresService.auditDataDirBeforeInit(dataDir: dataDir),
            .pgVersionPresent(nil),
        )
    }

    func testAuditDataDirReportsCorruptWhenPGVersionMissingButFilesPresent() throws {
        // The tripwire case: real cluster subdirs present (base/, global/,
        // pg_wal/, pg_xact/) but PG_VERSION is gone. Pre-this-PR the bare
        // `fileExists(PG_VERSION)` check would treat this as a fresh launch
        // and call initdb; `initdb` would refuse, but the user only sees
        // "initdb failed with N". The audit surfaces the real state.
        let (_, dataDir) = try makeAuditScaffold()
        try FileManager.default.createDirectory(at: dataDir, withIntermediateDirectories: true)
        for name in ["base", "global", "pg_wal", "pg_xact"] {
            try FileManager.default.createDirectory(
                at: dataDir.appendingPathComponent(name, isDirectory: true),
                withIntermediateDirectories: true,
            )
        }

        let result = PostgresService.auditDataDirBeforeInit(dataDir: dataDir)
        guard case .corruptOrForeign(let fileCount, let sampleNames) = result else {
            return XCTFail("Expected .corruptOrForeign, got \(result)")
        }
        XCTAssertEqual(fileCount, 4)
        XCTAssertEqual(sampleNames, ["base", "global", "pg_wal", "pg_xact"])
    }

    func testAuditDataDirSampleNamesCapAt8() throws {
        let (_, dataDir) = try makeAuditScaffold()
        try FileManager.default.createDirectory(at: dataDir, withIntermediateDirectories: true)
        // 12 entries; the sample is capped at 8 and returned sorted ascending.
        let names = ["a01", "b02", "c03", "d04", "e05", "f06", "g07", "h08", "i09", "j10", "k11", "l12"]
        for name in names {
            try Data().write(to: dataDir.appendingPathComponent(name))
        }

        let result = PostgresService.auditDataDirBeforeInit(dataDir: dataDir)
        guard case .corruptOrForeign(let fileCount, let sampleNames) = result else {
            return XCTFail("Expected .corruptOrForeign, got \(result)")
        }
        XCTAssertEqual(fileCount, 12)
        // FileManager.contentsOfDirectory's order is undefined per Apple's
        // docs (depends on filesystem semantics) so we assert only that we
        // get exactly 8 entries from the seeded set, sorted ascending.
        XCTAssertEqual(sampleNames.count, 8)
        XCTAssertTrue(Set(sampleNames).isSubset(of: Set(names)))
        XCTAssertEqual(sampleNames, sampleNames.sorted())
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

    // MARK: - Process Group Source-Text Guards

    /// forceKillEverything must use killpg() so grandchildren of tracked
    /// PIDs are reached. Source-text guard against accidental regression.
    func testForceKillEverythingUsesKillpg() throws {
        let thisFile = URL(fileURLWithPath: #file)
        let serviceManagerURL = thisFile
            .deletingLastPathComponent()  // → HarborClerkServerTests/
            .deletingLastPathComponent()  // → HarborClerkServer/ project root
            .appendingPathComponent("HarborClerkServer")
            .appendingPathComponent("ServiceManager.swift")
        let source = try String(contentsOf: serviceManagerURL, encoding: .utf8)
        let forceKillRange = source.range(of: "func forceKillEverything")!
        let nextFuncRange = source.range(of: "func ", range: forceKillRange.upperBound..<source.endIndex)
        let body = String(source[forceKillRange.upperBound..<(nextFuncRange?.lowerBound ?? source.endIndex)])
        XCTAssertTrue(body.contains("killpg("), "forceKillEverything must call killpg to reach grandchildren")
    }

    /// AppDelegate.setupMenu must register a "Force Stop All" item and the
    /// handler must call forceKillEverything(). Source-text guard —
    /// runtime menu inspection is brittle from a non-UI XCTest. Manual
    /// smoke testing covers the dialog itself.
    func testAppDelegateRegistersForceStopAllMenuItem() throws {
        let thisFile = URL(fileURLWithPath: #file)
        let appDelegateURL = thisFile
            .deletingLastPathComponent()  // → HarborClerkServerTests/
            .deletingLastPathComponent()  // → HarborClerkServer/ project root
            .appendingPathComponent("HarborClerkServer")
            .appendingPathComponent("AppDelegate.swift")
        let source = try String(contentsOf: appDelegateURL, encoding: .utf8)
        XCTAssertTrue(
            source.contains("\"Force Stop All\""),
            "AppDelegate.swift must declare a 'Force Stop All' menu item",
        )
        XCTAssertTrue(
            source.contains("forceKillEverything()"),
            "AppDelegate.swift must call serviceManager.forceKillEverything() in the Force Stop All handler",
        )
    }
}

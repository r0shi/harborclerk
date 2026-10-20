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

    // MARK: - Runtime caches stay out of the signed bundle (#846)

    /// The write-cache variables the launch paths must keep out of the bundle,
    /// written out here rather than read from the production list, so
    /// shrinking that list fails a test.
    private static let writeCacheVariables = [
        "NUMBA_CACHE_DIR", "PYTHONPYCACHEPREFIX", "XDG_CACHE_HOME", "MPLCONFIGDIR", "TORCH_HOME",
        "HF_HOME", "HF_HUB_CACHE", "TRANSFORMERS_CACHE", "SENTENCE_TRANSFORMERS_HOME",
    ]
    /// Read-only data paths: a bundle entry there is shipped data, and stays.
    private static let readOnlyDataVariables = ["NLTK_DATA"]

    private static func canonical(_ path: String) -> String {
        URL(fileURLWithPath: path).resolvingSymlinksInPath().standardizedFileURL.path + "/"
    }

    private static var bundleRoot: String { canonical(Bundle.main.bundleURL.path) }

    /// Whether `value`, or any entry of it as a colon-separated list, lies in the bundle.
    private static func pointsIntoBundle(_ value: String) -> Bool {
        value.split(separator: ":").contains {
            canonical(String($0)).hasPrefix(bundleRoot) || ServiceManager.isPath(String($0), inside: Bundle.main.bundleURL)
        }
    }

    /// A scratch folder for one test, removed afterwards (made writable first,
    /// since the fallback tests take write permission away).
    private func makeScratch() -> URL {
        let scratch = FileManager.default.temporaryDirectory
            .appendingPathComponent("cache-env-\(UUID().uuidString)", isDirectory: true)
        try? FileManager.default.createDirectory(at: scratch, withIntermediateDirectories: true)
        addTeardownBlock {
            let fm = FileManager.default
            // 0o500 directories can still be listed, so every one is reached.
            let dirs = (fm.enumerator(atPath: scratch.path)?.allObjects as? [String] ?? [])
                .map { scratch.appendingPathComponent($0).path } + [scratch.path]
            for dir in dirs {
                try? fm.setAttributes([.posixPermissions: 0o700], ofItemAtPath: dir)
            }
            try? fm.removeItem(at: scratch)
        }
        return scratch
    }

    /// Points the app's Caches folder at `dir` for this test only, so the
    /// launch paths run with their production defaults and write nothing
    /// into the user's Caches.
    private func overrideCachesDir(_ dir: URL) {
        AppSettings.cachesDirOverride = dir
        addTeardownBlock { AppSettings.cachesDirOverride = nil }
    }

    /// Every environment a Python process the server launches is given, built
    /// by the launch paths themselves: each service's `launchEnvironment()`
    /// over the shared dict, and Alembic's. Scratch settings and a dummy key
    /// throughout (the embedder and reranker read theirs through
    /// `settingsOverride`), so neither the live config.json nor the Keychain is
    /// read. The override service sets all ten cache variables to bundle paths
    /// in both its base dict and its own overrides.
    private func launchedPythonEnvironments(scratch: URL) -> [(who: String, env: [String: String])] {
        let settings = AppSettings(configURL: scratch.appendingPathComponent("config.json"))
        let resources = Bundle.main.resourceURL!
        let shared = ServiceManager.pythonEnvironment(settings: settings, bundle: resources, masterKeyB64: "dGVzdA==")
        let services: [PythonService] = [
            APIService(), EmbedderService(), RerankerService(), WorkerService(queue: "io", index: 0), WatcherService(),
            BundleCacheOverrideService(),
        ]
        var envs: [(who: String, env: [String: String])] = services.map { svc in
            svc.settingsOverride = settings
            svc.baseEnvironment = shared
            if svc is BundleCacheOverrideService {
                svc.baseEnvironment.merge(BundleCacheOverrideService.bundleCaches) { _, bundle in bundle }
            }
            return (svc.name, svc.launchEnvironment())
        }
        envs.append(("Alembic", MigrationRunner.environment(settings: settings, bundle: resources)))
        return envs
    }

    func testTheProductionListsSplitWriteCachesFromReadOnlyDataPaths() {
        XCTAssertTrue(Set(ServiceManager.writeCacheVariables).isSuperset(of: Self.writeCacheVariables))
        XCTAssertEqual(Set(ServiceManager.readOnlyDataVariables), Set(Self.readOnlyDataVariables))
        XCTAssertTrue(Set(ServiceManager.writeCacheVariables).isDisjoint(with: ServiceManager.readOnlyDataVariables))
    }

    func testEveryLaunchedPythonProcessKeepsItsCachesOutOfTheBundle() throws {
        let scratch = makeScratch()
        let cachesDir = scratch.appendingPathComponent("Caches/Harbor Clerk", isDirectory: true)
        overrideCachesDir(cachesDir)
        let expected = cachesDir.appendingPathComponent("numba").path

        for (who, env) in launchedPythonEnvironments(scratch: scratch) {
            XCTAssertEqual(env["PYTHONDONTWRITEBYTECODE"], "1", "\(who): bytecode would be written beside the bundled sources")

            let numba = try XCTUnwrap(
                env["NUMBA_CACHE_DIR"],
                "\(who): without NUMBA_CACHE_DIR numba caches into venv/.../__pycache__ inside the signed bundle")
            XCTAssertEqual(numba, expected, "\(who): the launch path must use AppSettings.cachesDir")
            var isDir: ObjCBool = false
            XCTAssertTrue(
                FileManager.default.fileExists(atPath: numba, isDirectory: &isDir) && isDir.boolValue,
                "\(who): the numba cache directory must exist before the process starts")
            XCTAssertEqual(env["NUMBA_CACHE_LOCATOR_CLASSES"], "UserProvidedCacheLocator",
                           "\(who): numba must never fall back to the in-tree locator")
            XCTAssertNil(env["NUMBA_DISABLE_JIT"], "\(who): the JIT stays on while a cache directory is usable")

            for key in Self.writeCacheVariables {
                guard let value = env[key] else { continue }
                XCTAssertFalse(Self.pointsIntoBundle(value), "\(who): \(key)=\(value) is inside the app bundle")
            }
        }

        let overridden = try XCTUnwrap(launchedPythonEnvironments(scratch: scratch).first { $0.who == "BundleCacheOverride" })
        for key in Self.writeCacheVariables where key != "NUMBA_CACHE_DIR" {
            XCTAssertNil(overridden.env[key], "\(key) pointed into the bundle and must be dropped, not kept")
        }
        XCTAssertEqual(overridden.env["NLTK_DATA"], BundleCacheOverrideService.bundleCaches["NLTK_DATA"],
                       "a read-only data path into the bundle is shipped data and stays")
    }

    /// Only a bundle path is dropped; the same variable pointing elsewhere is
    /// the operator's choice and stays.
    func testOnlyCachePathsInsideTheBundleAreDropped() {
        let scratch = makeScratch()
        overrideCachesDir(scratch.appendingPathComponent("Caches/Harbor Clerk", isDirectory: true))
        let inside = Bundle.main.resourceURL!.appendingPathComponent("venv").path

        let env = ServiceManager.applyingRuntimeCacheRules(to: [
            "HF_HOME": "/Users/someone/.cache/huggingface",
            "TORCH_HOME": inside,
            "XDG_CACHE_HOME": "/Users/someone/.cache:\(inside)",
            "NLTK_DATA": "/usr/share/nltk_data:\(inside)",
            "DATABASE_URL": "kept",
        ])

        XCTAssertEqual(env["HF_HOME"], "/Users/someone/.cache/huggingface")
        XCTAssertNil(env["TORCH_HOME"])
        XCTAssertNil(env["XDG_CACHE_HOME"], "one bundle entry in a path list is enough to drop a write cache")
        XCTAssertEqual(env["NLTK_DATA"], "/usr/share/nltk_data:\(inside)", "a read-only data path is left alone")
        XCTAssertEqual(env["DATABASE_URL"], "kept")
    }

    /// The last-resort directory is resolved once per app run: two launches
    /// in the fallback case get the same one, and `mkdtemp` runs once.
    func testTwoLaunchesInTheFallbackCaseShareOneDirectory() throws {
        let scratch = makeScratch()
        let locked = try lockedDir(in: scratch)
        overrideCachesDir(locked.appendingPathComponent("Caches/Harbor Clerk"))
        RuntimeCacheResolution.resetForTesting()
        addTeardownBlock { RuntimeCacheResolution.resetForTesting() }
        RuntimeCacheResolution.fallbackDirsOverride = [locked.appendingPathComponent("support")]
        var made = 0
        RuntimeCacheResolution.makeTemporaryDirOverride = {
            made += 1
            return scratch.appendingPathComponent("fresh-\(made)", isDirectory: true)
        }

        let first = WorkerService(queue: "io", index: 0).launchEnvironment()
        let second = APIService().launchEnvironment()
        let alembic = MigrationRunner.environment(
            settings: AppSettings(configURL: scratch.appendingPathComponent("config.json")), bundle: Bundle.main.resourceURL!)

        XCTAssertEqual(first["NUMBA_CACHE_DIR"], scratch.appendingPathComponent("fresh-1").path)
        XCTAssertEqual(second["NUMBA_CACHE_DIR"], first["NUMBA_CACHE_DIR"])
        XCTAssertEqual(alembic["NUMBA_CACHE_DIR"], first["NUMBA_CACHE_DIR"])
        XCTAssertEqual(made, 1, "one temporary directory per app run, not one per launch")
    }

    // MARK: An unusable cache directory falls back outside the bundle

    /// A directory under `scratch` that cannot be written (0o500).
    private func lockedDir(in scratch: URL) throws -> URL {
        let locked = scratch.appendingPathComponent("locked-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: locked, withIntermediateDirectories: true)
        try FileManager.default.setAttributes([.posixPermissions: 0o500], ofItemAtPath: locked.path)
        return locked
    }

    /// numba falls back to the source's `__pycache__`, in the bundle, when its
    /// directory fails a test write; the app must hand it a writable one.
    func testACacheDirectoryThatCannotBeCreatedFallsBack() throws {
        let scratch = makeScratch()
        let locked = try lockedDir(in: scratch)
        let fallback = scratch.appendingPathComponent("fallback", isDirectory: true)

        let env = ServiceManager.runtimeCacheEnvironment(
            cachesDir: locked.appendingPathComponent("Caches/Harbor Clerk"),
            fallbackDirs: [fallback], makeTemporaryDir: { nil })

        XCTAssertEqual(env["NUMBA_CACHE_DIR"], fallback.path)
        XCTAssertTrue(FileManager.default.isWritableFile(atPath: fallback.path), "the fallback is created and writable")
    }

    func testACacheDirectoryThatExistsButCannotBeWrittenFallsBack() throws {
        let scratch = makeScratch()
        let caches = scratch.appendingPathComponent("Caches/Harbor Clerk", isDirectory: true)
        let numba = caches.appendingPathComponent("numba", isDirectory: true)
        try FileManager.default.createDirectory(at: numba, withIntermediateDirectories: true)
        try FileManager.default.setAttributes([.posixPermissions: 0o500], ofItemAtPath: numba.path)
        let fallback = scratch.appendingPathComponent("fallback", isDirectory: true)

        let env = ServiceManager.runtimeCacheEnvironment(cachesDir: caches, fallbackDirs: [fallback], makeTemporaryDir: { nil })

        XCTAssertEqual(env["NUMBA_CACHE_DIR"], fallback.path, "exists is not enough: numba's probe write would fail there")
    }

    /// The candidates are tried in order, and the fresh temporary directory
    /// is made only when every one of them has failed.
    func testTheCandidatesAreTriedInOrderThenAFreshTemporaryDirectory() throws {
        let scratch = makeScratch()
        let locked = try lockedDir(in: scratch)
        let second = scratch.appendingPathComponent("second", isDirectory: true)
        var made = 0

        let env = ServiceManager.runtimeCacheEnvironment(
            cachesDir: locked.appendingPathComponent("caches"),
            fallbackDirs: [locked.appendingPathComponent("first"), second],
            makeTemporaryDir: { made += 1; return nil })
        XCTAssertEqual(env["NUMBA_CACHE_DIR"], second.path, "the first usable candidate wins")
        XCTAssertEqual(made, 0, "no temporary directory while a candidate works")

        let fresh = scratch.appendingPathComponent("fresh", isDirectory: true)
        let last = ServiceManager.runtimeCacheEnvironment(
            cachesDir: locked.appendingPathComponent("caches"),
            fallbackDirs: [locked.appendingPathComponent("first"), locked.appendingPathComponent("second")],
            makeTemporaryDir: { fresh })
        XCTAssertEqual(last["NUMBA_CACHE_DIR"], fresh.path)
    }

    /// The production order: Application Support (the app's own data folder)
    /// before `$TMPDIR`, both outside the bundle, and a fresh directory
    /// under /tmp after them.
    func testTheProductionFallbacksAreAppSupportThenTMPDIRThenAFreshDirectory() throws {
        let dirs = ServiceManager.runtimeCacheFallbackDirs
        XCTAssertEqual(dirs.count, 2)
        XCTAssertTrue(dirs[0].path.hasPrefix(AppSettings.dataDir.path + "/"), "\(dirs[0].path) is not in Application Support")
        XCTAssertTrue(dirs[1].path.hasPrefix(FileManager.default.temporaryDirectory.path), "\(dirs[1].path) is not in $TMPDIR")
        for dir in dirs {
            XCTAssertFalse(ServiceManager.isPath(dir.path, inside: Bundle.main.bundleURL), "\(dir.path) is inside the bundle")
        }
        let fresh = try XCTUnwrap(ServiceManager.makeFreshTemporaryDir())
        addTeardownBlock { try? FileManager.default.removeItem(at: fresh) }
        XCTAssertTrue(fresh.path.hasPrefix("/private/tmp/harbor-clerk-numba."))
        XCTAssertTrue(FileManager.default.isWritableFile(atPath: fresh.path))
    }

    /// Nothing usable anywhere: numba is not handed a directory, and stays
    /// held to the user-provided locator, so it refuses to cache (and umap
    /// does not import) rather than write into the bundle. The JIT is left
    /// alone: turning it off does not stop numba caching `@vectorize`
    /// functions, so it would not let umap import either.
    func testWithNoUsableDirectoryNumbaIsLeftWithoutOne() throws {
        let scratch = makeScratch()
        let locked = try lockedDir(in: scratch)

        let env = ServiceManager.runtimeCacheEnvironment(
            cachesDir: locked.appendingPathComponent("Caches/Harbor Clerk"),
            fallbackDirs: [locked.appendingPathComponent("support"), locked.appendingPathComponent("tmp")],
            makeTemporaryDir: { locked.appendingPathComponent("fresh") })

        XCTAssertNil(env["NUMBA_CACHE_DIR"])
        XCTAssertNil(env["NUMBA_DISABLE_JIT"])
        XCTAssertEqual(env["NUMBA_CACHE_LOCATOR_CLASSES"], "UserProvidedCacheLocator", "never the in-tree locator")
        XCTAssertEqual(env["PYTHONDONTWRITEBYTECODE"], "1")
    }

    func testAWritableCacheDirectoryIsUsedAndLeftClean() throws {
        let scratch = makeScratch()
        let caches = scratch.appendingPathComponent("Caches/Harbor Clerk", isDirectory: true)
        let fallback = scratch.appendingPathComponent("fallback", isDirectory: true)

        let env = ServiceManager.runtimeCacheEnvironment(cachesDir: caches, fallbackDirs: [fallback], makeTemporaryDir: { nil })

        let numba = caches.appendingPathComponent("numba")
        XCTAssertEqual(env["NUMBA_CACHE_DIR"], numba.path)
        XCTAssertNil(env["NUMBA_DISABLE_JIT"])
        XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: numba.path), [], "the write probe is removed")
        XCTAssertFalse(FileManager.default.fileExists(atPath: fallback.path))
    }

    // MARK: The launch call sites use the launch environment

    /// `start()` itself, with the process captured instead of spawned: the
    /// environment it hands the process is the launch environment, not one
    /// it builds on its own.
    func testPythonServiceStartLaunchesWithTheLaunchEnvironment() throws {
        let scratch = makeScratch()
        let cachesDir = scratch.appendingPathComponent("Caches/Harbor Clerk", isDirectory: true)
        overrideCachesDir(cachesDir)
        let svc = WorkerService(queue: "io", index: 0)
        svc.baseEnvironment = ["NUMBA_CACHE_DIR": Bundle.main.resourceURL!.appendingPathComponent("venv").path, "KEEP": "1"]
        var launched: [String: String]?
        svc.launcher = { proc in launched = proc.environment }

        try awaitStart(svc)

        let env = try XCTUnwrap(launched, "start() did not reach the launcher")
        XCTAssertEqual(env["NUMBA_CACHE_DIR"], cachesDir.appendingPathComponent("numba").path)
        XCTAssertEqual(env["NUMBA_CACHE_LOCATOR_CLASSES"], "UserProvidedCacheLocator")
        XCTAssertEqual(env["KEEP"], "1")
    }

    /// `PythonService.start()` is async; run it to completion with a bound.
    private func awaitStart(_ svc: PythonService) throws {
        let done = expectation(description: "start() returned")
        let finish = ResumeOnce()
        var failure: Error?
        Task {
            var error: Error?
            do { try await svc.start() } catch let e { error = e }
            // Late, after the timeout claimed `finish`: no write, no fulfil.
            guard finish.claim() else { return }
            failure = error
            done.fulfill()
        }
        wait(for: [done], timeout: 10)
        _ = finish.claim()
        if let failure { throw failure }
    }

    /// `MigrationRunner.run()` itself, with the process captured: Alembic gets
    /// the cache rules too.
    func testMigrationRunnerRunLaunchesWithTheCacheRules() throws {
        let scratch = makeScratch()
        let cachesDir = scratch.appendingPathComponent("Caches/Harbor Clerk", isDirectory: true)
        overrideCachesDir(cachesDir)
        let settings = AppSettings(configURL: scratch.appendingPathComponent("config.json"))
        var launched: [String: String]?
        let runner = MigrationRunner(settings: settings) { proc in
            launched = proc.environment
            return 0
        }

        let done = expectation(description: "run() returned")
        let finish = ResumeOnce()
        var failure: Error?
        Task {
            var error: Error?
            do { try await runner.run() } catch let e { error = e }
            // Late, after the timeout claimed `finish`: no write, no fulfil.
            guard finish.claim() else { return }
            failure = error
            done.fulfill()
        }
        wait(for: [done], timeout: 10)
        _ = finish.claim()
        XCTAssertNil(failure)

        let env = try XCTUnwrap(launched, "run() did not reach the launcher")
        XCTAssertEqual(env["NUMBA_CACHE_DIR"], cachesDir.appendingPathComponent("numba").path)
        XCTAssertEqual(env["NUMBA_CACHE_LOCATOR_CLASSES"], "UserProvidedCacheLocator")
        XCTAssertEqual(env["PYTHONDONTWRITEBYTECODE"], "1")
        XCTAssertNotNil(env["DATABASE_URL"])
    }

    func testTheCachesDirectoryIsNotInsideTheBundle() {
        let bundleRoot = Bundle.main.bundleURL.standardizedFileURL.path + "/"
        let caches = AppSettings.cachesDir.standardizedFileURL.path + "/"
        XCTAssertFalse(caches.hasPrefix(bundleRoot), "\(caches) is inside \(bundleRoot)")
        XCTAssertTrue(caches.contains("/Library/Caches/"), "expected the user's Caches folder, got \(caches)")
    }
}

/// A service whose base dict and own overrides both point all ten cache
/// variables into the bundle (and turn bytecode writing back on): the launch
/// environment must keep none of them (#846).
final class BundleCacheOverrideService: PythonService {
    static let bundleCaches: [String: String] = {
        let resources = Bundle.main.resourceURL!
        let venv = resources.appendingPathComponent("venv/lib/python3.12/site-packages").path
        var env = ["PYTHONDONTWRITEBYTECODE": "0"]
        for key in ["NUMBA_CACHE_DIR", "PYTHONPYCACHEPREFIX", "HF_HOME", "HF_HUB_CACHE", "TRANSFORMERS_CACHE",
                    "SENTENCE_TRANSFORMERS_HOME", "XDG_CACHE_HOME", "MPLCONFIGDIR", "TORCH_HOME"] {
            env[key] = venv + "/" + key.lowercased()
        }
        env["NLTK_DATA"] = "/usr/share/nltk_data:" + resources.appendingPathComponent("nltk").path
        return env
    }()

    init() { super.init(name: "BundleCacheOverride") }

    override var executableName: String { "harbor-clerk-none" }
    override var extraEnvironment: [String: String] { Self.bundleCaches }
}

/// The test host must start nothing: two independent guards stand between a
/// launch under `XCTestConfigurationFilePath` and the live stack (Keychain,
/// the live app's child pids, its ports and its launchd agents). Each guard is
/// tested with a stand-in for what it protects, so breaking one fails a test
/// without the other ever letting anything live run.
@MainActor
final class TestHostGuardTests: XCTestCase {
    private static let switchSet = ["XCTestConfigurationFilePath": "/dev/null"]

    /// This very process: the runner set the switch, and the launch stopped
    /// at the first guard. Breaking the first guard moves it to the second.
    /// Only `applicationDidFinishLaunching` writes the outcome, so the other
    /// tests here calling `launch` cannot make this one pass.
    func testThisTestHostStoppedAtTheLaunchGuard() {
        XCTAssertNotNil(ProcessInfo.processInfo.environment["XCTestConfigurationFilePath"])
        XCTAssertEqual(AppDelegate.launchOutcome, .stoppedAtLaunchGuard)
    }

    func testTheLaunchGuardNeverCallsBootUnderTheSwitch() {
        var booted = false
        AppDelegate.launch(environment: Self.switchSet) { booted = true }
        XCTAssertFalse(booted)

        AppDelegate.launch(environment: [:]) { booted = true }
        XCTAssertTrue(booted, "without the switch the stand-in boot runs")
    }

    func testTheBootGuardRefusesUnderTheSwitch() {
        XCTAssertFalse(AppDelegate.mayBootLiveStack(environment: Self.switchSet))
        XCTAssertTrue(AppDelegate.mayBootLiveStack(environment: [:]))
    }

    /// The second guard at its call site. Every side effect of booting goes
    /// through the stand-ins, which only record: never the real LiveBoot,
    /// because with the guard broken that would boot the live stack.
    func testTheBootGuardStopsEverySideEffectUnderTheSwitch() {
        var calls: [String] = []
        let recording = AppDelegate.LiveBoot(
            bootstrapMasterKey: { calls.append("masterKey") },
            startServices: { _ in calls.append("startServices") }
        )

        XCTAssertFalse(AppDelegate().bootLiveStack(environment: Self.switchSet, boot: recording))
        XCTAssertEqual(calls, [], "under the switch nothing may reach the Keychain or start a service")

        XCTAssertTrue(AppDelegate().bootLiveStack(environment: [:], boot: recording))
        XCTAssertEqual(calls, ["masterKey", "startServices"], "without it the stand-ins run, in order")
    }

    /// A host that never booted has nothing to stop; terminating it used to
    /// force-unwrap the service manager it never created.
    func testTerminatingAHostThatNeverBootedReturnsAtOnce() {
        XCTAssertEqual(AppDelegate().applicationShouldTerminate(NSApplication.shared), .terminateNow)
    }
}

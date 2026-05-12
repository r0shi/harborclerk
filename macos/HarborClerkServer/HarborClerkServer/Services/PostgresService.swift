import Foundation
import os

final class PostgresService: ManagedService {
    let name = "PostgreSQL"
    var state: ServiceState = .stopped
    /// True for the launchd-managed services. The HealthChecker uses
    /// this to skip its own auto-restart path — launchd's KeepAlive
    /// already handles crash recovery, and double-restarting would be
    /// a foot-gun.
    let isLaunchdManaged = true

    private let agent: LaunchdAgent

    init(launchctl: LaunchctlClient = RealLaunchctlClient()) {
        let plistURL = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/LaunchAgents/com.harborclerk.postgres.plist")
        self.agent = LaunchdAgent(
            label: "com.harborclerk.postgres",
            plistURL: plistURL,
            launchctl: launchctl,
        )
    }

    private var pgBinDir: URL {
        Bundle.main.resourceURL!.appendingPathComponent("postgres/bin")
    }
    private var pgShareDir: URL {
        Bundle.main.resourceURL!.appendingPathComponent("postgres/share")
    }
    private var pgLibDir: URL {
        Bundle.main.resourceURL!.appendingPathComponent("postgres/lib")
    }
    private var dataDir: URL { AppSettings.shared.postgresDataDir }
    private var port: Int { AppSettings.shared.postgresPort }

    /// Wait for `proc` to exit, returning whether it terminated cleanly
    /// (status == 0). Returns `false` (rather than raising) if the
    /// process was never launched.
    ///
    /// Apple's documented behaviour for `terminationStatus` on a
    /// Process that hasn't run is to raise `NSInvalidArgumentException`.
    /// Swift can't catch that — it propagates as an unhandled
    /// exception and aborts the entire app, taking every spawned
    /// subprocess (including PostgreSQL mid-query) down with it.
    /// We've actually seen this in the wild during model-switch
    /// teardown when `try? proc.run()` swallowed a launch failure
    /// upstream. See `project_menubar_crashes_during_model_switch.md`.
    ///
    /// `processIdentifier` is 0 for a never-launched Process, so
    /// gating on `> 0` skips the throwing read.
    private static func awaitExitedCleanly(_ proc: Process) async -> Bool {
        await withCheckedContinuation { c in
            DispatchQueue.global().async {
                proc.waitUntilExit()
                let exited = proc.processIdentifier != 0 && proc.terminationStatus == 0
                c.resume(returning: exited)
            }
        }
    }

    func start() async throws {
        let fm = FileManager.default
        let pgVersionFile = dataDir.appendingPathComponent("PG_VERSION")

        // Determine bundled PG major version (e.g. "18" from "postgres (PostgreSQL) 18.3")
        let expectedMajor = await bundledPgMajorVersion()

        // Check if data directory needs (re-)initialization
        var needsInit = false
        if fm.fileExists(atPath: pgVersionFile.path) {
            let stored = (try? String(contentsOf: pgVersionFile, encoding: .utf8))?
                .trimmingCharacters(in: .whitespacesAndNewlines)
            if stored != expectedMajor {
                Log.logger("postgresql").warning(
                    "Data directory version mismatch: found \(stored ?? "?", privacy: .public), expected \(expectedMajor, privacy: .public). Reinitializing."
                )
                try? fm.removeItem(at: dataDir)
                needsInit = true
            }
        } else {
            needsInit = true
        }

        if needsInit {
            try await initializeDatabase()
            try await createDatabaseAndExtensions()
        }

        // Ensure logging_collector config exists (upgrade from pre-0.4.1)
        try ensureLoggingConfig()

        // Stale postmaster.pid removal: same logic as before, but now we
        // don't manually invoke pg_ctl stop — launchctl bootout (inside
        // agent.ensureInstalled) handles the lingering instance.
        let pidFile = dataDir.appendingPathComponent("postmaster.pid")
        if fm.fileExists(atPath: pidFile.path) {
            let action = Self.stalePidAction(pidFileContents: try? String(contentsOf: pidFile))
            switch action {
            case .remove(let pid):
                try? fm.removeItem(at: pidFile)
                Log.logger("postgresql").info("Removed stale postmaster.pid (pid \(pid, privacy: .public))")
            case .removeUnparseable:
                try? fm.removeItem(at: pidFile)
            case .keep:
                Log.logger("postgresql").warning("Existing PostgreSQL process found; bootout will handle it")
                // Don't kill manually — let agent.ensureInstalled's bootout
                // do it cleanly via launchctl.
            }
        }

        // Install/refresh the plist with current bundle paths.
        let plist = LaunchdPlist.postgres(
            bundle: Bundle.main.resourceURL!,
            dataDir: dataDir,
            logsDir: AppSettings.shared.logsDir,
            port: port,
        )
        try await agent.ensureInstalled(plistContent: plist)

        // launchctl kickstart -k starts (or restarts) the service.
        try await agent.start()
    }

    func stop() async {
        state = .stopping
        do {
            try await agent.stop()
        } catch {
            Log.logger("postgresql").error(
                "launchctl bootout failed: \(error.localizedDescription, privacy: .public)"
            )
        }
        state = .stopped
    }

    func healthCheck() async -> Bool {
        let pgIsReady = pgBinDir.appendingPathComponent("pg_isready")
        let proc = Process()
        proc.executableURL = pgIsReady
        proc.arguments = ["-p", String(port), "-h", "localhost", "-U", "lka"]
        proc.environment = pgEnvironment()
        proc.standardOutput = FileHandle.nullDevice
        proc.standardError = FileHandle.nullDevice
        try? proc.run()
        return await Self.awaitExitedCleanly(proc)
    }

    // MARK: - Logging Config

    /// Write conf.d/harbor_clerk.conf with logging_collector settings.
    /// Always overwrites to pick up config changes on upgrade.
    private func ensureLoggingConfig() throws {
        let confDir = dataDir.appendingPathComponent("conf.d")
        let confFile = confDir.appendingPathComponent("harbor_clerk.conf")

        let logsDir = AppSettings.shared.logsDir.path
        // Day-of-week filenames: postgres-Mon.log … postgres-Sun.log (7 files max)
        // Each truncated when that day comes around again.
        // 7 × 20 MB ≈ 140 MB worst case.
        let conf = """
        # Harbor Clerk: built-in log rotation
        logging_collector = on
        log_directory = '\(logsDir)'
        log_filename = 'postgres-%a.log'
        log_rotation_age = 1d
        log_rotation_size = 20MB
        log_truncate_on_rotation = on
        log_file_mode = 0600
        """
        try FileManager.default.createDirectory(at: confDir, withIntermediateDirectories: true)
        try conf.write(to: confFile, atomically: true, encoding: .utf8)

        // Ensure include_dir is in postgresql.conf
        let pgConf = dataDir.appendingPathComponent("postgresql.conf")
        let existing = try String(contentsOf: pgConf, encoding: .utf8)
        if !existing.contains("include_dir") {
            try (existing + "\ninclude_dir = 'conf.d'\n").write(
                to: pgConf, atomically: true, encoding: .utf8)
        }
    }

    // MARK: - Setup

    private func initializeDatabase() async throws {
        try FileManager.default.createDirectory(at: dataDir, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: AppSettings.shared.logsDir, withIntermediateDirectories: true)

        let initdb = pgBinDir.appendingPathComponent("initdb")
        let proc = Process()
        proc.executableURL = initdb
        proc.arguments = ["-D", dataDir.path, "-U", "lka", "--encoding=UTF8", "--locale=C"]
        proc.environment = pgEnvironment()
        proc.standardOutput = FileHandle.nullDevice
        proc.standardError = FileHandle.nullDevice

        try proc.run()
        let exitCode: Int32 = await withCheckedContinuation { c in
            DispatchQueue.global().async {
                proc.waitUntilExit()
                c.resume(returning: proc.terminationStatus)
            }
        }

        if exitCode != 0 {
            throw ServiceError.startFailed(name, "initdb failed with \(exitCode)")
        }

        // Configure logging_collector — ensureLoggingConfig() writes the actual
        // settings on every start, but we need include_dir in postgresql.conf now.
        let confDir = dataDir.appendingPathComponent("conf.d")
        try FileManager.default.createDirectory(at: confDir, withIntermediateDirectories: true)
        let pgConf = dataDir.appendingPathComponent("postgresql.conf")
        let existing = try String(contentsOf: pgConf, encoding: .utf8)
        try (existing + "\ninclude_dir = 'conf.d'\n").write(to: pgConf, atomically: true, encoding: .utf8)
    }

    private func createDatabaseAndExtensions() async throws {
        // Start temporarily for setup
        let pgCtl = pgBinDir.appendingPathComponent("pg_ctl")
        let startProc = Process()
        startProc.executableURL = pgCtl
        startProc.arguments = [
            "-D", dataDir.path,
            "-o", "-p \(port) -k /tmp",
            "start",
        ]
        startProc.environment = pgEnvironment()
        startProc.standardOutput = FileHandle.nullDevice
        startProc.standardError = FileHandle.nullDevice
        try startProc.run()
        await withCheckedContinuation { (c: CheckedContinuation<Void, Never>) in
            DispatchQueue.global().async { startProc.waitUntilExit(); c.resume() }
        }

        guard startProc.terminationStatus == 0 else {
            throw ServiceError.startFailed(name, "pg_ctl start (setup) exited with \(startProc.terminationStatus)")
        }

        // Wait for ready (pg_ctl -w already waits, but belt-and-suspenders)
        for _ in 0..<30 {
            if await healthCheck() { break }
            try await Task.sleep(for: .seconds(1))
        }

        // Create database
        let createdb = pgBinDir.appendingPathComponent("createdb")
        let createProc = Process()
        createProc.executableURL = createdb
        createProc.arguments = ["-p", String(port), "-h", "localhost", "-U", "lka", "lka"]
        createProc.environment = pgEnvironment()
        createProc.standardOutput = FileHandle.nullDevice
        createProc.standardError = FileHandle.nullDevice
        try? createProc.run()
        await withCheckedContinuation { (c: CheckedContinuation<Void, Never>) in
            DispatchQueue.global().async { createProc.waitUntilExit(); c.resume() }
        }

        // Create extensions
        let psql = pgBinDir.appendingPathComponent("psql")
        // pgcrypto not needed — gen_random_uuid() is built-in on PG 13+
        let extensions = ["vector", "pg_trgm", "citext"]
        for ext in extensions {
            let extProc = Process()
            extProc.executableURL = psql
            extProc.arguments = [
                "-p", String(port), "-h", "localhost", "-U", "lka", "-d", "lka",
                "-c", "CREATE EXTENSION IF NOT EXISTS \(ext);",
            ]
            extProc.environment = pgEnvironment()
            extProc.standardOutput = FileHandle.nullDevice
            extProc.standardError = FileHandle.nullDevice
            try? extProc.run()
            await withCheckedContinuation { (c: CheckedContinuation<Void, Never>) in
                DispatchQueue.global().async { extProc.waitUntilExit(); c.resume() }
            }
        }

        // Stop — will be started properly by ServiceManager (via launchd)
        let stopProc = Process()
        stopProc.executableURL = pgCtl
        stopProc.arguments = ["-D", dataDir.path, "stop", "-m", "fast"]
        stopProc.environment = pgEnvironment()
        stopProc.standardOutput = FileHandle.nullDevice
        stopProc.standardError = FileHandle.nullDevice
        try? stopProc.run()
        await withCheckedContinuation { (c: CheckedContinuation<Void, Never>) in
            DispatchQueue.global().async { stopProc.waitUntilExit(); c.resume() }
        }
    }

    // MARK: - Stale PID Detection

    enum StalePidAction: Equatable {
        case keep           // PID is alive, don't touch
        case remove(Int32)  // PID is dead, remove file
        case removeUnparseable  // Can't parse file, remove it
    }

    /// Determine what to do with a postmaster.pid file.
    /// Extracted as static for testability.
    nonisolated static func stalePidAction(pidFileContents: String?) -> StalePidAction {
        guard let contents = pidFileContents,
              let pidLine = contents.components(separatedBy: "\n").first,
              let pid = Int32(pidLine) else {
            return .removeUnparseable
        }
        // kill(pid, 0) returns 0 if process exists, -1 if not
        if kill(pid, 0) != 0 {
            return .remove(pid)
        }
        return .keep
    }

    private func pgEnvironment() -> [String: String] {
        [
            "PGDATA": dataDir.path,
            "PATH": pgBinDir.path + ":/usr/bin:/bin",
            "LD_LIBRARY_PATH": pgLibDir.path,
            "DYLD_LIBRARY_PATH": pgLibDir.path,
            "PGSHARE": pgShareDir.path,
        ]
    }

    /// Extract the major version from the bundled postgres binary (e.g. "18").
    ///
    /// Async because `start()` runs on the MainActor — calling
    /// `proc.waitUntilExit()` synchronously there blocks the actor and
    /// stalls every other `@MainActor` task (CLAUDE.md gotcha:
    /// "never call proc.waitUntilExit() on MainActor — always use the
    /// async continuation pattern").
    private func bundledPgMajorVersion() async -> String {
        await withCheckedContinuation { c in
            DispatchQueue.global().async {
                let proc = Process()
                proc.executableURL = self.pgBinDir.appendingPathComponent("postgres")
                proc.arguments = ["--version"]
                proc.environment = self.pgEnvironment()
                let pipe = Pipe()
                proc.standardOutput = pipe
                proc.standardError = FileHandle.nullDevice
                do {
                    try proc.run()
                    proc.waitUntilExit()
                } catch {
                    c.resume(returning: "")
                    return
                }
                let data = pipe.fileHandleForReading.readDataToEndOfFile()
                // Output: "postgres (PostgreSQL) 18.3\n"
                guard let output = String(data: data, encoding: .utf8),
                      let versionStr = output.split(separator: " ").last else {
                    c.resume(returning: "")
                    return
                }
                c.resume(returning: String(versionStr.split(separator: ".").first ?? ""))
            }
        }
    }
}

enum ServiceError: LocalizedError {
    case startFailed(String, String)

    var errorDescription: String? {
        switch self {
        case .startFailed(let service, let detail):
            return "\(service) failed to start: \(detail)"
        }
    }
}

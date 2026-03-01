import Foundation
import os

final class PostgresService: ManagedService {
    let name = "PostgreSQL"
    var state: ServiceState = .stopped
    private var process: Process?

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

    func start() async throws {
        let fm = FileManager.default

        // First run: initdb
        if !fm.fileExists(atPath: dataDir.appendingPathComponent("PG_VERSION").path) {
            try await initializeDatabase()
            try await createDatabaseAndExtensions()
        }

        // Ensure logging_collector config exists (upgrade from pre-0.4.1)
        try ensureLoggingConfig()

        // Remove stale PID file if no process is actually running
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
                // PostgreSQL is still running (possibly shutting down) — stop it first
                Log.logger("postgresql").warning("Existing PostgreSQL process found, stopping it before start")
                let stopProc = Process()
                stopProc.executableURL = pgBinDir.appendingPathComponent("pg_ctl")
                stopProc.arguments = ["-D", dataDir.path, "stop", "-m", "immediate"]
                stopProc.environment = pgEnvironment()
                stopProc.standardOutput = FileHandle.nullDevice
                stopProc.standardError = FileHandle.nullDevice
                try? stopProc.run()
                await withCheckedContinuation { (c: CheckedContinuation<Void, Never>) in
                    DispatchQueue.global().async {
                        stopProc.waitUntilExit()
                        c.resume()
                    }
                }
            }
        }

        // Start PostgreSQL via pg_ctl
        // Logs are handled by logging_collector (configured in conf.d/harbor_clerk.conf)
        let pgCtl = pgBinDir.appendingPathComponent("pg_ctl")
        let proc = Process()
        proc.executableURL = pgCtl
        proc.arguments = [
            "-D", dataDir.path,
            "-o", "-p \(port) -k /tmp",
            "start",
        ]
        proc.environment = pgEnvironment()

        let pipe = Log.createPipe(category: "postgresql")
        proc.standardOutput = pipe
        proc.standardError = pipe

        try proc.run()
        let exitCode: Int32 = await withCheckedContinuation { c in
            DispatchQueue.global().async {
                proc.waitUntilExit()
                c.resume(returning: proc.terminationStatus)
            }
        }

        if exitCode != 0 {
            throw ServiceError.startFailed(name, "pg_ctl start exited with \(exitCode)")
        }
    }

    func stop() async {
        state = .stopping

        // Phase 1: pg_ctl stop -m fast (SIGINT — orderly shutdown)
        let pgCtl = pgBinDir.appendingPathComponent("pg_ctl")
        let fastProc = Process()
        fastProc.executableURL = pgCtl
        fastProc.arguments = ["-D", dataDir.path, "stop", "-m", "fast", "-t", "15"]
        fastProc.environment = pgEnvironment()
        fastProc.standardOutput = FileHandle.nullDevice
        fastProc.standardError = FileHandle.nullDevice
        try? fastProc.run()
        let fastExited: Bool = await withCheckedContinuation { c in
            DispatchQueue.global().async {
                fastProc.waitUntilExit()
                c.resume(returning: fastProc.terminationStatus == 0)
            }
        }

        if !fastExited {
            // Phase 2: pg_ctl stop -m immediate (SIGQUIT — skip recovery)
            Log.logger("lifecycle").warning("PostgreSQL fast shutdown failed, trying immediate mode")
            let immProc = Process()
            immProc.executableURL = pgCtl
            immProc.arguments = ["-D", dataDir.path, "stop", "-m", "immediate"]
            immProc.environment = pgEnvironment()
            immProc.standardOutput = FileHandle.nullDevice
            immProc.standardError = FileHandle.nullDevice
            try? immProc.run()

            let immExited: Bool = await withCheckedContinuation { c in
                DispatchQueue.global().async {
                    immProc.waitUntilExit()
                    c.resume(returning: immProc.terminationStatus == 0)
                }
            }

            if !immExited {
                // Phase 3: read postmaster.pid and SIGKILL
                Log.logger("lifecycle").warning("PostgreSQL immediate shutdown failed, sending SIGKILL")
                let pidFile = dataDir.appendingPathComponent("postmaster.pid")
                if let contents = try? String(contentsOf: pidFile),
                   let pidLine = contents.components(separatedBy: "\n").first,
                   let pid = Int32(pidLine) {
                    kill(pid, SIGKILL)
                    usleep(1_000_000) // 1s for process to die
                }
            }
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
        return await withCheckedContinuation { c in
            DispatchQueue.global().async {
                proc.waitUntilExit()
                c.resume(returning: proc.terminationStatus == 0)
            }
        }
    }

    // MARK: - Logging Config

    /// Ensure conf.d/harbor_clerk.conf exists with logging_collector settings.
    /// Idempotent — safe to call on every start for upgrades from pre-0.4.1.
    private func ensureLoggingConfig() throws {
        let confDir = dataDir.appendingPathComponent("conf.d")
        let confFile = confDir.appendingPathComponent("harbor_clerk.conf")
        guard !FileManager.default.fileExists(atPath: confFile.path) else { return }

        let logsDir = AppSettings.shared.logsDir.path
        let conf = """
        # Harbor Clerk: built-in log rotation
        logging_collector = on
        log_directory = '\(logsDir)'
        log_filename = 'postgres.log'
        log_rotation_age = 1d
        log_rotation_size = 10MB
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

        Log.logger("postgresql").info("Configured logging_collector with daily rotation")
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

        let pipe = Log.createPipe(category: "postgresql")
        proc.standardOutput = pipe
        proc.standardError = pipe

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

        // Configure logging_collector for built-in log rotation
        let logsDir = AppSettings.shared.logsDir.path
        let conf = """
        # Harbor Clerk: built-in log rotation
        logging_collector = on
        log_directory = '\(logsDir)'
        log_filename = 'postgres.log'
        log_rotation_age = 1d
        log_rotation_size = 10MB
        log_truncate_on_rotation = on
        log_file_mode = 0600
        """
        let confURL = dataDir.appendingPathComponent("conf.d/harbor_clerk.conf")
        try FileManager.default.createDirectory(
            at: confURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        try conf.write(to: confURL, atomically: true, encoding: .utf8)

        // Include conf.d in postgresql.conf
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
        try startProc.run()
        await withCheckedContinuation { (c: CheckedContinuation<Void, Never>) in
            DispatchQueue.global().async { startProc.waitUntilExit(); c.resume() }
        }

        // Wait for ready
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

        // Stop — will be started properly by ServiceManager
        let stopProc = Process()
        stopProc.executableURL = pgCtl
        stopProc.arguments = ["-D", dataDir.path, "stop", "-m", "fast"]
        stopProc.environment = pgEnvironment()
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

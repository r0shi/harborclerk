import Foundation

final class PostgresService: ManagedService {
    let name = "PostgreSQL"
    var state: ServiceState = .stopped
    private var process: Process?
    private let logManager = LogManager.shared

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

        // Remove stale PID file if no process is actually running
        let pidFile = dataDir.appendingPathComponent("postmaster.pid")
        if fm.fileExists(atPath: pidFile.path) {
            if let contents = try? String(contentsOf: pidFile),
               let pidLine = contents.components(separatedBy: "\n").first,
               let pid = Int32(pidLine) {
                // Check if the PID is actually running
                if kill(pid, 0) != 0 {
                    try? fm.removeItem(at: pidFile)
                    logManager.append(service: name, text: "Removed stale postmaster.pid (pid \(pid))")
                }
            } else {
                try? fm.removeItem(at: pidFile)
            }
        }

        // Start PostgreSQL via pg_ctl
        let pgCtl = pgBinDir.appendingPathComponent("pg_ctl")
        let proc = Process()
        proc.executableURL = pgCtl
        proc.arguments = [
            "-D", dataDir.path,
            "-o", "-p \(port) -k /tmp",
            "-l", AppSettings.shared.logsDir.appendingPathComponent("postgres.log").path,
            "start",
        ]
        proc.environment = pgEnvironment()

        let pipe = logManager.createPipe(service: name)
        proc.standardOutput = pipe
        proc.standardError = pipe

        try proc.run()
        proc.waitUntilExit()

        if proc.terminationStatus != 0 {
            throw ServiceError.startFailed(name, "pg_ctl start exited with \(proc.terminationStatus)")
        }
    }

    func stop() {
        state = .stopping
        let pgCtl = pgBinDir.appendingPathComponent("pg_ctl")
        let proc = Process()
        proc.executableURL = pgCtl
        proc.arguments = ["-D", dataDir.path, "stop", "-m", "fast"]
        proc.environment = pgEnvironment()
        try? proc.run()
        proc.waitUntilExit()
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
        proc.waitUntilExit()
        return proc.terminationStatus == 0
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

        let pipe = logManager.createPipe(service: name)
        proc.standardOutput = pipe
        proc.standardError = pipe

        try proc.run()
        proc.waitUntilExit()

        if proc.terminationStatus != 0 {
            throw ServiceError.startFailed(name, "initdb failed with \(proc.terminationStatus)")
        }
    }

    private func createDatabaseAndExtensions() async throws {
        // Start temporarily for setup
        let pgCtl = pgBinDir.appendingPathComponent("pg_ctl")
        let startProc = Process()
        startProc.executableURL = pgCtl
        startProc.arguments = [
            "-D", dataDir.path,
            "-o", "-p \(port) -k /tmp",
            "-l", AppSettings.shared.logsDir.appendingPathComponent("postgres.log").path,
            "start",
        ]
        startProc.environment = pgEnvironment()
        try startProc.run()
        startProc.waitUntilExit()

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
        createProc.waitUntilExit()

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
            extProc.waitUntilExit()
        }

        // Stop — will be started properly by ServiceManager
        let stopProc = Process()
        stopProc.executableURL = pgCtl
        stopProc.arguments = ["-D", dataDir.path, "stop", "-m", "fast"]
        stopProc.environment = pgEnvironment()
        try? stopProc.run()
        stopProc.waitUntilExit()
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

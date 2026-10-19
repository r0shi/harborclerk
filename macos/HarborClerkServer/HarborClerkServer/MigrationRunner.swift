import Foundation
import os

/// Runs `alembic upgrade head` as a subprocess before the API starts.
/// Retries on failure to handle transient issues (lock contention, PG startup race).
struct MigrationRunner {
    private let maxAttempts = 3
    private let retryDelay: UInt64 = 3_000_000_000 // 3 seconds
    private let settings: AppSettings
    private let launch: (Process) async throws -> Int32

    /// Both default to the real thing. A test passes scratch settings and a
    /// `launch` that captures the process (and the environment `run()` gave
    /// it) and returns an exit code without spawning anything.
    init(
        settings: AppSettings = .shared,
        launch: @escaping (Process) async throws -> Int32 = { try await $0.runAndAwait() }
    ) {
        self.settings = settings
        self.launch = launch
    }

    func run() async throws {
        let bundle = Bundle.main.resourceURL!
        let python = bundle.appendingPathComponent("venv/bin/python")

        let env = Self.environment(settings: settings, bundle: bundle)

        var lastError: Error?

        for attempt in 1...maxAttempts {
            do {
                try await runAlembic(python: python, workDir: bundle, env: env)
                Log.logger("alembic").info("Migrations complete (attempt \(attempt, privacy: .public))")
                return
            } catch {
                lastError = error
                Log.logger("alembic").warning(
                    "Migration attempt \(attempt, privacy: .public)/\(self.maxAttempts, privacy: .public) failed: \(error.localizedDescription, privacy: .public)"
                )
                if attempt < maxAttempts {
                    try? await Task.sleep(nanoseconds: retryDelay)
                }
            }
        }

        throw lastError ?? ServiceError.startFailed("Alembic", "all \(maxAttempts) attempts failed")
    }

    /// Alembic's environment. Its own closed dict, not the services' one, but
    /// the same cache rule: nothing it imports may write into the bundle (#846).
    static func environment(settings: AppSettings, bundle: URL) -> [String: String] {
        let env = [
            "DATABASE_URL": "postgresql+asyncpg://lka@localhost:\(settings.postgresPort)/lka",
            "PATH": bundle.appendingPathComponent("venv/bin").path + ":/usr/bin:/bin",
            "PYTHONPATH": bundle.appendingPathComponent("venv/lib").path,
        ]
        return ServiceManager.applyingRuntimeCacheRules(to: env)
    }

    private func runAlembic(python: URL, workDir: URL, env: [String: String]) async throws {
        let proc = Process()
        proc.executableURL = python
        proc.arguments = ["-m", "alembic", "upgrade", "head"]
        proc.currentDirectoryURL = workDir.appendingPathComponent("alembic").deletingLastPathComponent()
        proc.environment = env

        let pipe = Log.createPipe(category: "alembic")
        proc.standardOutput = pipe
        proc.standardError = pipe

        // `Process.runAndAwait` uses terminationHandler — safer than
        // `waitUntilExit` (see ProcessAsync.swift). Alembic typically
        // runs for seconds, so the short-lived-child race that hung
        // launchctl in the wild doesn't apply here, but use the same
        // helper everywhere so we have one wait pattern to reason about.
        let exitCode = try await launch(proc)

        if exitCode != 0 {
            throw ServiceError.startFailed("Alembic", "exit code \(exitCode)")
        }
    }
}

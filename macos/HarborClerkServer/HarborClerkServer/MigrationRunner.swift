import Foundation
import os

/// Runs `alembic upgrade head` as a subprocess before the API starts.
/// Retries on failure to handle transient issues (lock contention, PG startup race).
struct MigrationRunner {
    private let maxAttempts = 3
    private let retryDelay: UInt64 = 3_000_000_000 // 3 seconds

    func run() async throws {
        let bundle = Bundle.main.resourceURL!
        let python = bundle.appendingPathComponent("venv/bin/python")

        let settings = AppSettings.shared
        let env = [
            "DATABASE_URL": "postgresql+asyncpg://lka@localhost:\(settings.postgresPort)/lka",
            "PATH": bundle.appendingPathComponent("venv/bin").path + ":/usr/bin:/bin",
            "PYTHONPATH": bundle.appendingPathComponent("venv/lib").path,
        ]

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
        let exitCode = try await proc.runAndAwait()

        if exitCode != 0 {
            throw ServiceError.startFailed("Alembic", "exit code \(exitCode)")
        }
    }
}

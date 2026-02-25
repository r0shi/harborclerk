import Foundation
import os

/// Runs `alembic upgrade head` as a subprocess before the API starts.
struct MigrationRunner {
    func run() async throws {
        let bundle = Bundle.main.resourceURL!
        let python = bundle.appendingPathComponent("venv/bin/python")

        let proc = Process()
        proc.executableURL = python
        proc.arguments = ["-m", "alembic", "upgrade", "head"]
        proc.currentDirectoryURL = bundle.appendingPathComponent("alembic").deletingLastPathComponent()

        // Alembic needs DATABASE_URL and the project on PYTHONPATH
        let settings = AppSettings.shared
        proc.environment = [
            "DATABASE_URL": "postgresql+asyncpg://lka@localhost:\(settings.postgresPort)/lka",
            "PATH": bundle.appendingPathComponent("venv/bin").path + ":/usr/bin:/bin",
            "PYTHONPATH": bundle.appendingPathComponent("venv/lib").path,
        ]

        let pipe = Log.createPipe(category: "alembic")
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
            throw ServiceError.startFailed("Alembic", "exit code \(exitCode)")
        }
    }
}

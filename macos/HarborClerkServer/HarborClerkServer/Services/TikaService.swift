import Foundation
import os

final class TikaService: ManagedService {
    let name = "Tika"
    var state: ServiceState = .stopped
    private var process: Process?
    /// Called after process exits unexpectedly and state is set to .errored.
    var onUnexpectedExit: (@MainActor () -> Void)?

    private var javaBin: URL {
        Bundle.main.resourceURL!.appendingPathComponent("java/Contents/Home/bin/java")
    }
    private var tikaJar: URL {
        Bundle.main.resourceURL!.appendingPathComponent("tika/tika-server.jar")
    }
    private var port: Int { AppSettings.shared.tikaPort }

    func start() async throws {
        let proc = Process()
        proc.executableURL = javaBin
        proc.arguments = [
            "-jar", tikaJar.path,
            "--host", "127.0.0.1",
            "--port", String(port),
        ]

        let pipe = Log.createPipe(category: "tika")
        proc.standardOutput = pipe
        proc.standardError = pipe

        let tikaLogger = Log.logger("tika")
        proc.terminationHandler = { [weak self] p in
            Task { @MainActor in
                guard let self, self.state == .running else { return }
                self.state = .errored
                tikaLogger.error("Process exited unexpectedly (\(p.terminationStatus, privacy: .public))")
                self.onUnexpectedExit?()
            }
        }

        try proc.run()
        process = proc
    }

    func stop() async {
        state = .stopping
        guard let proc = process, proc.isRunning else {
            state = .stopped
            return
        }
        proc.terminate() // SIGTERM
        // JVM can be slow to unwind — 10s grace, then SIGKILL
        DispatchQueue.global().asyncAfter(deadline: .now() + 10) {
            guard proc.isRunning else { return }
            Log.logger("lifecycle").warning("Tika still running after 10s, sending SIGKILL")
            kill(proc.processIdentifier, SIGKILL)
        }
        await withCheckedContinuation { (c: CheckedContinuation<Void, Never>) in
            DispatchQueue.global().async {
                proc.waitUntilExit()
                c.resume()
            }
        }
        process = nil
        state = .stopped
    }

    func healthCheck() async -> Bool {
        // Tika returns 200 on GET /tika when ready
        guard let url = URL(string: "http://127.0.0.1:\(port)/tika") else { return false }
        do {
            let (_, response) = try await URLSession.shared.data(from: url)
            return (response as? HTTPURLResponse)?.statusCode == 200
        } catch {
            return false
        }
    }
}

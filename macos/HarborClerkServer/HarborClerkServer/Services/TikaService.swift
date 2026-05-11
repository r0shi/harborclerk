import Foundation
import os

final class TikaService: ManagedService {
    let name = "Tika"
    var state: ServiceState = .stopped
    private var process: Process?
    /// Called after process exits unexpectedly and state is set to .errored.
    var onUnexpectedExit: (@MainActor () -> Void)?

    /// Expose the child PID for orphan tracking.
    var processIdentifier: Int32? { process?.isRunning == true ? process?.processIdentifier : nil }

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

        // Tika 3.x forks a child JVM that resolves `java` from PATH; point it at the bundled JRE.
        let javaHome = Bundle.main.resourceURL!.appendingPathComponent("java/Contents/Home").path
        proc.environment = [
            "JAVA_HOME": javaHome,
            "PATH": "\(javaHome)/bin:/usr/bin:/bin",
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

        try proc.runAsProcessGroupLeader()
        process = proc
    }

    func stop() async {
        state = .stopping
        guard let proc = process, proc.isRunning else {
            state = .stopped
            return
        }
        proc.terminate() // SIGTERM
        // JVM can be slow to unwind — 10s grace, then SIGKILL. Uses the
        // shared helper so the Pipe+waitUntilExit deadlock pattern
        // (audit memo: project_menubar_process_management_audit.md)
        // can't make this stall the rest of stopAll().
        //
        // NOTE: Tika's JVM can fork child JVMs for heavy extractions.
        // SIGKILL on the tracked PID doesn't catch those grandchildren —
        // Tier B follow-up is to use posix_spawn + setpgid so killpg can
        // hit the whole tree.
        await proc.waitForExitWithDeadline(graceSeconds: 10, serviceName: name)
        process = nil
        state = .stopped
    }

    func healthCheck() async -> Bool {
        // Tika returns 200 on GET /tika when ready
        guard let url = URL(string: "http://127.0.0.1:\(port)/tika") else { return false }
        return await httpProbeOK(url)
    }
}

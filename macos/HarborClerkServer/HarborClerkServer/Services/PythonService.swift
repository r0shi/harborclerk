import Foundation
import os

/// Base class for Python subprocess services (API, Worker, Embedder).
class PythonService: ManagedService {
    let name: String
    var state: ServiceState = .stopped
    var process: Process?
    var baseEnvironment: [String: String] = [:]

    /// Seconds to wait after SIGTERM before sending SIGKILL.
    /// API server needs more time when mid-research (SSE streams, agent threads, DB sessions).
    var shutdownGracePeriod: TimeInterval = 30.0
    /// Called after process exits unexpectedly and internal restarts are exhausted.
    var onUnexpectedExit: (@MainActor () -> Void)?

    private var restartCount = 0
    private let maxRestarts = 3

    init(name: String) {
        self.name = name
    }

    /// Subclasses override to provide the executable and arguments.
    var executableName: String { fatalError("Subclasses must override") }
    var arguments: [String] { [] }
    var extraEnvironment: [String: String] { [:] }

    func start() async throws {
        let bundle = Bundle.main.resourceURL!
        let venvBin = bundle.appendingPathComponent("venv/bin")
        let executable = venvBin.appendingPathComponent(executableName)

        let proc = Process()
        proc.executableURL = executable
        proc.arguments = arguments

        // Merge base Python env with service-specific overrides
        var env = baseEnvironment
        for (k, v) in extraEnvironment {
            env[k] = v
        }
        proc.environment = env

        let category = name.lowercased()
        let pipe = Log.createPipe(category: category)
        proc.standardOutput = pipe
        proc.standardError = pipe

        let serviceLogger = Log.logger(category)
        proc.terminationHandler = { [weak self] p in
            guard let self = self else { return }
            Task { @MainActor in
                if self.state == .running && self.restartCount < self.maxRestarts {
                    self.restartCount += 1
                    let delay = pow(2.0, Double(self.restartCount))
                    serviceLogger.error("Process exited (\(p.terminationStatus, privacy: .public)), restarting in \(Int(delay), privacy: .public)s (attempt \(self.restartCount, privacy: .public)/\(self.maxRestarts, privacy: .public))")
                    try? await Task.sleep(for: .seconds(delay))
                    try? await self.start()
                } else if self.state == .running {
                    self.state = .errored
                    serviceLogger.error("Process exited, max restarts reached")
                    self.onUnexpectedExit?()
                }
            }
        }

        try proc.runAsProcessGroupLeader()
        process = proc
    }

    /// Reset the restart counter. Called by ServiceManager after health check passes.
    func resetRestartCount() {
        restartCount = 0
    }

    func stop() async {
        state = .stopping
        guard let proc = process, proc.isRunning else {
            state = .stopped
            return
        }
        proc.terminate() // SIGTERM
        // Use the shared deadline helper — detaches pipes (defusing the
        // Pipe+waitUntilExit deadlock described in
        // project_menubar_process_management_audit.md), schedules SIGKILL
        // after the grace period, then waits.
        await proc.waitForExitWithDeadline(
            graceSeconds: shutdownGracePeriod,
            serviceName: name,
        )
        process = nil
        state = .stopped
    }

    func healthCheck() async -> Bool {
        // Default: no health check, subclasses override
        return process?.isRunning == true
    }
}

import Foundation
import os

/// Base class for Python subprocess services (API, Worker, Embedder).
class PythonService: ManagedService {
    let name: String
    var state: ServiceState = .stopped
    var process: Process?
    var baseEnvironment: [String: String] = [:]

    /// Seconds to wait after SIGTERM before sending SIGKILL.
    var shutdownGracePeriod: TimeInterval = 5.0
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

        try proc.run()
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
        let grace = shutdownGracePeriod
        let svcName = name
        DispatchQueue.global().asyncAfter(deadline: .now() + grace) {
            guard proc.isRunning else { return }
            Log.logger("lifecycle").warning(
                "\(svcName, privacy: .public) still running after \(Int(grace), privacy: .public)s, sending SIGKILL")
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
        // Default: no health check, subclasses override
        return process?.isRunning == true
    }
}

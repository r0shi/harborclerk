import Foundation

/// Base class for Python subprocess services (API, Worker, Embedder).
class PythonService: ManagedService {
    let name: String
    var state: ServiceState = .stopped
    var process: Process?
    var baseEnvironment: [String: String] = [:]

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

        let pipe = LogManager.shared.createPipe(service: name)
        proc.standardOutput = pipe
        proc.standardError = pipe

        let serviceName = self.name
        proc.terminationHandler = { [weak self] p in
            guard let self = self else { return }
            Task { @MainActor in
                if self.state == .running && self.restartCount < self.maxRestarts {
                    self.restartCount += 1
                    let delay = pow(2.0, Double(self.restartCount))
                    LogManager.shared.append(
                        service: serviceName,
                        text: "Process exited (\(p.terminationStatus)), restarting in \(Int(delay))s (attempt \(self.restartCount)/\(self.maxRestarts))"
                    )
                    try? await Task.sleep(for: .seconds(delay))
                    try? await self.start()
                } else if self.state == .running {
                    self.state = .errored
                    LogManager.shared.append(service: serviceName, text: "Process exited, max restarts reached")
                }
            }
        }

        try proc.run()
        process = proc
        restartCount = 0
    }

    func stop() {
        state = .stopping
        guard let proc = process, proc.isRunning else {
            state = .stopped
            return
        }
        proc.terminate()
        DispatchQueue.global().asyncAfter(deadline: .now() + 5) { [weak self] in
            if self?.process?.isRunning == true {
                self?.process?.interrupt()
            }
        }
        proc.waitUntilExit()
        process = nil
        state = .stopped
    }

    func healthCheck() async -> Bool {
        // Default: no health check, subclasses override
        return process?.isRunning == true
    }
}

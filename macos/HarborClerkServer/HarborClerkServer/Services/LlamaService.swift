import Foundation
import os

final class LlamaService: ManagedService {
    let name = "LLM"
    var state: ServiceState = .stopped
    private var process: Process?
    /// Called after process exits unexpectedly and state is set to .errored.
    var onUnexpectedExit: (@MainActor () -> Void)?

    /// Expose the child PID for orphan tracking.
    var processIdentifier: Int32? { process?.isRunning == true ? process?.processIdentifier : nil }

    private var llamaBin: URL {
        Bundle.main.resourceURL!.appendingPathComponent("llama/llama-server")
    }
    private var port: Int { AppSettings.shared.llamaPort }

    func start() async throws {
        let settings = AppSettings.shared
        let modelPath = settings.activeModelPath
        guard !modelPath.isEmpty else {
            // No model selected — revert to stopped (ServiceManager set .starting)
            state = .stopped
            return
        }

        guard FileManager.default.fileExists(atPath: modelPath) else {
            Log.logger("llm").error("Model file not found: \(modelPath, privacy: .public)")
            state = .errored
            return
        }

        let yarnEnabled = settings.llmYarnEnabled
        let yarnConfig = settings.activeModelYarn
        let useYarn = yarnEnabled && yarnConfig != nil
        let contextWindow = useYarn ? yarnConfig!.extendedContext : settings.activeModelContextWindow

        let proc = Process()
        proc.executableURL = llamaBin
        var args = [
            "-m", modelPath,
            "--host", "127.0.0.1",
            "--port", String(port),
            "-ngl", "99",
            "-c", String(contextWindow),
            "--threads", String(max(1, ProcessInfo.processInfo.processorCount / 2)),
        ]
        if useYarn, let yarn = yarnConfig {
            args += [
                "--rope-scaling", "yarn",
                "--rope-scale", String(yarn.ropeScale),
                "--yarn-orig-ctx", String(yarn.originalContext),
            ]
            if let attn = yarn.attnFactor {
                args += ["--yarn-attn-factor", String(attn)]
            }
        }
        proc.arguments = args

        let pipe = Log.createPipe(category: "llm")
        proc.standardOutput = pipe
        proc.standardError = pipe

        let llmLogger = Log.logger("llm")
        proc.terminationHandler = { [weak self] p in
            Task { @MainActor in
                guard let self, self.state == .running else { return }
                self.state = .errored
                llmLogger.error("Process exited unexpectedly (\(p.terminationStatus, privacy: .public))")
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
        // Model unload can be slow — 10s grace, then SIGKILL
        DispatchQueue.global().asyncAfter(deadline: .now() + 10) {
            guard proc.isRunning else { return }
            Log.logger("lifecycle").warning("LLM still running after 10s, sending SIGKILL")
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
        guard !AppSettings.shared.activeModelPath.isEmpty else { return false }
        guard let url = URL(string: "http://127.0.0.1:\(port)/health") else { return false }
        do {
            let (_, response) = try await URLSession.shared.data(from: url)
            return (response as? HTTPURLResponse)?.statusCode == 200
        } catch {
            return false
        }
    }
}

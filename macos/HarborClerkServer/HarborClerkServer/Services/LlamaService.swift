import Foundation
import os

final class LlamaService: ManagedService {
    let name = "LLM"
    var state: ServiceState = .stopped
    private var process: Process?

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

        let proc = Process()
        proc.executableURL = llamaBin
        proc.arguments = [
            "-m", modelPath,
            "--host", "127.0.0.1",
            "--port", String(port),
            "-ngl", "99",
            "-c", "8192",
            "--threads", String(max(1, ProcessInfo.processInfo.processorCount / 2)),
        ]

        let pipe = Log.createPipe(category: "llm")
        proc.standardOutput = pipe
        proc.standardError = pipe

        let llmLogger = Log.logger("llm")
        proc.terminationHandler = { [weak self] p in
            Task { @MainActor in
                if self?.state == .running {
                    self?.state = .errored
                    llmLogger.error("Process exited unexpectedly (\(p.terminationStatus, privacy: .public))")
                }
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
        guard let url = URL(string: "http://localhost:\(port)/health") else { return false }
        do {
            let (_, response) = try await URLSession.shared.data(from: url)
            return (response as? HTTPURLResponse)?.statusCode == 200
        } catch {
            return false
        }
    }
}

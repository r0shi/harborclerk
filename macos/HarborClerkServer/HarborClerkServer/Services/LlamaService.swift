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
            // Single parallel slot. Heavy models can't afford more KV
            // cache; small models could but we don't tune per-model yet.
            // See memory note: project_per_model_np_tuning.md
            "-np", "1",
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

        try proc.runAsProcessGroupLeader()
        process = proc
    }

    func stop() async {
        state = .stopping

        if let proc = process, proc.isRunning {
            proc.terminate() // SIGTERM
            // Model unload can be slow — 10s grace, then SIGKILL. Uses the
            // shared helper so the Pipe+waitUntilExit deadlock pattern
            // (audit memo: project_menubar_process_management_audit.md)
            // can't make this stall the rest of stopAll().
            await proc.waitForExitWithDeadline(graceSeconds: 10, serviceName: name)
        }
        process = nil

        // Belt-and-suspenders: confirm the LLM port is actually released
        // before declaring stop complete. waitUntilExit() returns when the
        // tracked Process tears down, but in practice subsequent start()s
        // have failed with "couldn't bind HTTP server socket" — usually an
        // orphan llama-server from an earlier crash that the Process
        // object never knew about (e.g. across an app relaunch). Probe
        // the port and force-kill any straggler before returning, so the
        // next start() always begins with a free socket.
        await ensurePortReleased(timeout: 5.0)

        state = .stopped
    }

    /// Wait up to `timeout` for the LLM port to be released, then
    /// SIGKILL anything still holding it. Cheap when the port is
    /// already free.
    private func ensurePortReleased(timeout: TimeInterval) async {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if await pidsHoldingPort().isEmpty { return }
            try? await Task.sleep(nanoseconds: 200_000_000) // 200 ms
        }
        let stragglers = await pidsHoldingPort()
        guard !stragglers.isEmpty else { return }
        Log.logger("lifecycle").warning(
            "LLM port \(self.port, privacy: .public) still held after stop; force-killing pids \(stragglers, privacy: .public)"
        )
        for pid in stragglers {
            kill(pid, SIGKILL)
        }
        // Brief pause for the kernel to release the socket.
        try? await Task.sleep(nanoseconds: 500_000_000) // 500 ms
    }

    /// Return PIDs (if any) currently bound to the LLM port. Uses lsof
    /// off the main thread.
    private func pidsHoldingPort() async -> [Int32] {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/sbin/lsof")
        proc.arguments = ["-ti", "tcp:\(self.port)"]
        let pipe = Pipe()
        proc.standardOutput = pipe
        proc.standardError = FileHandle.nullDevice
        do {
            try proc.run()
        } catch {
            return []
        }
        return await withCheckedContinuation { (c: CheckedContinuation<[Int32], Never>) in
            DispatchQueue.global().async {
                proc.waitUntilExit()
                let data = pipe.fileHandleForReading.readDataToEndOfFile()
                let pids = (String(data: data, encoding: .utf8) ?? "")
                    .split(whereSeparator: \.isNewline)
                    .compactMap { Int32($0.trimmingCharacters(in: .whitespaces)) }
                c.resume(returning: pids)
            }
        }
    }

    func healthCheck() async -> Bool {
        guard !AppSettings.shared.activeModelPath.isEmpty else { return false }
        guard let url = URL(string: "http://127.0.0.1:\(port)/health") else { return false }
        return await httpProbeOK(url)
    }
}

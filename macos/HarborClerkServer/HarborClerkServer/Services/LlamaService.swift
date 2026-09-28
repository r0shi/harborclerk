import Foundation
import os

final class LlamaService: ManagedService {
    let name = "LLM"
    var state: ServiceState = .stopped
    /// Internal so `AppDelegate.forceStopAllServices` can nil this after a
    /// SIGKILL pass — matches the access level of `PythonService.process`.
    /// Without the nil, `processIdentifier` could surface a stale dead-PID
    /// until Foundation's kqueue catches up.
    var process: Process?
    /// Called after process exits unexpectedly and state is set to .errored.
    var onUnexpectedExit: (@MainActor () -> Void)?

    /// Expose the child PID for orphan tracking.
    var processIdentifier: Int32? { process?.isRunning == true ? process?.processIdentifier : nil }
    var holdsLiveProcess: Bool { processIdentifier != nil }

    /// The stop in flight, so a second caller joins it instead of racing it.
    /// Two relaunches overlap when config.json changes twice within a few
    /// seconds (activate, then deactivate; or the model id and the restart
    /// signal landing on different poll ticks). Two stops of one process both
    /// waited for it, both nilled `process` (losing a successor that had been
    /// assigned in between) and both probed the port, where the slower one
    /// found the successor's socket and killed it as a straggler.
    private var stopInFlight: Task<Void, Never>?
    private let stopLock = NSLock()
    /// How many stops actually ran (as opposed to joined one in flight).
    /// Read by the coalescing test; nothing in the app uses it.
    private(set) var stopsPerformed = 0
    /// Seconds after SIGTERM before the stop escalates to SIGKILL. Model
    /// unload can be slow, hence 10; a test that needs the stop to stay in
    /// flight for a measurable moment shortens it.
    var stopGraceSeconds: TimeInterval = 10

    private var llamaBin: URL {
        Bundle.main.resourceURL!.appendingPathComponent("llama/llama-server")
    }
    /// The port whose holders the stop path kills as stragglers. A test points
    /// it at a port nothing on the machine holds, because the default is the
    /// live app's llama-server.
    var portOverride: Int?
    private var port: Int { portOverride ?? AppSettings.shared.llamaPort }

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

        // Defensive port probe. `ServiceManager.startAll()` already calls
        // this for the llama port, but `start()` is also invoked from
        // auto-restart, model switch, and manual-restart paths that
        // bypass startAll. If the previous llama-server (or anything
        // else) is still holding port 8102, the new process exits
        // immediately with "couldn't bind HTTP server socket" and the
        // service goes to errored — fixable only by manual intervention
        // until now.
        await ServiceManager.killStaleProcess(onPort: settings.llamaPort)

        // A stop that landed during that await has already moved the state on;
        // spawning now would leave a server nothing tracks and nothing stops,
        // because every stop path decides by `process` and this one would be
        // assigned after the stop had cleared it (#689).
        //
        // Best-effort across threads: this class is not MainActor-isolated and
        // the project is in Swift 5 mode, so `start()` and `stop()` can run on
        // different pool threads at once and this read is unsynchronised with
        // `stop()`'s write. The identity check in `performStop` (only the
        // process it waited on is cleared) is what protects a spawn that slips
        // through; this guard just avoids the spawn in the common case.
        guard state == .starting else {
            Log.logger("llm").notice("Not launching: stopped while preparing to start (state \(self.state.rawValue, privacy: .public))")
            return
        }

        let yarnEnabled = settings.llmYarnEnabled
        let yarnConfig = settings.activeModelYarn
        let useYarn = yarnEnabled && yarnConfig != nil
        let requestedContext = useYarn ? yarnConfig!.extendedContext : settings.activeModelContextWindow

        // What fits this Mac decides the context, not the registry. The
        // weights are measured from the file; the KV cost per token comes
        // from the registry mirror. A model whose weights alone do not fit
        // is not launched: llama-server would allocate it anyway and the
        // host, not the server, would fail.
        let modelBytes = (try? FileManager.default.attributesOfItem(atPath: modelPath)[.size] as? Int) ?? 0
        let ramBytes = Int(ProcessInfo.processInfo.physicalMemory)
        let contextWindow = MemoryBudget.maxContext(
            modelBytes: modelBytes,
            kvBytesPerToken: settings.activeModelKvBytesPerToken,
            fixedBytes: settings.activeModelFixedBytes,
            requested: requestedContext,
            ramBytes: ramBytes
        )
        if contextWindow == 0 {
            Log.logger("llm").error(
                "Not launching: \(modelPath, privacy: .public) needs more memory than this Mac has (weights \(modelBytes / 1_000_000_000) GB, \(ramBytes / 1_000_000_000) GB installed)"
            )
            state = .errored
            return
        }
        if contextWindow < requestedContext {
            Log.logger("llm").warning(
                "Context clamped from \(requestedContext) to \(contextWindow) tokens to fit \(ramBytes / 1_000_000_000) GB of memory"
            )
        }

        // The prompt cache gets what the context left, and no more, up to llama-server's
        // own 8 GiB default, which nothing had budgeted (#657).
        let promptCacheMiB = MemoryBudget.promptCacheMiB(
            modelBytes: modelBytes,
            kvBytesPerToken: settings.activeModelKvBytesPerToken,
            fixedBytes: settings.activeModelFixedBytes,
            context: contextWindow,
            ramBytes: ramBytes
        )
        if promptCacheMiB == 0 {
            // The designed steady state on a Mac whose context is clamped, so not a warning.
            Log.logger("llm").info(
                "Prompt cache off: after the model and its context this Mac has less left than a \(MemoryBudget.promptCacheMinTokens)-token state of this model, so returning to an earlier conversation re-reads it"
            )
        } else {
            Log.logger("llm").info("Prompt cache bounded at \(promptCacheMiB) MiB")
        }

        let proc = Process()
        proc.executableURL = llamaBin
        var args = [
            "-m", modelPath,
            "--host", "127.0.0.1",
            "--port", String(port),
            "-ngl", "99",
            // Per-model parallel slot count. -c is the total: a slot does
            // not add KV memory, it divides the context, so a request sees
            // -c / -np. Every curated model runs one slot: the whole window
            // for each job is worth more here than parallel requests.
            // Source of truth: ModelInfo.parallel_slots in
            // src/harbor_clerk/llm/models.py (mirrored above in
            // Settings.activeModelParallelSlots).
            "-np", String(settings.activeModelParallelSlots),
            "-c", String(contextWindow),
            "--cache-ram", String(promptCacheMiB),
            // Bounded, and in the budget (activeModelFixedBytes): the default is 32 per slot in host RAM.
            "--ctx-checkpoints", String(MemoryBudget.ctxCheckpoints),
            "--threads", String(max(1, ProcessInfo.processInfo.processorCount / 2)),
        ]
        if promptCacheMiB == 0 {
            // With no cache there is nothing to park an idle slot in. Said here, so llama-server does not
            // say "--cache-idle-slots requires --cache-ram, disabling" on every start.
            args.append("--no-cache-idle-slots")
        }
        // No RoPE stretch when the clamp left no context to stretch into.
        args += MemoryBudget.yarnArguments(contextWindow: contextWindow, slots: settings.activeModelParallelSlots, yarn: useYarn ? yarnConfig : nil)
        proc.arguments = args

        let pipe = Log.createPipe(
            category: "llm",
            fileURL: settings.logsDir.appendingPathComponent("llm.log")
        )
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

    /// Stop whatever llama-server this service holds, whatever state it
    /// recorded. The state is advisory: it has said `errored` over a server
    /// that was still loading and then served for hours, and `stopped` over
    /// one spawned after a stop had run. The process is the fact (#689).
    ///
    /// Concurrent callers join the stop in flight rather than running a
    /// second one; see `stopInFlight`.
    func stop() async {
        let task: Task<Void, Never> = stopLock.withLock {
            if let inFlight = stopInFlight { return inFlight }
            // Set before the Task is scheduled, so a `start()` resuming from
            // an await sees it at once rather than after the executor gets
            // round to running the stop. Only the caller that owns the stop
            // sets it: a joiner arrives while the owner's stop is in flight,
            // so `stopping` is already set.
            state = .stopping
            let task = Task { await self.performStop() }
            stopInFlight = task
            return task
        }
        await task.value
    }

    private func performStop() async {
        stopsPerformed += 1
        if let proc = process, proc.isRunning {
            // The whole group, per macos/AGENTS.md; llama-server spawns no
            // children today, so this is the rule rather than a repair.
            // `terminate()` is the fallback for a child that was not made a
            // group leader (setpgid failed and was logged at launch).
            if killpg(proc.processIdentifier, SIGTERM) != 0 {
                proc.terminate()
            }
            // Grace, then SIGKILL. Uses the shared helper so the
            // Pipe+waitUntilExit deadlock pattern (audit memo:
            // project_menubar_process_management_audit.md) can't make this
            // stall the rest of stopAll().
            await proc.waitForExitWithDeadline(graceSeconds: stopGraceSeconds, serviceName: name)
            // Only the process this stop waited on. A start that ran while the
            // wait was in flight (Start or Restart from the status window
            // passes `startService` while the state is `stopping`) has
            // assigned its own child here; clearing that one would leave a
            // live server that `holdsLiveProcess` denies and no relaunch path
            // can reach — the one orphan path the state guards do not close.
            if process === proc { process = nil }
        } else if process?.isRunning != true {
            process = nil
        }

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
        // Cleared here, under the lock, before the Task's value resumes anyone:
        // cleared by the owner after resuming, a caller could join a finished
        // Task and return with nothing stopped and `stopping` never set.
        stopLock.withLock { stopInFlight = nil }
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
            // Plain kill, as `killStaleProcess(onPort:)` does: these pids come
            // from lsof, not from a spawn of ours, and `killpg` would take a
            // foreign group leader's whole group with it. The group signal is
            // reserved for the child this service launched.
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

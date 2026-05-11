import Foundation
import os

// MARK: - Service protocol & state

enum ServiceState: String, CaseIterable {
    case stopped, startupPending, starting, running, shutdownPending, stopping, errored
}

extension Notification.Name {
    static let servicesStateChanged = Notification.Name("servicesStateChanged")
}

protocol ManagedService: AnyObject {
    var name: String { get }
    var state: ServiceState { get set }
    func start() async throws
    func stop() async
    func healthCheck() async -> Bool
}

// MARK: - Health-probe helper

/// One-shot HTTP probe for service healthChecks.
///
/// Each call uses its own ephemeral `URLSession` with a short request
/// timeout, then invalidates it. This deliberately avoids
/// `URLSession.shared` because the shared session, with no per-request
/// timeout, has accumulated many concurrent in-flight tasks against
/// unresponsive backends (notably llama-server during the 30-60 s
/// weight-load window for big models). Each in-flight task installs a
/// dispatch-source timeout timer; under that load, CFNetwork's
/// timer-cancellation path has crashed the entire app with a
/// pointer-authentication failure inside
/// `_dispatch_source_set_runloop_timer_4CF`. See
/// `project_menubar_crashes_during_model_switch.md`.
///
/// Per-service ephemeral sessions sidestep this in three ways:
///   1. Short timeouts cap each task's lifetime so stale tasks don't
///      pile up while the user is reactivating after a deactivation.
///   2. Each session has its own task table, partitioning the herd
///      of completion handlers that lands when llama-server finally
///      responds, instead of racing in a single shared scheduler.
///   3. `invalidateAndCancel()` on exit cleans up promptly rather
///      than relying on the framework's deferred cleanup.
///
/// Returns `true` iff the URL responded with HTTP 200 within `timeout`
/// seconds. Any error (timeout, connection refused, non-200) returns
/// `false`. Callers wanting softer semantics (e.g. "process alive
/// even if HTTP slow") can layer that on top.
func httpProbeOK(_ url: URL, timeout: TimeInterval = 3) async -> Bool {
    let config = URLSessionConfiguration.ephemeral
    config.timeoutIntervalForRequest = timeout
    config.timeoutIntervalForResource = timeout
    config.urlCache = nil
    config.httpCookieStorage = nil
    config.urlCredentialStorage = nil
    let session = URLSession(configuration: config)
    defer { session.invalidateAndCancel() }
    do {
        let (_, response) = try await session.data(from: url)
        return (response as? HTTPURLResponse)?.statusCode == 200
    } catch {
        return false
    }
}

// MARK: - ServiceManager

@MainActor
final class ServiceManager: ObservableObject {
    @Published var services: [any ManagedService] = []

    let postgresService: PostgresService
    let tikaService: TikaService
    let embedderService: EmbedderService
    let llamaService: LlamaService
    let apiService: APIService
    let watcherService: WatcherService
    private var ioWorkers: [WorkerService] = []
    private var cpuWorkers: [WorkerService] = []
    /// Single-worker pool dedicated to LLM-bound stages (currently just
    /// `summarize`). llama-server runs with -np 1 on heavy models, so
    /// allowing multiple workers to pull summarize jobs would just have
    /// them busy-wait on the LLM lock and starve other IO/CPU work.
    /// Always exactly one worker; doesn't scale with preset.
    private var llmWorkers: [WorkerService] = []

    /// All worker services regardless of queue. Use this anywhere a
    /// generic "stop / restart / iterate over workers" is needed.
    private var allWorkers: [WorkerService] { ioWorkers + cpuWorkers + llmWorkers }

    /// Set by AppDelegate after creating the HealthChecker.
    weak var healthChecker: HealthChecker?

    private var startAllInProgress = false
    private var configChangeTask: Task<Void, Never>?
    private var configWatcherActive = false
    private var lastLlmModelId: String = ""
    /// 3-second mtime poller for config.json. We previously had a
    /// DispatchSource FSEvents watcher in addition, but it dropped events
    /// in practice (cause never diagnosed), forcing the user to relaunch
    /// the app to pick up model switches. The poller proved sufficient
    /// across the cross-topic research sweep, so the FSEvents path was
    /// removed to leave one source of truth.
    private var configPollTimer: DispatchSourceTimer?
    private var lastConfigMtime: Date = .distantPast

    // MARK: - Auto-restart flap detection

    /// Sliding-window restart history per service name.
    private var restartHistory: [String: [Date]] = [:]
    /// Max auto-restarts allowed within the window before declaring flapping.
    private let maxRestartsInWindow = 5
    /// Window duration for flap detection.
    private let restartWindowSeconds: TimeInterval = 300 // 5 minutes
    /// Guards against concurrent auto-restarts for the same service.
    private var autoRestartInProgress: Set<String> = []

    var overallState: ServiceState {
        Self.computeOverallState(services.map(\.state))
    }

    nonisolated static func computeOverallState(_ states: [ServiceState]) -> ServiceState {
        if states.isEmpty { return .stopped }
        if states.contains(.errored) { return .errored }
        if states.allSatisfy({ $0 == .running }) { return .running }
        if states.allSatisfy({ $0 == .stopped }) { return .stopped }
        if states.contains(.stopping) || states.contains(.shutdownPending) { return .stopping }
        return .starting
    }

    init() {
        let settings = AppSettings.shared

        postgresService = PostgresService()
        tikaService = TikaService()
        embedderService = EmbedderService()
        llamaService = LlamaService()
        apiService = APIService()
        watcherService = WatcherService()

        // Worker counts based on preset
        let cpuCount = ProcessInfo.processInfo.processorCount
        let counts = Self.workerCounts(preset: settings.workerPreset, cores: cpuCount)

        for i in 0..<counts.io {
            ioWorkers.append(WorkerService(queue: "io", index: i))
        }
        for i in 0..<counts.cpu {
            cpuWorkers.append(WorkerService(queue: "cpu", index: i))
        }
        for i in 0..<counts.llm {
            llmWorkers.append(WorkerService(queue: "llm", index: i))
        }

        services = [postgresService, tikaService, embedderService, llamaService, apiService, watcherService]
            + ioWorkers + cpuWorkers + llmWorkers
    }

    nonisolated static func workerCounts(preset: String, cores: Int) -> (io: Int, cpu: Int, llm: Int) {
        // LLM worker count is intentionally constant (1) regardless of
        // preset — see `llmWorkers` property doc for why.
        switch preset {
        case "quiet":
            return (1, 1, 1)
        case "fast":
            return (min(8, max(2, cores / 2)), 3, 1)
        default: // balanced
            return (min(8, max(2, cores / 4)), 2, 1)
        }
    }

    // MARK: - Orphan PID cleanup

    /// Path to the file tracking child PIDs across launches.
    private static let pidFileURL: URL = AppSettings.dataDir.appendingPathComponent("child-pids.txt")

    /// Record all current child PIDs to disk so orphans can be cleaned up on next launch.
    private func savePidFile() {
        var pids: [String] = []
        for service in services {
            if let pySvc = service as? PythonService, let proc = pySvc.process, proc.isRunning {
                pids.append(String(proc.processIdentifier))
            } else if let tika = service as? TikaService, let pid = tika.processIdentifier {
                pids.append(String(pid))
            } else if let llama = service as? LlamaService, let pid = llama.processIdentifier {
                pids.append(String(pid))
            }
        }
        let content = pids.joined(separator: "\n")
        try? content.write(to: Self.pidFileURL, atomically: true, encoding: .utf8)
    }

    /// Remove the PID file (called after orderly shutdown).
    private func removePidFile() {
        try? FileManager.default.removeItem(at: Self.pidFileURL)
    }

    /// Kill any orphaned child processes from a prior run that didn't shut down cleanly.
    private func killOrphanedProcesses() async {
        guard let content = try? String(contentsOf: Self.pidFileURL, encoding: .utf8) else { return }
        let pids = content.components(separatedBy: "\n")
            .compactMap { Int32($0.trimmingCharacters(in: .whitespaces)) }
        guard !pids.isEmpty else { return }

        var signalled: [Int32] = []
        for pid in pids {
            // Check if process is still alive
            guard kill(pid, 0) == 0 else { continue }
            Log.logger("lifecycle").warning("Killing orphaned process \(pid, privacy: .public) from prior run")
            kill(pid, SIGTERM)
            signalled.append(pid)
        }

        // Give SIGTERM a moment, then SIGKILL any survivors
        if !signalled.isEmpty {
            try? await Task.sleep(for: .seconds(2))
            for pid in signalled {
                if kill(pid, 0) == 0 {
                    Log.logger("lifecycle").warning("Orphaned process \(pid, privacy: .public) didn't exit, sending SIGKILL")
                    kill(pid, SIGKILL)
                }
            }
        }

        removePidFile()
    }

    // MARK: - Lifecycle

    /// Kill any process listening on the given port (leftover from a prior run).
    /// PostgreSQL handles stale PIDs via pg_ctl / postmaster.pid, so skip it.
    private func killStaleProcess(onPort port: Int) async {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/sbin/lsof")
        proc.arguments = ["-ti", "tcp:\(port)"]
        let pipe = Pipe()
        proc.standardOutput = pipe
        proc.standardError = FileHandle.nullDevice
        try? proc.run()
        let data: Data = await withCheckedContinuation { c in
            DispatchQueue.global().async {
                proc.waitUntilExit()
                let d = pipe.fileHandleForReading.readDataToEndOfFile()
                c.resume(returning: d)
            }
        }

        guard let output = String(data: data, encoding: .utf8)?
            .trimmingCharacters(in: .whitespacesAndNewlines),
            !output.isEmpty else { return }

        for line in output.components(separatedBy: "\n") {
            if let pid = Int32(line.trimmingCharacters(in: .whitespaces)) {
                Log.logger("lifecycle").warning(
                    "Killing stale process \(pid, privacy: .public) on port \(port, privacy: .public)")
                kill(pid, SIGTERM)
                try? await Task.sleep(for: .milliseconds(500))
                if kill(pid, 0) == 0 {
                    kill(pid, SIGKILL)
                }
            }
        }
    }

    func startAll() async {
        // Prevent concurrent startAll() calls (e.g. auto-start + user click)
        guard !startAllInProgress else { return }
        startAllInProgress = true
        defer { startAllInProgress = false }

        // Kill any orphaned processes from a prior unclean shutdown
        await killOrphanedProcesses()

        let settings = AppSettings.shared

        // Set base environment on all Python services
        let env = Self.pythonEnvironment()
        for service in services {
            if let pySvc = service as? PythonService {
                pySvc.baseEnvironment = env
            }
        }

        // Mark all services as startup pending before sequential start
        for service in services {
            if service.state == .stopped || service.state == .errored {
                service.state = .startupPending
            }
        }
        notifyStateChanged()

        // 1. PostgreSQL (handles stale PIDs internally via postmaster.pid)
        await startService(postgresService)

        // 2. Alembic migrations (blocks all downstream services on failure)
        guard await runMigrations() else { return }

        // 3. Tika (JVM startup can take ~30-60s)
        await killStaleProcess(onPort: settings.tikaPort)
        await startService(tikaService)

        // 4. Embedder (can take a while for model load)
        await killStaleProcess(onPort: settings.embedderPort)
        await startService(embedderService)

        // 5. LLM server (skip silently if no model selected)
        await killStaleProcess(onPort: settings.llamaPort)
        await startService(llamaService)

        // 6. API server
        await killStaleProcess(onPort: settings.apiPort)
        await startService(apiService)

        // 7. Workers
        for worker in allWorkers {
            await startService(worker)
        }

        // 8. Start the watcher daemon (talks to postgres directly; doesn't need API).
        //    Replaced the old Swift FSEvents WatchedFolderManager with a managed
        //    Python subprocess (`harbor-clerk-watcher`); see WatcherService.swift.
        await startService(watcherService)

        // 9. Watch config.json for model changes from the web UI
        startConfigWatcher()

        // 10. Persist child PIDs for orphan cleanup on next launch
        savePidFile()

        notifyStateChanged()
    }

    func stopAll() async {
        stopConfigWatcher()

        // Mark all running/starting services as shutdown pending
        for service in services {
            if service.state == .running || service.state == .starting || service.state == .startupPending {
                service.state = .shutdownPending
            }
        }
        notifyStateChanged()

        // Stop workers in parallel: SIGTERM all → wait-with-deadline all
        // concurrently. Pre-audit (2026-05-11) this loop reimplemented the
        // wait inline WITHOUT a SIGKILL fallback, so a single Python worker
        // stuck in a blocking syscall (or behind a Pipe+waitUntilExit
        // deadlock) would stall the entire shutdown indefinitely. The
        // rewrite uses the shared `Process.waitForExitWithDeadline` helper,
        // which detaches pipes and escalates to SIGKILL after the grace
        // period — bounded at ~12s regardless of how many workers are hung.
        for worker in allWorkers {
            worker.state = .stopping
            worker.process?.terminate()
        }
        notifyStateChanged()

        await withTaskGroup(of: Void.self) { group in
            for worker in allWorkers {
                let proc = worker.process
                let grace = worker.shutdownGracePeriod
                let workerName = worker.name
                group.addTask {
                    if let proc, proc.isRunning {
                        await proc.waitForExitWithDeadline(
                            graceSeconds: grace,
                            serviceName: workerName,
                        )
                    }
                    await MainActor.run {
                        worker.process = nil
                        worker.state = .stopped
                        self.notifyStateChanged()
                    }
                }
            }
        }

        // Stop remaining services in reverse order, notifying after each.
        // Sequential here is intentional: shutdown ordering matters
        // (api → llama → embedder → tika → postgres) because dependents
        // need their backends alive long enough to close cleanly. Each
        // service's own stop() uses the shared deadline helper so a hung
        // service still can't stall the next one for more than its grace
        // period.
        let nonWorkers = services.filter { svc in
            !allWorkers.contains(where: { ObjectIdentifier($0) == ObjectIdentifier(svc) })
        }.reversed()
        for service in nonWorkers {
            await service.stop()
            notifyStateChanged()
        }

        // All children stopped — remove PID file
        removePidFile()
    }

    /// Send SIGKILL to every child we know about. Used by the
    /// `applicationShouldTerminate` deadline path — when `stopAll()` has
    /// failed to complete in time and we'd rather have a noisy unclean
    /// shutdown than a menubar app the user can't quit without Terminal.
    ///
    /// Three sources for child PIDs:
    ///   1. Every `ManagedService.process` we currently hold a strong
    ///      reference to.
    ///   2. The `PostgresService` postmaster PID (read from
    ///      `postmaster.pid` inside the data dir).
    ///   3. `child-pids.txt` written by `savePidFile()` — catches any
    ///      child whose state we may have lost track of mid-shutdown.
    ///
    /// Synchronous and best-effort. We do *not* wait for the SIGKILLed
    /// processes to actually die — the caller (`AppDelegate`) will call
    /// `exit(0)` immediately after, and at that point the kernel will
    /// reparent any survivors to launchd.
    ///
    /// Note (Tier B follow-up): without process groups (see audit memo),
    /// this only catches the tracked PIDs, not their descendants. Tika's
    /// child JVMs and worker-spawned utilities can survive. Adding
    /// `posix_spawn` + `setpgid` + `killpg` is the durable fix.
    func forceKillEverything() {
        let logger = Log.logger("lifecycle")
        var killed: Set<Int32> = []

        // 1) Every Process we currently hold.
        for service in services {
            if let pySvc = service as? PythonService, let proc = pySvc.process {
                let pid = proc.processIdentifier
                if pid > 0 {
                    kill(pid, SIGKILL)
                    killed.insert(pid)
                }
            } else if let tika = service as? TikaService, let pid = tika.processIdentifier {
                kill(pid, SIGKILL)
                killed.insert(pid)
            } else if let llama = service as? LlamaService, let pid = llama.processIdentifier {
                kill(pid, SIGKILL)
                killed.insert(pid)
            }
        }

        // 2) Postgres — pg_ctl forks a postmaster we don't directly hold;
        //    read its PID from the data dir so it doesn't survive as an
        //    orphan still holding port 5433.
        let pgPidFile = AppSettings.shared.postgresDataDir.appendingPathComponent("postmaster.pid")
        if let contents = try? String(contentsOf: pgPidFile, encoding: .utf8),
           let pidLine = contents.components(separatedBy: "\n").first,
           let pid = Int32(pidLine), pid > 0, !killed.contains(pid)
        {
            kill(pid, SIGKILL)
            killed.insert(pid)
        }

        // 3) child-pids.txt — anything our in-memory state lost track of
        //    (e.g. an auto-restart that swapped the Process ref after the
        //    last savePidFile call).
        if let contents = try? String(contentsOf: Self.pidFileURL, encoding: .utf8) {
            for line in contents.components(separatedBy: "\n") {
                let trimmed = line.trimmingCharacters(in: .whitespaces)
                guard let pid = Int32(trimmed), pid > 0, !killed.contains(pid) else { continue }
                // kill(pid, 0) confirms the PID still belongs to a live
                // process before we SIGKILL — a recycled PID belonging to
                // someone else's process would be a serious bug.
                guard kill(pid, 0) == 0 else { continue }
                kill(pid, SIGKILL)
                killed.insert(pid)
            }
        }

        logger.warning("forceKillEverything sent SIGKILL to \(killed.count, privacy: .public) tracked PIDs")

        // Don't leave the PID file behind — next launch's orphan cleanup
        // shouldn't re-try to kill processes we already SIGKILLed.
        removePidFile()
    }

    func stopService(_ service: any ManagedService) async {
        await service.stop()
        savePidFile()
        notifyStateChanged()
    }

    func restartService(_ service: any ManagedService) async {
        // Reload settings before restart. The config-watcher path (see
        // startConfigWatcher) is unreliable, so AppSettings.shared can be
        // stale relative to config.json — without this reload, a manual
        // "Restart LLM" right after a model switch would relaunch llama
        // with the OLD model_id from cached settings.
        AppSettings.shared.reload()
        await service.stop()
        notifyStateChanged()
        // 5s (was 1s) — same rationale as the model-switch path: the old
        // process's TCP socket needs time to release before the new process
        // can rebind. 1s wasn't enough in practice and caused
        // "couldn't bind HTTP server socket" failures + flap-protection
        // trip when used as part of an automated sweep.
        try? await Task.sleep(for: .seconds(5))
        await startService(service)
    }

    /// Auto-restart an errored service with flap detection.
    /// Called from HealthChecker (consecutive failures) and onUnexpectedExit callbacks (crash).
    func attemptAutoRestart(_ service: any ManagedService) async {
        // Don't auto-restart Postgres (data safety) or during shutdown
        guard !(service is PostgresService) else { return }
        guard service.state != .shutdownPending else { return }
        // Prevent concurrent auto-restarts for the same service
        guard !autoRestartInProgress.contains(service.name) else { return }
        autoRestartInProgress.insert(service.name)
        defer { autoRestartInProgress.remove(service.name) }

        // Flap detection: trim old entries, check rate
        let now = Date()
        let cutoff = now.addingTimeInterval(-restartWindowSeconds)
        var history = restartHistory[service.name, default: []].filter { $0 > cutoff }

        if history.count >= maxRestartsInWindow {
            Log.logger("lifecycle").error(
                "[\(service.name, privacy: .public)] Flapping (\(history.count, privacy: .public) restarts in 5m) — not restarting")
            return
        }

        // Backoff before retrying: if the most-recent restart was < 8s ago,
        // sleep before trying again. Without this, 5 startup-failures in
        // <10s blow through the entire flap budget instantly (observed
        // during automated model-switch sweep — port-bind race caused
        // each restart to fail in <2s, tripping flap protection in ~6s).
        if let last = history.last, now.timeIntervalSince(last) < 8.0 {
            let wait = 8.0 - now.timeIntervalSince(last)
            Log.logger("lifecycle").info(
                "[\(service.name, privacy: .public)] Backing off \(String(format: "%.1f", wait), privacy: .public)s before retry")
            try? await Task.sleep(for: .seconds(Int(ceil(wait))))
        }

        history.append(now)
        restartHistory[service.name] = history

        Log.logger("lifecycle").info(
            "[\(service.name, privacy: .public)] Auto-restarting (attempt \(history.count, privacy: .public)/\(self.maxRestartsInWindow, privacy: .public) in window)")

        // Pause health checker for this service during restart
        healthChecker?.pausedServices.insert(service.name)
        defer { healthChecker?.pausedServices.remove(service.name) }

        await restartService(service)
    }

    func startService(_ service: any ManagedService) async {
        // Bail if a shutdown was requested while we were waiting to start
        guard service.state != .shutdownPending else { return }

        // Ensure Python services have env set
        if let pySvc = service as? PythonService, pySvc.baseEnvironment.isEmpty {
            pySvc.baseEnvironment = Self.pythonEnvironment()
        }
        service.state = .starting
        notifyStateChanged()

        // Wire up auto-restart callback for crash recovery
        if let tika = service as? TikaService {
            tika.onUnexpectedExit = { [weak self, weak tika] in
                guard let self, let svc = tika else { return }
                Task { @MainActor in await self.attemptAutoRestart(svc) }
            }
        } else if let llama = service as? LlamaService {
            llama.onUnexpectedExit = { [weak self, weak llama] in
                guard let self, let svc = llama else { return }
                Task { @MainActor in await self.attemptAutoRestart(svc) }
            }
        } else if let pySvc = service as? PythonService {
            pySvc.onUnexpectedExit = { [weak self, weak pySvc] in
                guard let self, let svc = pySvc else { return }
                Task { @MainActor in await self.attemptAutoRestart(svc) }
            }
        }

        do {
            try await service.start()

            // Service chose not to start (e.g. LLM with no model selected)
            if service.state == .stopped {
                notifyStateChanged()
                return
            }

            // Wait for health check with timeout
            let timeout: TimeInterval = (service is EmbedderService || service is LlamaService) ? 120 : (service is TikaService) ? 60 : 30
            let deadline = Date().addingTimeInterval(timeout)
            while Date() < deadline {
                // Bail early if the process died — no point waiting for the full timeout
                if let pySvc = service as? PythonService, pySvc.process?.isRunning != true {
                    service.state = .errored
                    notifyStateChanged()
                    Log.logger("lifecycle").error("[\(service.name, privacy: .public)] Process exited during startup")
                    return
                }
                if await service.healthCheck() {
                    service.state = .running
                    // Reset crash counter now that the service is confirmed healthy
                    if let pySvc = service as? PythonService {
                        pySvc.resetRestartCount()
                    }
                    savePidFile()
                    notifyStateChanged()
                    return
                }
                try? await Task.sleep(for: .seconds(1))
            }

            // Timeout waiting for health
            service.state = .errored
            notifyStateChanged()
            Log.logger("lifecycle").error("[\(service.name, privacy: .public)] Health check timeout")
        } catch {
            service.state = .errored
            notifyStateChanged()
            Log.logger("lifecycle").error("[\(service.name, privacy: .public)] Start failed: \(error.localizedDescription, privacy: .public)")
        }
    }

    /// Run Alembic migrations. Returns true on success, false on failure.
    /// On failure, sets all pending services to errored so they don't start with a stale schema.
    @discardableResult
    private func runMigrations() async -> Bool {
        let runner = MigrationRunner()
        do {
            try await runner.run()
            return true
        } catch {
            Log.logger("alembic").error(
                "Database migration failed after 3 attempts: \(error.localizedDescription, privacy: .public). Services will not start. Try restarting, or use System Maintenance > Run Migrations."
            )
            // Block downstream services — running with stale schema is worse than not running
            for service in services where service.state == .startupPending {
                service.state = .errored
            }
            notifyStateChanged()
            return false
        }
    }

    func notifyStateChanged() {
        objectWillChange.send()
        NotificationCenter.default.post(name: .servicesStateChanged, object: nil)
    }

    // MARK: - Targeted restart

    /// Restart only the services affected by the given changed setting keys.
    func restartForChangedSettings(_ changedKeys: Set<String>) async {
        // Determine which infrastructure and python services need restart
        var infraToRestart: [any ManagedService] = []
        var pythonToRestart: Set<ObjectIdentifier> = []
        var needsMigrations = false
        var needsWorkerRecreate = false

        for key in changedKeys {
            switch key {
            case "postgres_port":
                infraToRestart.append(postgresService)
                pythonToRestart.insert(ObjectIdentifier(apiService))
                for w in allWorkers { pythonToRestart.insert(ObjectIdentifier(w)) }
                needsMigrations = true

            case "tika_port":
                infraToRestart.append(tikaService)
                pythonToRestart.insert(ObjectIdentifier(apiService))
                for w in allWorkers { pythonToRestart.insert(ObjectIdentifier(w)) }

            case "embedder_port":
                infraToRestart.append(embedderService)
                pythonToRestart.insert(ObjectIdentifier(apiService))
                for w in allWorkers { pythonToRestart.insert(ObjectIdentifier(w)) }

            case "llama_port":
                infraToRestart.append(llamaService)
                pythonToRestart.insert(ObjectIdentifier(apiService))

            case "llm_model_id", "llm_yarn_enabled":
                infraToRestart.append(llamaService)

            case "api_port", "allow_remote_web", "allow_remote_mcp":
                pythonToRestart.insert(ObjectIdentifier(apiService))

            case "worker_preset":
                needsWorkerRecreate = true

            case "log_level":
                pythonToRestart.insert(ObjectIdentifier(apiService))
                pythonToRestart.insert(ObjectIdentifier(embedderService))
                for w in allWorkers { pythonToRestart.insert(ObjectIdentifier(w)) }

            default:
                break
            }
        }

        // Deduplicate infra list
        var seenInfra = Set<ObjectIdentifier>()
        infraToRestart = infraToRestart.filter { seenInfra.insert(ObjectIdentifier($0)).inserted }

        // Collect python services to restart (excluding workers if we're recreating them)
        let workersToStop: [WorkerService] = needsWorkerRecreate ? allWorkers : allWorkers.filter { pythonToRestart.contains(ObjectIdentifier($0)) }
        // Embedder before API to match startAll() ordering
        let nonWorkerPython: [any ManagedService] = [embedderService, apiService].filter { pythonToRestart.contains(ObjectIdentifier($0)) }

        // 1. Stop python services (dependents first: workers → api/embedder)
        // Send SIGTERM to all workers in parallel, then wait for all to exit.
        // This reduces total stop time from N×30s to ~30s.
        for worker in workersToStop {
            worker.state = .stopping
            worker.process?.terminate()
        }
        notifyStateChanged()
        for worker in workersToStop {
            if let proc = worker.process, proc.isRunning {
                await withCheckedContinuation { (c: CheckedContinuation<Void, Never>) in
                    DispatchQueue.global().async {
                        proc.waitUntilExit()
                        c.resume()
                    }
                }
            }
            worker.process = nil
            worker.state = .stopped
        }
        notifyStateChanged()
        for svc in nonWorkerPython {
            await svc.stop()
        }
        notifyStateChanged()

        // 2. Stop infrastructure services (reset errored state + flap history for fresh start)
        for svc in infraToRestart {
            if svc.state == .errored {
                svc.state = .stopped
                restartHistory.removeValue(forKey: svc.name)
            } else {
                await svc.stop()
            }
        }
        notifyStateChanged()

        // 3. Start infrastructure services (startup order)
        let infraOrder: [any ManagedService] = [postgresService, tikaService, embedderService, llamaService]
        for svc in infraOrder where infraToRestart.contains(where: { ObjectIdentifier($0) == ObjectIdentifier(svc) }) {
            await startService(svc)
        }

        // 4. Run migrations if needed
        if needsMigrations {
            await runMigrations()
        }

        // 5. Recreate workers if preset changed
        if needsWorkerRecreate {
            recreateWorkers()
        }

        // 6. Update environment on all affected python services
        let env = Self.pythonEnvironment()
        for svc in nonWorkerPython {
            if let pySvc = svc as? PythonService {
                pySvc.baseEnvironment = env
            }
        }
        // Workers always get fresh env (whether recreated or restarted)
        for worker in allWorkers {
            worker.baseEnvironment = env
        }

        // 7. Reset crash counters so stale counts from prior runs don't cause
        //    premature .errored states after an intentional restart.
        for svc in nonWorkerPython {
            (svc as? PythonService)?.resetRestartCount()
        }
        for worker in allWorkers {
            worker.resetRestartCount()
        }

        // 8. Start affected python services (embedder → api → workers)
        for svc in nonWorkerPython {
            await startService(svc)
        }
        let workersToStart = needsWorkerRecreate ? allWorkers : allWorkers.filter { pythonToRestart.contains(ObjectIdentifier($0)) }
        for worker in workersToStart {
            await startService(worker)
        }

        notifyStateChanged()
    }

    /// Tear down old workers and create new ones based on current preset.
    func recreateWorkers() {
        // Clear restart history for old workers so new ones don't inherit stale flap state
        for worker in allWorkers {
            restartHistory[worker.name] = nil
        }

        // Remove old workers from services array
        let oldWorkerIds = Set(allWorkers.map { ObjectIdentifier($0) })
        services.removeAll { oldWorkerIds.contains(ObjectIdentifier($0)) }

        // Create new workers
        let cpuCount = ProcessInfo.processInfo.processorCount
        let settings = AppSettings.shared
        let counts = Self.workerCounts(preset: settings.workerPreset, cores: cpuCount)

        ioWorkers = (0..<counts.io).map { WorkerService(queue: "io", index: $0) }
        cpuWorkers = (0..<counts.cpu).map { WorkerService(queue: "cpu", index: $0) }
        llmWorkers = (0..<counts.llm).map { WorkerService(queue: "llm", index: $0) }

        services.append(contentsOf: allWorkers)

        Log.logger("lifecycle").info(
            "Recreated workers: \(counts.io, privacy: .public) io, \(counts.cpu, privacy: .public) cpu, \(counts.llm, privacy: .public) llm (preset: \(settings.workerPreset, privacy: .public))"
        )
    }

    // MARK: - Config file watcher

    /// Start watching config.json for changes from the Python side.
    ///
    /// Implementation: a 3-second mtime poller. We previously also wired
    /// a DispatchSource FSEvents watcher for instant response, but it
    /// silently dropped events in practice (cause never diagnosed) and
    /// the user had to relaunch the whole app to pick up model switches.
    /// The poller covered every observed change across the cross-topic
    /// sweep, so the FSEvents path was removed to leave one code path
    /// to reason about. 3 s is fast enough for the model-switch UX
    /// (the full restart costs ~5 s anyway) and cheap (one stat per
    /// tick).
    func startConfigWatcher() {
        configWatcherActive = true
        let settings = AppSettings.shared
        lastLlmModelId = settings.llmModelId
        let path = settings.configURL.path

        // Seed the mtime baseline so the first tick doesn't fire on
        // whatever the file already contains.
        if let attrs = try? FileManager.default.attributesOfItem(atPath: path),
           let mtime = attrs[.modificationDate] as? Date {
            lastConfigMtime = mtime
        }

        let timer = DispatchSource.makeTimerSource(queue: .global(qos: .utility))
        timer.schedule(deadline: .now() + .seconds(3), repeating: .seconds(3))
        timer.setEventHandler { [weak self] in
            guard let self else { return }
            guard let attrs = try? FileManager.default.attributesOfItem(atPath: path),
                  let mtime = attrs[.modificationDate] as? Date else { return }
            // Round to milliseconds to dodge HFS/APFS 1-sec resolution false-positives
            if mtime > self.lastConfigMtime.addingTimeInterval(0.001) {
                let prev = self.lastConfigMtime
                self.lastConfigMtime = mtime
                Task { @MainActor in
                    Log.logger("lifecycle").info(
                        "config.json mtime changed (\(prev.timeIntervalSince1970, privacy: .public) → \(mtime.timeIntervalSince1970, privacy: .public)) — running handleConfigChange")
                    self.handleConfigChange()
                }
            }
        }
        timer.resume()
        configPollTimer = timer
    }

    func stopConfigWatcher() {
        configWatcherActive = false
        configChangeTask?.cancel()
        configChangeTask = nil
        configPollTimer?.cancel()
        configPollTimer = nil
    }

    private func handleConfigChange() {
        guard configWatcherActive else { return }
        let settings = AppSettings.shared
        settings.reload()

        // Check for LLM restart signal from Python (persistent 5xx detection)
        if settings.llmRestartRequested {
            Log.logger("lifecycle").warning("LLM restart requested by API (persistent 5xx)")
            settings.clearLlmRestart()
            configChangeTask?.cancel()
            configChangeTask = Task {
                if llamaService.state == .running || llamaService.state == .starting {
                    await llamaService.stop()
                    notifyStateChanged()
                    try? await Task.sleep(for: .seconds(1))
                } else if llamaService.state == .errored {
                    llamaService.state = .stopped
                    restartHistory.removeValue(forKey: llamaService.name)
                    notifyStateChanged()
                }
                if !settings.llmModelId.isEmpty {
                    await startService(llamaService)
                }
            }
            return
        }

        let newModelId = settings.llmModelId
        guard newModelId != lastLlmModelId else { return }

        let previousId = lastLlmModelId
        lastLlmModelId = newModelId
        Log.logger("lifecycle").info(
            "Config change: llm_model_id \(previousId, privacy: .public) → \(newModelId, privacy: .public)"
        )

        configChangeTask?.cancel()
        configChangeTask = Task {
            // Stop current llama-server if running or errored
            if llamaService.state == .running || llamaService.state == .starting {
                await llamaService.stop()
                notifyStateChanged()
                // 1s wasn't enough on macOS — llama-server's old socket
                // sometimes hadn't released port 8102 by the time the new
                // process tried to bind, causing "couldn't bind HTTP server
                // socket" and an immediate exit. 5s gives the kernel ample
                // time to release the port.
                try? await Task.sleep(for: .seconds(5))
            } else if llamaService.state == .errored {
                // Reset errored state and clear flap history so the new model gets a fresh start
                llamaService.state = .stopped
                restartHistory.removeValue(forKey: llamaService.name)
                notifyStateChanged()
            }

            if newModelId.isEmpty {
                // Model deactivated — stay stopped
                llamaService.state = .stopped
                notifyStateChanged()
            } else {
                // Start with new model
                await startService(llamaService)
            }

            // Update Python services' env so restarts pick up the new model ID
            let env = Self.pythonEnvironment()
            for service in services {
                if let pySvc = service as? PythonService {
                    pySvc.baseEnvironment = env
                }
            }
        }
    }

    // MARK: - Environment

    /// Build the full environment dict for Python services.
    nonisolated static func pythonEnvironment() -> [String: String] {
        let settings = AppSettings.shared
        let bundle = Bundle.main.resourceURL!

        let hashFile = bundle.appendingPathComponent("venv/build-hash.txt")
        let buildHash = (try? String(contentsOf: hashFile, encoding: .utf8))?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? "unknown"

        // Master encryption key for secret storage. See MasterKeyManager and the
        // Python harbor_clerk.secrets module. Read from Keychain (the bootstrap
        // call in AppDelegate.applicationDidFinishLaunching ensures it exists)
        // and exported to subprocesses as the same env var Docker operators set
        // directly — one Python code path on both deployments.
        let masterKey = MasterKeyManager.production.loadOrGenerate()
        let masterKeyB64 = MasterKeyManager.production.base64Encoded(key: masterKey)

        return [
            "BUILD_HASH": buildHash,
            "DATABASE_URL": "postgresql+asyncpg://lka@localhost:\(settings.postgresPort)/lka",
            "STORAGE_BACKEND": "filesystem",
            "STORAGE_PATH": settings.originalsDir.path,
            "EMBEDDER_URL": "http://localhost:\(settings.embedderPort)",
            "TIKA_URL": "http://localhost:\(settings.tikaPort)",
            "SECRET_KEY": settings.secretKey,
            "LOG_LEVEL": settings.logLevel,
            "STATIC_DIR": bundle.appendingPathComponent("frontend-dist").path,
            "API_HOST": (settings.allowRemoteWeb || settings.allowRemoteMCP) ? "0.0.0.0" : "127.0.0.1",
            "API_PORT": String(settings.apiPort),
            "PATH": [
                bundle.appendingPathComponent("venv/bin").path,
                bundle.appendingPathComponent("tesseract/bin").path,
                bundle.appendingPathComponent("java/bin").path,
                "/usr/bin",
                "/bin",
            ].joined(separator: ":"),
            "TESSDATA_PREFIX": bundle.appendingPathComponent("tesseract/share/tessdata").path,
            "LLAMA_SERVER_URL": "http://localhost:\(settings.llamaPort)",
            "LLM_MODEL_ID": settings.llmModelId,
            "LLM_YARN_ENABLED": settings.llmYarnEnabled ? "true" : "false",
            "MODELS_DIR": settings.modelsDir.path,
            "NATIVE_CONFIG_FILE": settings.configURL.path,
            "HARBOR_CLERK_MASTER_KEY": masterKeyB64,
        ]
    }
}

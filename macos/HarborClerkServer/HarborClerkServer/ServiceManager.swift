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

// MARK: - ServiceManager

@MainActor
final class ServiceManager: ObservableObject {
    @Published var services: [any ManagedService] = []

    let postgresService: PostgresService
    let tikaService: TikaService
    let embedderService: EmbedderService
    let llamaService: LlamaService
    let apiService: APIService
    private var ioWorkers: [WorkerService] = []
    private var cpuWorkers: [WorkerService] = []

    private var configWatcherSource: DispatchSourceFileSystemObject?
    private var configFileDescriptor: Int32 = -1
    private var configChangeTask: Task<Void, Never>?
    private var configWatcherActive = false
    private var lastLlmModelId: String = ""

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

        // Worker counts based on preset
        let cpuCount = ProcessInfo.processInfo.processorCount
        let (ioCount, cpuWorkerCount) = Self.workerCounts(preset: settings.workerPreset, cores: cpuCount)

        for i in 0..<ioCount {
            ioWorkers.append(WorkerService(queue: "io", index: i))
        }
        for i in 0..<cpuWorkerCount {
            cpuWorkers.append(WorkerService(queue: "cpu", index: i))
        }

        services = [postgresService, tikaService, embedderService, llamaService, apiService]
            + ioWorkers + cpuWorkers
    }

    nonisolated static func workerCounts(preset: String, cores: Int) -> (io: Int, cpu: Int) {
        switch preset {
        case "quiet":
            return (1, 1)
        case "fast":
            return (min(8, max(2, cores / 2)), min(2, max(1, cores / 4)))
        default: // balanced
            return (min(8, max(2, cores / 4)), 1)
        }
    }

    // MARK: - Lifecycle

    /// Kill any process listening on the given port (leftover from a prior run).
    /// PostgreSQL handles stale PIDs via pg_ctl / postmaster.pid, so skip it.
    nonisolated private func killStaleProcess(onPort port: Int) {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/sbin/lsof")
        proc.arguments = ["-ti", "tcp:\(port)"]
        let pipe = Pipe()
        proc.standardOutput = pipe
        proc.standardError = FileHandle.nullDevice
        try? proc.run()
        proc.waitUntilExit()

        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        guard let output = String(data: data, encoding: .utf8)?
            .trimmingCharacters(in: .whitespacesAndNewlines),
            !output.isEmpty else { return }

        for line in output.components(separatedBy: "\n") {
            if let pid = Int32(line.trimmingCharacters(in: .whitespaces)) {
                Log.logger("lifecycle").warning(
                    "Killing stale process \(pid, privacy: .public) on port \(port, privacy: .public)")
                kill(pid, SIGTERM)
                usleep(500_000) // 0.5s grace
                if kill(pid, 0) == 0 {
                    kill(pid, SIGKILL)
                }
            }
        }
    }

    func startAll() async {
        let settings = AppSettings.shared

        // Set base environment on all Python services
        let env = pythonEnvironment()
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

        // 2. Alembic migrations
        await runMigrations()

        // 3. Tika (JVM startup can take ~30-60s)
        killStaleProcess(onPort: settings.tikaPort)
        await startService(tikaService)

        // 4. Embedder (can take a while for model load)
        killStaleProcess(onPort: settings.embedderPort)
        await startService(embedderService)

        // 5. LLM server (skip silently if no model selected)
        killStaleProcess(onPort: settings.llamaPort)
        await startService(llamaService)

        // 6. API server
        killStaleProcess(onPort: settings.apiPort)
        await startService(apiService)

        // 7. Workers
        for worker in ioWorkers + cpuWorkers {
            await startService(worker)
        }

        // 8. Watch config.json for model changes from the web UI
        startConfigWatcher()

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

        // Stop workers in parallel (SIGTERM all, then wait) — same as restartForChangedSettings
        let allWorkers = ioWorkers + cpuWorkers
        for worker in allWorkers {
            worker.state = .stopping
            worker.process?.terminate()
        }
        notifyStateChanged()
        for worker in allWorkers {
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

        // Stop remaining services in reverse order, notifying after each
        let nonWorkers = services.filter { svc in
            !allWorkers.contains(where: { ObjectIdentifier($0) == ObjectIdentifier(svc) })
        }.reversed()
        for service in nonWorkers {
            await service.stop()
            notifyStateChanged()
        }
    }

    func stopService(_ service: any ManagedService) async {
        await service.stop()
        notifyStateChanged()
    }

    func restartService(_ service: any ManagedService) async {
        await service.stop()
        notifyStateChanged()
        try? await Task.sleep(for: .seconds(1))
        await startService(service)
    }

    func startService(_ service: any ManagedService) async {
        // Ensure Python services have env set
        if let pySvc = service as? PythonService, pySvc.baseEnvironment.isEmpty {
            pySvc.baseEnvironment = pythonEnvironment()
        }
        service.state = .starting
        notifyStateChanged()

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

    private func runMigrations() async {
        let runner = MigrationRunner()
        do {
            try await runner.run()
            Log.logger("alembic").info("Migrations complete")
        } catch {
            Log.logger("alembic").error("Migration failed: \(error.localizedDescription, privacy: .public)")
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
                for w in ioWorkers + cpuWorkers { pythonToRestart.insert(ObjectIdentifier(w)) }
                needsMigrations = true

            case "tika_port":
                infraToRestart.append(tikaService)
                pythonToRestart.insert(ObjectIdentifier(apiService))
                for w in ioWorkers + cpuWorkers { pythonToRestart.insert(ObjectIdentifier(w)) }

            case "embedder_port":
                infraToRestart.append(embedderService)
                pythonToRestart.insert(ObjectIdentifier(apiService))
                for w in ioWorkers + cpuWorkers { pythonToRestart.insert(ObjectIdentifier(w)) }

            case "llama_port":
                infraToRestart.append(llamaService)
                pythonToRestart.insert(ObjectIdentifier(apiService))

            case "llm_model_id":
                infraToRestart.append(llamaService)

            case "api_port", "allow_remote_web", "allow_remote_mcp":
                pythonToRestart.insert(ObjectIdentifier(apiService))

            case "worker_preset":
                needsWorkerRecreate = true

            case "log_level":
                pythonToRestart.insert(ObjectIdentifier(apiService))
                pythonToRestart.insert(ObjectIdentifier(embedderService))
                for w in ioWorkers + cpuWorkers { pythonToRestart.insert(ObjectIdentifier(w)) }

            default:
                break
            }
        }

        // Deduplicate infra list
        var seenInfra = Set<ObjectIdentifier>()
        infraToRestart = infraToRestart.filter { seenInfra.insert(ObjectIdentifier($0)).inserted }

        // Collect python services to restart (excluding workers if we're recreating them)
        let workersToStop: [WorkerService] = needsWorkerRecreate ? ioWorkers + cpuWorkers : (ioWorkers + cpuWorkers).filter { pythonToRestart.contains(ObjectIdentifier($0)) }
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

        // 2. Stop infrastructure services
        for svc in infraToRestart {
            await svc.stop()
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
        let env = pythonEnvironment()
        for svc in nonWorkerPython {
            if let pySvc = svc as? PythonService {
                pySvc.baseEnvironment = env
            }
        }
        // Workers always get fresh env (whether recreated or restarted)
        for worker in ioWorkers + cpuWorkers {
            worker.baseEnvironment = env
        }

        // 7. Reset crash counters so stale counts from prior runs don't cause
        //    premature .errored states after an intentional restart.
        for svc in nonWorkerPython {
            (svc as? PythonService)?.resetRestartCount()
        }
        for worker in ioWorkers + cpuWorkers {
            worker.resetRestartCount()
        }

        // 8. Start affected python services (embedder → api → workers)
        for svc in nonWorkerPython {
            await startService(svc)
        }
        let allWorkers = ioWorkers + cpuWorkers
        let workersToStart = needsWorkerRecreate ? allWorkers : allWorkers.filter { pythonToRestart.contains(ObjectIdentifier($0)) }
        for worker in workersToStart {
            await startService(worker)
        }

        notifyStateChanged()
    }

    /// Tear down old workers and create new ones based on current preset.
    func recreateWorkers() {
        // Remove old workers from services array
        let oldWorkerIds = Set((ioWorkers + cpuWorkers).map { ObjectIdentifier($0) })
        services.removeAll { oldWorkerIds.contains(ObjectIdentifier($0)) }

        // Create new workers
        let cpuCount = ProcessInfo.processInfo.processorCount
        let settings = AppSettings.shared
        let (ioCount, cpuWorkerCount) = Self.workerCounts(preset: settings.workerPreset, cores: cpuCount)

        ioWorkers = (0..<ioCount).map { WorkerService(queue: "io", index: $0) }
        cpuWorkers = (0..<cpuWorkerCount).map { WorkerService(queue: "cpu", index: $0) }

        services.append(contentsOf: ioWorkers + cpuWorkers)

        Log.logger("lifecycle").info(
            "Recreated workers: \(ioCount, privacy: .public) io, \(cpuWorkerCount, privacy: .public) cpu (preset: \(settings.workerPreset, privacy: .public))"
        )
    }

    // MARK: - Config file watcher

    /// Start watching config.json for changes from the Python side.
    func startConfigWatcher() {
        configWatcherActive = true
        let settings = AppSettings.shared
        lastLlmModelId = settings.llmModelId
        let path = settings.configURL.path

        let fd = open(path, O_EVTONLY)
        guard fd >= 0 else {
            Log.logger("lifecycle").error("Cannot open config.json for watching")
            return
        }
        configFileDescriptor = fd

        let source = DispatchSource.makeFileSystemObjectSource(
            fileDescriptor: fd,
            eventMask: [.write, .rename],
            queue: .global(qos: .utility)
        )
        source.setEventHandler { [weak self] in
            // After an atomic write (rename), the fd points to the old inode.
            // Re-create the watcher to track the new file.
            let flags = source.data
            Task { @MainActor in
                self?.handleConfigChange()
                if flags.contains(.rename) {
                    self?.stopConfigWatcher()
                    self?.startConfigWatcher()
                }
            }
        }
        source.setCancelHandler {
            close(fd)
        }
        source.resume()
        configWatcherSource = source
    }

    func stopConfigWatcher() {
        configWatcherActive = false
        configChangeTask?.cancel()
        configChangeTask = nil
        configWatcherSource?.cancel()
        configWatcherSource = nil
        configFileDescriptor = -1
    }

    private func handleConfigChange() {
        guard configWatcherActive else { return }
        let settings = AppSettings.shared
        settings.reload()

        let newModelId = settings.llmModelId
        guard newModelId != lastLlmModelId else { return }

        let previousId = lastLlmModelId
        lastLlmModelId = newModelId
        Log.logger("lifecycle").info(
            "Config change: llm_model_id \(previousId, privacy: .public) → \(newModelId, privacy: .public)"
        )

        configChangeTask?.cancel()
        configChangeTask = Task {
            // Stop current llama-server if running
            if llamaService.state == .running || llamaService.state == .starting {
                await llamaService.stop()
                notifyStateChanged()
                try? await Task.sleep(for: .seconds(1))
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
            let env = pythonEnvironment()
            for service in services {
                if let pySvc = service as? PythonService {
                    pySvc.baseEnvironment = env
                }
            }
        }
    }

    // MARK: - Environment

    /// Build the full environment dict for Python services.
    func pythonEnvironment() -> [String: String] {
        let settings = AppSettings.shared
        let bundle = Bundle.main.resourceURL!

        let hashFile = bundle.appendingPathComponent("venv/build-hash.txt")
        let buildHash = (try? String(contentsOf: hashFile, encoding: .utf8))?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? "unknown"

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
            "MODELS_DIR": settings.modelsDir.path,
            "NATIVE_CONFIG_FILE": settings.configURL.path,
        ]
    }
}

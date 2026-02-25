import Foundation
import os

// MARK: - Service protocol & state

enum ServiceState: String, CaseIterable {
    case stopped, starting, running, stopping, errored
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
    private var lastLlmModelId: String = ""

    var overallState: ServiceState {
        Self.computeOverallState(services.map(\.state))
    }

    nonisolated static func computeOverallState(_ states: [ServiceState]) -> ServiceState {
        if states.isEmpty { return .stopped }
        if states.contains(.errored) { return .errored }
        if states.allSatisfy({ $0 == .running }) { return .running }
        if states.allSatisfy({ $0 == .stopped }) { return .stopped }
        if states.contains(.stopping) { return .stopping }
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

    func startAll() async {
        // Set base environment on all Python services
        let env = pythonEnvironment()
        for service in services {
            if let pySvc = service as? PythonService {
                pySvc.baseEnvironment = env
            }
        }

        // 1. PostgreSQL
        await startService(postgresService)

        // 2. Alembic migrations
        await runMigrations()

        // 3. Tika (JVM startup can take ~30-60s)
        await startService(tikaService)

        // 4. Embedder (can take a while for model load)
        await startService(embedderService)

        // 5. LLM server (skip silently if no model selected)
        await startService(llamaService)

        // 6. API server
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
        // Reverse order
        let reversed = Array(services.reversed())
        for service in reversed {
            await service.stop()
        }
        notifyStateChanged()
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

            // Wait for health check with timeout
            let timeout: TimeInterval = (service is EmbedderService || service is LlamaService) ? 120 : (service is TikaService) ? 60 : 30
            let deadline = Date().addingTimeInterval(timeout)
            while Date() < deadline {
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

    // MARK: - Config file watcher

    /// Start watching config.json for changes from the Python side.
    func startConfigWatcher() {
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
        configWatcherSource?.cancel()
        configWatcherSource = nil
        configFileDescriptor = -1
    }

    private func handleConfigChange() {
        let settings = AppSettings.shared
        settings.reload()

        let newModelId = settings.llmModelId
        guard newModelId != lastLlmModelId else { return }

        let previousId = lastLlmModelId
        lastLlmModelId = newModelId
        Log.logger("lifecycle").info(
            "Config change: llm_model_id \(previousId, privacy: .public) → \(newModelId, privacy: .public)"
        )

        Task {
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

        return [
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

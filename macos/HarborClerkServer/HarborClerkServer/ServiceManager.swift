import Foundation

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
    func stop()
    func healthCheck() async -> Bool
}

// MARK: - ServiceManager

@MainActor
final class ServiceManager: ObservableObject {
    @Published var services: [any ManagedService] = []

    let postgresService: PostgresService
    let redisService: RedisService
    let embedderService: EmbedderService
    let llamaService: LlamaService
    let apiService: APIService
    private var ioWorkers: [WorkerService] = []
    private var cpuWorkers: [WorkerService] = []

    var overallState: ServiceState {
        if services.contains(where: { $0.state == .errored }) { return .errored }
        if services.allSatisfy({ $0.state == .running }) { return .running }
        if services.allSatisfy({ $0.state == .stopped }) { return .stopped }
        if services.contains(where: { $0.state == .stopping }) { return .stopping }
        return .starting
    }

    init() {
        let settings = AppSettings.shared

        postgresService = PostgresService()
        redisService = RedisService()
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

        services = [postgresService, redisService, embedderService, llamaService, apiService]
            + ioWorkers + cpuWorkers
    }

    static func workerCounts(preset: String, cores: Int) -> (io: Int, cpu: Int) {
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

        // 3. Redis
        await startService(redisService)

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

        notifyStateChanged()
    }

    func stopAll() {
        // Reverse order
        let reversed = Array(services.reversed())
        for service in reversed {
            service.stop()
        }
        notifyStateChanged()
    }

    private func startService(_ service: any ManagedService) async {
        service.state = .starting
        notifyStateChanged()

        do {
            try await service.start()

            // Wait for health check with timeout
            let timeout: TimeInterval = (service is EmbedderService || service is LlamaService) ? 120 : 30
            let deadline = Date().addingTimeInterval(timeout)
            while Date() < deadline {
                if await service.healthCheck() {
                    service.state = .running
                    notifyStateChanged()
                    return
                }
                try? await Task.sleep(for: .seconds(1))
            }

            // Timeout waiting for health
            service.state = .errored
            notifyStateChanged()
            LogManager.shared.append(service: service.name, text: "Health check timeout")
        } catch {
            service.state = .errored
            notifyStateChanged()
            LogManager.shared.append(service: service.name, text: "Start failed: \(error)")
        }
    }

    private func runMigrations() async {
        let runner = MigrationRunner()
        do {
            try await runner.run()
            LogManager.shared.append(service: "alembic", text: "Migrations complete")
        } catch {
            LogManager.shared.append(service: "alembic", text: "Migration failed: \(error)")
        }
    }

    func notifyStateChanged() {
        objectWillChange.send()
        NotificationCenter.default.post(name: .servicesStateChanged, object: nil)
    }

    // MARK: - Environment

    /// Build the full environment dict for Python services.
    func pythonEnvironment() -> [String: String] {
        let settings = AppSettings.shared
        let bundle = Bundle.main.resourceURL!

        return [
            "DATABASE_URL": "postgresql+asyncpg://lka@localhost:\(settings.postgresPort)/lka",
            "REDIS_URL": "redis://localhost:\(settings.redisPort)/0",
            "STORAGE_BACKEND": "filesystem",
            "STORAGE_PATH": settings.originalsDir.path,
            "EMBEDDER_URL": "http://localhost:\(settings.embedderPort)",
            "TIKA_URL": "",
            "SECRET_KEY": settings.secretKey,
            "LOG_LEVEL": settings.logLevel,
            "STATIC_DIR": bundle.appendingPathComponent("frontend-dist").path,
            "API_HOST": (settings.allowRemoteWeb || settings.allowRemoteMCP) ? "0.0.0.0" : "127.0.0.1",
            "API_PORT": String(settings.apiPort),
            "PATH": [
                bundle.appendingPathComponent("venv/bin").path,
                bundle.appendingPathComponent("tesseract/bin").path,
                bundle.appendingPathComponent("poppler/bin").path,
                "/usr/bin",
                "/bin",
            ].joined(separator: ":"),
            "TESSDATA_PREFIX": bundle.appendingPathComponent("tesseract/share/tessdata").path,
            "LLAMA_SERVER_URL": "http://localhost:\(settings.llamaPort)",
            "LLM_MODEL_ID": settings.llmModelId,
            "MODELS_DIR": settings.modelsDir.path,
        ]
    }
}

import Foundation

/// Persistent settings stored in ~/Library/Application Support/Harbor Clerk/config.json
///
/// Thread-safe: all access to the internal `data` dictionary is protected by `NSLock`.
final class AppSettings: @unchecked Sendable {
    static let shared = AppSettings()

    private(set) var configURL: URL
    private var data: [String: Any]
    private let lock = NSLock()

    var postgresPort: Int {
        get { lock.withLock { data["postgres_port"] as? Int ?? 5433 } }
        set { lock.withLock { data["postgres_port"] = newValue }; save() }
    }

    var tikaPort: Int {
        get { lock.withLock { data["tika_port"] as? Int ?? 9998 } }
        set { lock.withLock { data["tika_port"] = newValue }; save() }
    }

    var apiPort: Int {
        get { lock.withLock { data["api_port"] as? Int ?? 8100 } }
        set { lock.withLock { data["api_port"] = newValue }; save() }
    }

    var embedderPort: Int {
        get { lock.withLock { data["embedder_port"] as? Int ?? 8101 } }
        set { lock.withLock { data["embedder_port"] = newValue }; save() }
    }

    var workerPreset: String {
        get { lock.withLock { data["worker_preset"] as? String ?? "balanced" } }
        set { lock.withLock { data["worker_preset"] = newValue }; save() }
    }

    var secretKey: String {
        get {
            let existing: String? = lock.withLock { data["secret_key"] as? String }
            if let existing, !existing.isEmpty { return existing }
            // Generate on first access
            let key = generateSecretKey()
            lock.withLock { data["secret_key"] = key }
            save()
            return key
        }
        set { lock.withLock { data["secret_key"] = newValue }; save() }
    }

    var logLevel: String {
        get { lock.withLock { data["log_level"] as? String ?? "INFO" } }
        set { lock.withLock { data["log_level"] = newValue }; save() }
    }

    var allowRemoteWeb: Bool {
        get { lock.withLock { data["allow_remote_web"] as? Bool ?? false } }
        set { lock.withLock { data["allow_remote_web"] = newValue }; save() }
    }

    var allowRemoteMCP: Bool {
        get { lock.withLock { data["allow_remote_mcp"] as? Bool ?? false } }
        set { lock.withLock { data["allow_remote_mcp"] = newValue }; save() }
    }

    var llamaPort: Int {
        get { lock.withLock { data["llama_port"] as? Int ?? 8102 } }
        set { lock.withLock { data["llama_port"] = newValue }; save() }
    }

    var llmModelId: String {
        get { lock.withLock { data["llm_model_id"] as? String ?? "" } }
        set { lock.withLock { data["llm_model_id"] = newValue }; save() }
    }

    // MARK: - Derived paths

    static let dataDir: URL = {
        let appSupport = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first!
        return appSupport.appendingPathComponent("Harbor Clerk")
    }()

    var postgresDataDir: URL { Self.dataDir.appendingPathComponent("postgres-data") }
    var originalsDir: URL { Self.dataDir.appendingPathComponent("originals") }
    var logsDir: URL { Self.dataDir.appendingPathComponent("logs") }
    var modelsDir: URL { Self.dataDir.appendingPathComponent("models") }

    /// Resolved path to the active model GGUF file, or empty string if none.
    var activeModelPath: String {
        // Read directly from data under lock to avoid re-entrant lock via llmModelId
        let modelId: String = lock.withLock { data["llm_model_id"] as? String ?? "" }
        guard !modelId.isEmpty else { return "" }
        // Map model IDs to filenames — mirrors the Python registry
        let filenames: [String: String] = [
            "qwen3-8b": "Qwen3-8B-Q4_K_M.gguf",
            "qwen3-4b": "Qwen3-4B-Q4_K_M.gguf",
            "llama3.2-3b": "Llama-3.2-3B-Instruct-Q4_K_M.gguf",
            "mistral-7b": "Mistral-7B-Instruct-v0.3-Q4_K_M.gguf",
            "deepseek-r1-8b": "DeepSeek-R1-Distill-Qwen-8B-Q4_K_M.gguf",
        ]
        guard let filename = filenames[modelId] else { return "" }
        return modelsDir.appendingPathComponent(filename).path
    }

    // MARK: - Init

    private init() {
        let dir = Self.dataDir
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)

        configURL = dir.appendingPathComponent("config.json")

        if let jsonData = try? Data(contentsOf: configURL),
           let json = try? JSONSerialization.jsonObject(with: jsonData) as? [String: Any] {
            data = json
        } else {
            data = [:]
        }
    }

    /// Testable initializer that uses a custom config file path.
    init(configURL: URL) {
        self.configURL = configURL

        if let jsonData = try? Data(contentsOf: configURL),
           let json = try? JSONSerialization.jsonObject(with: jsonData) as? [String: Any] {
            data = json
        } else {
            data = [:]
        }
    }

    private func save() {
        let jsonData = lock.withLock {
            try? JSONSerialization.data(withJSONObject: data, options: .prettyPrinted)
        }
        if let jsonData {
            try? jsonData.write(to: configURL)
        }
    }

    /// Re-read config.json from disk (e.g. after Python updated it).
    func reload() {
        if let jsonData = try? Data(contentsOf: configURL),
           let json = try? JSONSerialization.jsonObject(with: jsonData) as? [String: Any] {
            lock.withLock { data = json }
        }
    }

    private func generateSecretKey() -> String {
        var bytes = [UInt8](repeating: 0, count: 32)
        _ = SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes)
        return bytes.map { String(format: "%02x", $0) }.joined()
    }
}

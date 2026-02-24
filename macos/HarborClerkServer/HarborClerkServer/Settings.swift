import Foundation

/// Persistent settings stored in ~/Library/Application Support/Harbor Clerk/config.json
final class AppSettings {
    static let shared = AppSettings()

    private let configURL: URL
    private var data: [String: Any]

    var postgresPort: Int {
        get { data["postgres_port"] as? Int ?? 5433 }
        set { data["postgres_port"] = newValue; save() }
    }

    var tikaPort: Int {
        get { data["tika_port"] as? Int ?? 9998 }
        set { data["tika_port"] = newValue; save() }
    }

    var apiPort: Int {
        get { data["api_port"] as? Int ?? 8100 }
        set { data["api_port"] = newValue; save() }
    }

    var embedderPort: Int {
        get { data["embedder_port"] as? Int ?? 8101 }
        set { data["embedder_port"] = newValue; save() }
    }

    var workerPreset: String {
        get { data["worker_preset"] as? String ?? "balanced" }
        set { data["worker_preset"] = newValue; save() }
    }

    var secretKey: String {
        get {
            if let key = data["secret_key"] as? String, !key.isEmpty {
                return key
            }
            // Generate on first access
            let key = generateSecretKey()
            data["secret_key"] = key
            save()
            return key
        }
        set { data["secret_key"] = newValue; save() }
    }

    var logLevel: String {
        get { data["log_level"] as? String ?? "INFO" }
        set { data["log_level"] = newValue; save() }
    }

    var allowRemoteWeb: Bool {
        get { data["allow_remote_web"] as? Bool ?? false }
        set { data["allow_remote_web"] = newValue; save() }
    }

    var allowRemoteMCP: Bool {
        get { data["allow_remote_mcp"] as? Bool ?? false }
        set { data["allow_remote_mcp"] = newValue; save() }
    }

    var llamaPort: Int {
        get { data["llama_port"] as? Int ?? 8102 }
        set { data["llama_port"] = newValue; save() }
    }

    var llmModelId: String {
        get { data["llm_model_id"] as? String ?? "" }
        set { data["llm_model_id"] = newValue; save() }
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
        guard !llmModelId.isEmpty else { return "" }
        // Map model IDs to filenames — mirrors the Python registry
        let filenames: [String: String] = [
            "qwen3-8b": "Qwen3-8B-Q4_K_M.gguf",
            "qwen3-4b": "Qwen3-4B-Q4_K_M.gguf",
            "llama3.2-3b": "Llama-3.2-3B-Instruct-Q4_K_M.gguf",
            "mistral-7b": "Mistral-7B-Instruct-v0.3-Q4_K_M.gguf",
            "deepseek-r1-8b": "DeepSeek-R1-Distill-Qwen-8B-Q4_K_M.gguf",
        ]
        guard let filename = filenames[llmModelId] else { return "" }
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
        if let jsonData = try? JSONSerialization.data(withJSONObject: data, options: .prettyPrinted) {
            try? jsonData.write(to: configURL)
        }
    }

    private func generateSecretKey() -> String {
        var bytes = [UInt8](repeating: 0, count: 32)
        _ = SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes)
        return bytes.map { String(format: "%02x", $0) }.joined()
    }
}

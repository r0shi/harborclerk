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

    var rerankerPort: Int {
        get { lock.withLock { data["reranker_port"] as? Int ?? 8201 } }
        set { lock.withLock { data["reranker_port"] = newValue }; save() }
    }

    var rerankerEnabled: Bool {
        get { lock.withLock { data["reranker_enabled"] as? Bool ?? true } }
        set { lock.withLock { data["reranker_enabled"] = newValue }; save() }
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

    var enableCliAccess: Bool {
        get { lock.withLock { data["enable_cli_access"] as? Bool ?? false } }
        set { lock.withLock { data["enable_cli_access"] = newValue }; save() }
    }

    var llamaPort: Int {
        get { lock.withLock { data["llama_port"] as? Int ?? 8102 } }
        set { lock.withLock { data["llama_port"] = newValue }; save() }
    }

    var llmModelId: String {
        get { lock.withLock { data["llm_model_id"] as? String ?? "" } }
        set { lock.withLock { data["llm_model_id"] = newValue }; save() }
    }

    var llmYarnEnabled: Bool {
        get { lock.withLock { data["llm_yarn_enabled"] as? Bool ?? false } }
        set { lock.withLock { data["llm_yarn_enabled"] = newValue }; save() }
    }

    /// True when Python has signaled that llama-server needs a hard restart.
    var llmRestartRequested: Bool {
        lock.withLock { data["llm_restart"] as? Bool ?? false }
    }

    /// Clear the restart flag from config.json so it doesn't fire again.
    func clearLlmRestart() {
        lock.withLock { _ = data.removeValue(forKey: "llm_restart") }
        save()
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
            "deepseek-r1-0528-8b": "DeepSeek-R1-0528-Qwen3-8B-Q4_K_M.gguf",
            "gpt-oss-20b": "gpt-oss-20b-Q4_K_M.gguf",
            "qwen36-35b-a3b": "Qwen3.6-35B-A3B-UD-Q4_K_M.gguf",
            "gemma4-26b-a4b": "google_gemma-4-26B-A4B-it-Q4_K_M.gguf",
        ]
        guard let filename = filenames[modelId] else { return "" }
        return modelsDir.appendingPathComponent(filename).path
    }

    /// Native context window (tokens) for the active model. Mirrors Python registry.
    var activeModelContextWindow: Int {
        let modelId: String = lock.withLock { data["llm_model_id"] as? String ?? "" }
        let contextWindows: [String: Int] = [
            "qwen3-8b": 32768,
            "qwen3-4b": 32768,
            "deepseek-r1-0528-8b": 32768,
            "gpt-oss-20b": 128000,
            "qwen36-35b-a3b": 262144,
            "gemma4-26b-a4b": 128000,
        ]
        return contextWindows[modelId] ?? 32768
    }

    /// YaRN configuration for models that support context extension.
    struct YarnConfig {
        let extendedContext: Int
        let ropeScale: Double
        let originalContext: Int
        let attnFactor: Double?
    }

    /// YaRN parameters for models that support it. nil = not applicable.
    var activeModelYarn: YarnConfig? {
        let modelId: String = lock.withLock { data["llm_model_id"] as? String ?? "" }
        let configs: [String: YarnConfig] = [
            "qwen3-8b": YarnConfig(extendedContext: 131072, ropeScale: 4.0, originalContext: 32768, attnFactor: nil),
            "qwen3-4b": YarnConfig(extendedContext: 131072, ropeScale: 4.0, originalContext: 32768, attnFactor: nil),
            "deepseek-r1-0528-8b": YarnConfig(extendedContext: 131072, ropeScale: 4.0, originalContext: 32768, attnFactor: 0.8782),
        ]
        return configs[modelId]
    }

    /// llama-server `-np` slot count for the active model. Mirrors
    /// `ModelInfo.parallel_slots` in `src/harbor_clerk/llm/models.py`.
    /// Defaults to 1 when the active model id isn't recognized (e.g.
    /// during the brief window of a model switch when config.json
    /// references a model the Swift mirror doesn't know yet) — safest
    /// fallback since `-np 1` always fits.
    var activeModelParallelSlots: Int {
        let modelId: String = lock.withLock { data["llm_model_id"] as? String ?? "" }
        let slots: [String: Int] = [
            "qwen3-8b": 2,             // mid (32K context)
            "qwen3-4b": 4,             // small
            "deepseek-r1-0528-8b": 2,  // mid (32K context)
            "gpt-oss-20b": 1,          // heavy — MoE active params are small but 128K context → KV too big for 2 slots
            "gemma4-26b-a4b": 1,       // heavy
            "qwen36-35b-a3b": 1,       // heavy
        ]
        return slots[modelId] ?? 1
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
        guard let jsonData else { return }
        // Atomic write — write to a sibling temp file and rename. Without
        // this, Python's refresh_llm_settings() can briefly observe a
        // truncated file mid-write, fail to parse, and silently keep its
        // stale in-memory value for one more poll cycle. Mirrors the
        // temp+rename pattern in Python's sync_native_config().
        let dir = configURL.deletingLastPathComponent()
        let tmpURL = dir.appendingPathComponent(".\(configURL.lastPathComponent).tmp")
        do {
            try jsonData.write(to: tmpURL)
            _ = try FileManager.default.replaceItemAt(configURL, withItemAt: tmpURL)
        } catch {
            try? FileManager.default.removeItem(at: tmpURL)
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

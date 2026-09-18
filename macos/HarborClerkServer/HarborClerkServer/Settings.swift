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

    var gatewayPort: Int {
        get { lock.withLock { data["gateway_port"] as? Int ?? GatewayConfig.defaultPort } }
        set { lock.withLock { data["gateway_port"] = newValue }; save() }
    }

    var gatewayHostname: String {
        get { lock.withLock { data["gateway_hostname"] as? String ?? GatewayConfig.defaultHostname } }
        set { lock.withLock { data["gateway_hostname"] = GatewayConfig.normalizedHostname(newValue) }; save() }
    }

    var gatewayBindAddresses: [String] {
        get { lock.withLock { data["gateway_bind_addresses"] as? [String] ?? GatewayConfig.defaultBindAddresses } }
        set { lock.withLock { data["gateway_bind_addresses"] = GatewayConfig.normalizedBindAddresses(newValue) }; save() }
    }

    var gatewayCertificateMode: GatewayCertificateMode {
        get {
            let raw = lock.withLock { data["gateway_certificate_mode"] as? String ?? GatewayCertificateMode.internal.rawValue }
            return GatewayCertificateMode(rawValue: raw) ?? .internal
        }
        set { lock.withLock { data["gateway_certificate_mode"] = newValue.rawValue }; save() }
    }

    var gatewayCertificatePath: String {
        get { lock.withLock { data["gateway_certificate_path"] as? String ?? "" } }
        set { lock.withLock { data["gateway_certificate_path"] = newValue.trimmingCharacters(in: .whitespacesAndNewlines) }; save() }
    }

    var gatewayPrivateKeyPath: String {
        get { lock.withLock { data["gateway_private_key_path"] as? String ?? "" } }
        set { lock.withLock { data["gateway_private_key_path"] = newValue.trimmingCharacters(in: .whitespacesAndNewlines) }; save() }
    }

    var gatewayExposesFullApp: Bool {
        GatewayConfig.exposesFullApp(bindAddresses: gatewayBindAddresses)
    }

    var localMCPBaseURL: String {
        GatewayConfig.localBaseURL(hostname: gatewayHostname, gatewayPort: gatewayPort)
    }

    var embedderPort: Int {
        get { lock.withLock { data["embedder_port"] as? Int ?? 8101 } }
        set { lock.withLock { data["embedder_port"] = newValue }; save() }
    }

    /// e5 rollback switch; default must match EMBED_NEEDS_PREFIX in embedder/src/embedder/app.py.
    var embedNeedsPrefix: Bool {
        get { lock.withLock { data["embed_needs_prefix"] as? Bool ?? false } }
        set { lock.withLock { data["embed_needs_prefix"] = newValue }; save() }
    }

    var rerankerPort: Int {
        get { lock.withLock { data["reranker_port"] as? Int ?? 8201 } }
        set { lock.withLock { data["reranker_port"] = newValue }; save() }
    }

    var rerankerEnabled: Bool {
        get { lock.withLock { data["reranker_enabled"] as? Bool ?? true } }
        set { lock.withLock { data["reranker_enabled"] = newValue }; save() }
    }

    /// Release the GPU allocator cache once the unused pool exceeds this many
    /// MB; 0 disables. Surfaced here because `pythonEnvironment()` builds a
    /// closed dict — a knob the Python side reads is unreachable on macOS
    /// unless it is passed explicitly, and macOS is the only platform where the
    /// MPS path runs. Default matches `embedder.gpu_cache.CACHE_HIGH_WATER_MB`.
    var gpuCacheHighWaterMB: Int {
        get { lock.withLock { data["gpu_cache_high_water_mb"] as? Int ?? 4096 } }
        set { lock.withLock { data["gpu_cache_high_water_mb"] = newValue }; save() }
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
            "qwen35-9b": "Qwen3.5-9B-Q4_K_M.gguf",
            "qwen35-4b": "Qwen3.5-4B-Q4_K_M.gguf",
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
            "qwen35-9b": 262144,
            "qwen35-4b": 262144,
            "gpt-oss-20b": 128000,
            "qwen36-35b-a3b": 262144,
            "gemma4-26b-a4b": 262144,  // the GGUF's; 128K is the E2B/E4B figure (#548)
        ]
        return contextWindows[modelId] ?? 32768
    }

    /// KV cache the active model needs per token of context (bytes, f16, the
    /// layers whose cache grows with context) and the part that does not grow
    /// (sliding windows, linear-attention state). Mirrors
    /// `ModelInfo.kv_bytes_per_token` / `kv_fixed_bytes` in
    /// `src/harbor_clerk/llm/models.py`, where each value's derivation from the
    /// GGUF header is recorded. An unknown id gets the largest per-token cost
    /// in the table, so the clamp errs towards a smaller context.
    var activeModelKvBytesPerToken: Int {
        let modelId: String = lock.withLock { data["llm_model_id"] as? String ?? "" }
        return Self.kvBytesPerToken[modelId] ?? Self.kvBytesPerToken.values.max() ?? 0
    }

    var activeModelKvFixedBytes: Int {
        let modelId: String = lock.withLock { data["llm_model_id"] as? String ?? "" }
        return Self.kvFixedBytes[modelId] ?? 0
    }

    static let kvBytesPerToken: [String: Int] = [
        "qwen3-8b": 147_456,
        "qwen3-4b": 147_456,
        "qwen35-9b": 32_768,
        "qwen35-4b": 32_768,
        "gpt-oss-20b": 24_576,
        "qwen36-35b-a3b": 20_480,
        "gemma4-26b-a4b": 20_480,
    ]

    static let kvFixedBytes: [String: Int] = [
        "qwen3-8b": 0,
        "qwen3-4b": 0,
        "qwen35-9b": 100_000_000,
        "qwen35-4b": 100_000_000,
        "gpt-oss-20b": 3_145_728,
        "qwen36-35b-a3b": 300_000_000,
        "gemma4-26b-a4b": 209_715_200,
    ]

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
            "qwen35-9b": 2,            // 262K window: 131K per slot
            "qwen35-4b": 2,            // 262K window: 131K per slot
            "qwen3-4b": 1,             // small but -np 1 — 8K/slot under -np 4 too tight for tools schema + ambiguous results (v3 sweep, models.py)
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

/// The memory a model needs against the memory a Mac has. Mirrors
/// `memory_bytes` / `max_context` in `src/harbor_clerk/llm/models.py`;
/// `AppSettingsTests` holds the constants and the rounding to the Python
/// values. This is the check that protects the host: unified memory lets
/// llama-server allocate more than the machine has, and the machine then
/// swaps until the kernel watchdog panics (the mini did, 2026-09-17).
enum MemoryBudget {
    /// Compute buffers, Metal scratch, the process itself.
    static let runtimeOverheadBytes = 1_000_000_000
    /// The OS, the embedder and reranker, Postgres, the Tika JVM and the API.
    static let hostHeadroomBytes = 6_000_000_000

    /// The largest context, up to `requested`, that fits a Mac with `ramBytes`
    /// of physical memory; 0 when the weights alone do not fit. A multiple of
    /// 1024, and never below 4096 unless 0: a smaller context is not worth
    /// running.
    /// The RoPE arguments for a YaRN launch, or none: YaRN stretches RoPE by
    /// `ropeScale` to reach `extendedContext`, at a quality cost that keeps it
    /// off by default. Once the memory clamp has brought the context down to
    /// the native window or below, the stretch buys nothing and is not applied.
    static func yarnArguments(contextWindow: Int, yarn: AppSettings.YarnConfig?) -> [String] {
        guard let yarn, contextWindow > yarn.originalContext else { return [] }
        var args = ["--rope-scaling", "yarn", "--rope-scale", String(yarn.ropeScale), "--yarn-orig-ctx", String(yarn.originalContext)]
        if let attn = yarn.attnFactor {
            args += ["--yarn-attn-factor", String(attn)]
        }
        return args
    }

    static func maxContext(modelBytes: Int, kvBytesPerToken: Int, kvFixedBytes: Int, requested: Int, ramBytes: Int) -> Int {
        let spare = ramBytes - hostHeadroomBytes - runtimeOverheadBytes - modelBytes - kvFixedBytes
        if spare <= 0 { return 0 }
        var tokens = kvBytesPerToken == 0 ? requested : min(requested, spare / kvBytesPerToken)
        tokens -= tokens % 1024
        return tokens >= 4096 ? tokens : 0
    }
}

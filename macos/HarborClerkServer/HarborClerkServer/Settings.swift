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

    var redisPort: Int {
        get { data["redis_port"] as? Int ?? 6380 }
        set { data["redis_port"] = newValue; save() }
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

    // MARK: - Derived paths

    static let dataDir: URL = {
        let appSupport = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first!
        return appSupport.appendingPathComponent("Harbor Clerk")
    }()

    var postgresDataDir: URL { Self.dataDir.appendingPathComponent("postgres-data") }
    var redisDataDir: URL { Self.dataDir.appendingPathComponent("redis-data") }
    var originalsDir: URL { Self.dataDir.appendingPathComponent("originals") }
    var logsDir: URL { Self.dataDir.appendingPathComponent("logs") }

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

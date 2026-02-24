import XCTest
@testable import HarborClerkServer

final class AppSettingsTests: XCTestCase {

    private var tempDir: URL!
    private var configURL: URL!

    override func setUp() {
        super.setUp()
        tempDir = FileManager.default.temporaryDirectory
            .appendingPathComponent("AppSettingsTests-\(UUID().uuidString)")
        try? FileManager.default.createDirectory(at: tempDir, withIntermediateDirectories: true)
        configURL = tempDir.appendingPathComponent("config.json")
    }

    override func tearDown() {
        try? FileManager.default.removeItem(at: tempDir)
        super.tearDown()
    }

    // MARK: - Defaults

    func testDefaultsWhenNoConfigFile() {
        let settings = AppSettings(configURL: configURL)
        XCTAssertEqual(settings.postgresPort, 5433)
        XCTAssertEqual(settings.tikaPort, 9998)
        XCTAssertEqual(settings.apiPort, 8100)
        XCTAssertEqual(settings.embedderPort, 8101)
        XCTAssertEqual(settings.llamaPort, 8102)
        XCTAssertEqual(settings.workerPreset, "balanced")
        XCTAssertEqual(settings.logLevel, "INFO")
        XCTAssertEqual(settings.allowRemoteWeb, false)
        XCTAssertEqual(settings.allowRemoteMCP, false)
        XCTAssertEqual(settings.llmModelId, "")
    }

    // MARK: - Load from file

    func testLoadFromExistingConfig() throws {
        let json: [String: Any] = [
            "postgres_port": 5555,
            "tika_port": 9000,
            "worker_preset": "fast",
            "log_level": "DEBUG",
        ]
        let data = try JSONSerialization.data(withJSONObject: json)
        try data.write(to: configURL)

        let settings = AppSettings(configURL: configURL)
        XCTAssertEqual(settings.postgresPort, 5555)
        XCTAssertEqual(settings.tikaPort, 9000)
        XCTAssertEqual(settings.workerPreset, "fast")
        XCTAssertEqual(settings.logLevel, "DEBUG")
        // Other fields keep defaults
        XCTAssertEqual(settings.apiPort, 8100)
    }

    // MARK: - Save and reload

    func testSaveAndReload() {
        let settings = AppSettings(configURL: configURL)
        settings.postgresPort = 6000
        settings.workerPreset = "quiet"

        let reloaded = AppSettings(configURL: configURL)
        XCTAssertEqual(reloaded.postgresPort, 6000)
        XCTAssertEqual(reloaded.workerPreset, "quiet")
    }

    // MARK: - Secret key

    func testSecretKeyAutoGenerates() {
        let settings = AppSettings(configURL: configURL)
        let key = settings.secretKey
        XCTAssertEqual(key.count, 64, "Secret key should be 64-char hex string (32 bytes)")
        XCTAssertTrue(key.allSatisfy { $0.isHexDigit }, "Secret key should be hex")
    }

    func testSecretKeyPersistsAcrossReloads() {
        let settings = AppSettings(configURL: configURL)
        let key1 = settings.secretKey

        let reloaded = AppSettings(configURL: configURL)
        let key2 = reloaded.secretKey
        XCTAssertEqual(key1, key2)
    }

    // MARK: - Active model path

    func testActiveModelPathKnownModels() {
        let settings = AppSettings(configURL: configURL)
        let expected: [String: String] = [
            "qwen3-8b": "Qwen3-8B-Q4_K_M.gguf",
            "qwen3-4b": "Qwen3-4B-Q4_K_M.gguf",
            "llama3.2-3b": "Llama-3.2-3B-Instruct-Q4_K_M.gguf",
            "mistral-7b": "Mistral-7B-Instruct-v0.3-Q4_K_M.gguf",
            "deepseek-r1-8b": "DeepSeek-R1-Distill-Qwen-8B-Q4_K_M.gguf",
        ]
        for (modelId, filename) in expected {
            settings.llmModelId = modelId
            XCTAssertTrue(settings.activeModelPath.hasSuffix(filename),
                "Expected path for \(modelId) to end with \(filename), got \(settings.activeModelPath)")
        }
    }

    func testActiveModelPathUnknownModel() {
        let settings = AppSettings(configURL: configURL)
        settings.llmModelId = "nonexistent-model"
        XCTAssertEqual(settings.activeModelPath, "")
    }

    func testActiveModelPathEmptyModelId() {
        let settings = AppSettings(configURL: configURL)
        settings.llmModelId = ""
        XCTAssertEqual(settings.activeModelPath, "")
    }
}

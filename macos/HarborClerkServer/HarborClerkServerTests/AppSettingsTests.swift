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

    // MARK: - CLI access

    func testEnableCliAccessDefaultsFalse() {
        let settings = AppSettings(configURL: configURL)
        XCTAssertEqual(settings.enableCliAccess, false)
    }

    func testEnableCliAccessPersistsAcrossReload() {
        let settings = AppSettings(configURL: configURL)
        settings.enableCliAccess = true

        let reloaded = AppSettings(configURL: configURL)
        XCTAssertEqual(reloaded.enableCliAccess, true)
    }

    func testEnableCliAccessToggleOffPersists() {
        let settings = AppSettings(configURL: configURL)
        settings.enableCliAccess = true
        settings.enableCliAccess = false

        let reloaded = AppSettings(configURL: configURL)
        XCTAssertEqual(reloaded.enableCliAccess, false)
    }

    func testEnableCliAccessLoadedFromExistingConfig() throws {
        let json: [String: Any] = ["enable_cli_access": true]
        let data = try JSONSerialization.data(withJSONObject: json)
        try data.write(to: configURL)

        let settings = AppSettings(configURL: configURL)
        XCTAssertEqual(settings.enableCliAccess, true)
    }

    // MARK: - Active model path

    func testActiveModelPathKnownModels() {
        let settings = AppSettings(configURL: configURL)
        let expected: [String: String] = [
            "qwen3-8b": "Qwen3-8B-Q4_K_M.gguf",
            "qwen3-4b": "Qwen3-4B-Q4_K_M.gguf",
            "gpt-oss-20b": "gpt-oss-20b-Q4_K_M.gguf",
            "qwen36-35b-a3b": "Qwen3.6-35B-A3B-UD-Q4_K_M.gguf",
            "gemma4-26b-a4b": "google_gemma-4-26B-A4B-it-Q4_K_M.gguf",
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

    // MARK: - Per-model parallel_slots (llama-server -np)

    /// All model IDs Swift knows about. Source of truth for the completeness
    /// checks: every model in this set MUST have an entry in both
    /// `activeModelPath`'s filenames dict and `activeModelParallelSlots`'s
    /// slots dict. The Python-side test enforces the same completeness
    /// against `MODELS.keys()`; this test enforces Swift's mirror stays
    /// internally consistent so a new model can't be added to one Swift
    /// dict without the other.
    private static let knownModelIds: Set<String> = [
        "qwen3-8b",
        "qwen3-4b",
        "gpt-oss-20b",
        "qwen36-35b-a3b",
        "gemma4-26b-a4b",
    ]

    /// Mirror of `tests/test_llm_models.py::test_curated_models_parallel_slots_tiered_by_size`.
    /// If this test diverges from the Python registry, llama-server will
    /// launch with the wrong -np value, either OOMing on a heavy model
    /// (slots too high) or wasting capacity on a small model (slots too
    /// low). The python-side test enforces the source-of-truth values;
    /// this one enforces the Swift mirror agrees AND that every known
    /// model has an explicit tier entry.
    func testActiveModelParallelSlotsMatchesPythonRegistry() {
        let settings = AppSettings(configURL: configURL)
        let expected: [String: Int] = [
            // Small — but exception: qwen3-4b is -np 1, see models.py
            "qwen3-4b": 1,
            // Mid (5-12 GB, ≤32K context) → 2 slots
            "qwen3-8b": 2,
            // Heavy (>15 GB OR 128K+ context) → 1 slot
            "gpt-oss-20b": 1,  // 128K context → KV cache too big for 2 slots on 18 GB
            "gemma4-26b-a4b": 1,
            "qwen36-35b-a3b": 1,
        ]
        for (modelId, slots) in expected {
            settings.llmModelId = modelId
            XCTAssertEqual(
                settings.activeModelParallelSlots,
                slots,
                "Expected \(modelId) → -np \(slots), got \(settings.activeModelParallelSlots)",
            )
        }
        // Completeness check (mirrors the Python test's belt-and-suspenders):
        // every model the Swift mirror knows about must have an explicit tier
        // entry. Without this, a future model added to Settings.activeModelPath
        // but missed in activeModelParallelSlots' slots dict would silently
        // fall back to `-np 1`, leaving the small/mid throughput gain on the
        // floor with no test failure.
        XCTAssertEqual(
            Set(expected.keys),
            Self.knownModelIds,
            "parallel_slots tier table out of sync with Settings.activeModelPath's filename map",
        )
    }

    /// Unknown model id (e.g. mid-switch when config.json names a model
    /// the Swift mirror doesn't know yet, or a typo) falls back to the
    /// always-safe `-np 1`. Without this fallback, an unrecognized id
    /// risks an OOM if a hypothetical future entry got a too-large slot
    /// count via some other code path — defense in depth.
    func testActiveModelParallelSlotsUnknownModelDefaultsToOne() {
        let settings = AppSettings(configURL: configURL)
        settings.llmModelId = "nonexistent-future-model"
        XCTAssertEqual(settings.activeModelParallelSlots, 1)
    }

    func testActiveModelParallelSlotsEmptyModelIdDefaultsToOne() {
        let settings = AppSettings(configURL: configURL)
        settings.llmModelId = ""
        XCTAssertEqual(settings.activeModelParallelSlots, 1)
    }
}

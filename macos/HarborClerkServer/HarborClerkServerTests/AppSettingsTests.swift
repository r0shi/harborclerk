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
        XCTAssertEqual(settings.gatewayPort, 8443)
        XCTAssertEqual(settings.gatewayHostname, "localhost")
        XCTAssertEqual(settings.gatewayBindAddresses, ["127.0.0.1", "::1"])
        XCTAssertEqual(settings.gatewayCertificateMode, .internal)
        XCTAssertEqual(settings.gatewayCertificatePath, "")
        XCTAssertEqual(settings.gatewayPrivateKeyPath, "")
        XCTAssertTrue(settings.gatewayExposesFullApp)
        XCTAssertEqual(settings.localMCPBaseURL, "https://localhost:8443")
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
        XCTAssertEqual(settings.gatewayPort, 8443)
        XCTAssertEqual(settings.gatewayHostname, "localhost")
    }

    // MARK: - Save and reload

    func testSaveAndReload() {
        let settings = AppSettings(configURL: configURL)
        settings.postgresPort = 6000
        settings.gatewayPort = 9443
        settings.gatewayHostname = "harbor.tailnet.ts.net"
        settings.gatewayBindAddresses = ["100.80.1.2"]
        settings.gatewayCertificateMode = .custom
        settings.gatewayCertificatePath = "/tmp/harbor.pem"
        settings.gatewayPrivateKeyPath = "/tmp/harbor-key.pem"
        settings.workerPreset = "quiet"

        let reloaded = AppSettings(configURL: configURL)
        XCTAssertEqual(reloaded.postgresPort, 6000)
        XCTAssertEqual(reloaded.gatewayPort, 9443)
        XCTAssertEqual(reloaded.gatewayHostname, "harbor.tailnet.ts.net")
        XCTAssertEqual(reloaded.gatewayBindAddresses, ["100.80.1.2"])
        XCTAssertEqual(reloaded.gatewayCertificateMode, .custom)
        XCTAssertEqual(reloaded.gatewayCertificatePath, "/tmp/harbor.pem")
        XCTAssertEqual(reloaded.gatewayPrivateKeyPath, "/tmp/harbor-key.pem")
        XCTAssertFalse(reloaded.gatewayExposesFullApp)
        XCTAssertEqual(reloaded.localMCPBaseURL, "https://harbor.tailnet.ts.net:9443")
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
            "gemma4-12b": "gemma-4-12b-it-Q4_K_M.gguf",
            "gemma4-26b-a4b": "google_gemma-4-26B-A4B-it-Q4_K_M.gguf",
            "qwen35-9b": "Qwen3.5-9B-Q4_K_M.gguf",
            "qwen35-4b": "Qwen3.5-4B-Q4_K_M.gguf",
        ]
        for (modelId, filename) in expected {
            settings.llmModelId = modelId
            XCTAssertTrue(settings.activeModelPath.hasSuffix(filename),
                "Expected path for \(modelId) to end with \(filename), got \(settings.activeModelPath)")
        }
        // A model added to the other tables but not to the filename map launches as "Model file not found".
        XCTAssertEqual(Set(expected.keys), Self.knownModelIds, "filename map out of sync with the model set")
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

    // MARK: - Memory budget (#556)

    /// Mirror of `tests/test_llm_models.py`: the constants and the rounding
    /// must agree with `src/harbor_clerk/llm/models.py`, or the launcher clamps
    /// to a different context than the API advertises.
    func testMemoryBudgetConstantsMatchPythonRegistry() {
        XCTAssertEqual(MemoryBudget.runtimeOverheadBytes, 1_000_000_000)
        XCTAssertEqual(MemoryBudget.hostHeadroomBytes, 6_000_000_000)
        XCTAssertEqual(MemoryBudget.freeMemoryDivisor, 10)
        XCTAssertEqual(MemoryBudget.tightFitContext, 16_384)
        XCTAssertEqual(MemoryBudget.ctxCheckpoints, 3)
        // fixed: the model's fixed KV alone. held: that plus the three context checkpoints the launcher
        // lets llama-server keep of it, which is what the context and the cache are sized from.
        let expected: [String: (perToken: Int, fixed: Int, held: Int, context: Int)] = [
            "qwen3-8b": (147_456, 0, 0, 32768),
            "qwen3-4b": (147_456, 0, 0, 32768),
            "qwen35-9b": (32_768, 52_690_944, 210_763_776, 262144),
            "qwen35-4b": (32_768, 52_690_944, 210_763_776, 262144),
            "gpt-oss-20b": (24_576, 18_874_368, 75_497_472, 128000),
            "qwen36-35b-a3b": (20_480, 65_863_680, 263_454_720, 262144),
            "gemma4-12b": (16_384, 503_316_480, 2_013_265_920, 262144),
            "gemma4-26b-a4b": (20_480, 314_572_800, 1_258_291_200, 262144),  // #548
        ]
        let settings = AppSettings(configURL: configURL)
        for (modelId, e) in expected {
            settings.llmModelId = modelId
            XCTAssertEqual(settings.activeModelKvBytesPerToken, e.perToken, modelId)
            XCTAssertEqual(settings.activeModelKvFixedBytes, e.fixed, modelId)
            XCTAssertEqual(settings.activeModelFixedBytes, e.held, modelId)
            XCTAssertEqual(settings.activeModelContextWindow, e.context, modelId)
        }
        XCTAssertEqual(Set(expected.keys), Self.knownModelIds, "memory table out of sync with the model set")
        // Checkpoints are kept per slot. Every model runs one today, so nothing above would notice the slots
        // being ignored: a second slot holds a second set.
        XCTAssertEqual(AppSettings.fixedBytes(modelId: "gemma4-26b-a4b", slots: 2), 314_572_800 + 2 * 3 * 314_572_800)
        XCTAssertEqual(AppSettings.fixedBytes(modelId: "qwen3-8b", slots: 2), 0)
        XCTAssertEqual(AppSettings.fixedBytes(modelId: "a-model-swift-does-not-know", slots: 1), 0)
        settings.llmModelId = "a-model-swift-does-not-know"
        XCTAssertEqual(settings.activeModelKvBytesPerToken, 147_456, "unknown ids get the largest cost, so the clamp errs small")
    }

    /// YaRN is a RoPE stretch to reach the extended window. Clamped to the
    /// native window or below, it would cost quality for no context.
    func testYarnArgumentsAreDroppedWhenTheClampLeavesNothingToStretchInto() {
        let yarn = AppSettings.YarnConfig(extendedContext: 131072, ropeScale: 4.0, originalContext: 32768, attnFactor: nil)
        func args(_ context: Int, _ slots: Int = 1, _ y: AppSettings.YarnConfig? = yarn) -> [String] {
            MemoryBudget.yarnArguments(contextWindow: context, slots: slots, yarn: y)
        }
        XCTAssertEqual(args(131072), ["--rope-scaling", "yarn", "--rope-scale", "4.0", "--yarn-orig-ctx", "32768"])
        XCTAssertEqual(args(34816).count, 6, "2K over native is still over native")
        XCTAssertEqual(args(32768), [], "exactly native: nothing to stretch into")
        XCTAssertEqual(args(26624), [], "below native on a 12 GB Mac")
        XCTAssertEqual(args(131072, 1, nil), [], "YaRN off, or a model without it")
        // A request sees -c / -np. A model given two slots and clamped to 60K has 30K per request: native.
        XCTAssertEqual(args(131072, 2).count, 6, "65K per request")
        XCTAssertEqual(args(61440, 2), [], "30K per request is inside the native window")
        XCTAssertEqual(args(65536, 2), [], "exactly native per request")
        let scaled = AppSettings.YarnConfig(extendedContext: 131072, ropeScale: 4.0, originalContext: 32768, attnFactor: 1.2)
        XCTAssertEqual(args(65536, 1, scaled).suffix(2), ["--yarn-attn-factor", "1.2"])
    }

    /// Same cases as `test_max_context_is_what_fits...` in Python.
    func testMaxContextIsWhatFitsAndZeroWhenTheWeightsAloneDoNot() {
        let m = (bytes: 5_000_000_000, perToken: 147_456, fixed: 0, requested: 32768)
        func fit(_ ram: Int, _ model: (bytes: Int, perToken: Int, fixed: Int, requested: Int) = m) -> Int {
            MemoryBudget.maxContext(modelBytes: model.bytes, kvBytesPerToken: model.perToken, fixedBytes: model.fixed, requested: model.requested, ramBytes: ram)
        }
        XCTAssertEqual(fit(64 * 1024 * 1024 * 1024), 32768)
        // A tenth of the Mac stays free where the model allows it (#684). Same cases as Python.
        XCTAssertEqual(fit(20_000_000_000), 32768, "8 GB spare less 2 GB free is 40690 tokens: past the model's window")
        XCTAssertEqual(fit(17_000_000_000), 21504, "5 GB spare less 1.7 GB free is 22379 tokens: the margin holds")
        XCTAssertEqual(fit(16_000_000_000), 16384, "fits 26624 but only 15360 with the margin: the working context")
        XCTAssertEqual(fit(14_000_000_000), 13312, "what fits, since that is under the working context")
        XCTAssertEqual(fit(12_500_000_000), 0, "3389 tokens is not worth running")
        XCTAssertEqual(fit(11_000_000_000), 0)
        XCTAssertEqual(fit(16_000_000_000, (5_000_000_000, 0, 0, 32768)), 32768, "no per-token cost: the model's own window")
        XCTAssertEqual(fit(11_000_000_000, (5_000_000_000, 0, 0, 32768)), 0)
        XCTAssertEqual(fit(13_000_000_000, (5_000_000_000, 0, 0, 32768)), 16384, "no per-token cost, nothing left once the margin is kept")
        XCTAssertEqual(fit(16_000_000_000, (5_000_000_000, 20_480, 209_715_200, 262144)), (16_000_000_000 - 12_209_715_200 - 1_600_000_000) / 20_480 / 1024 * 1024)
        XCTAssertEqual(MemoryBudget.freeMarginBytes(16_000_000_000), 1_600_000_000)
        // The mini's own case. Sized to fill 32 GiB the 35B-A3B was given 241664 tokens and Metal refused
        // its first prompt; 98304 was measured working with 11% of the machine free.
        XCTAssertEqual(fit(34_359_738_368, (22_134_528_992, 20_480, 263_454_720, 262144)), 73_728)
    }

    /// Same cases as `test_the_prompt_cache_gets_what_the_context_leaves` in Python.
    func testThePromptCacheGetsWhatTheContextLeaves() {
        let gib = 1_073_741_824
        func cache(_ ram: Int, _ m: (bytes: Int, perToken: Int, fixed: Int), _ context: Int) -> Int {
            MemoryBudget.promptCacheMiB(modelBytes: m.bytes, kvBytesPerToken: m.perToken, fixedBytes: m.fixed, context: context, ramBytes: ram)
        }
        let qwen8 = (bytes: 5_027_783_488, perToken: 147_456, fixed: 0)
        // fixed: what `activeModelFixedBytes` gives, the three context checkpoints included.
        let gptoss = (bytes: 11_624_759_488, perToken: 24_576, fixed: 75_497_472)
        let qwen35b = (bytes: 22_134_528_992, perToken: 20_480, fixed: 263_454_720)
        let qwen9 = (bytes: 5_680_522_464, perToken: 32_768, fixed: 210_763_776)
        XCTAssertEqual(cache(32 * gib, qwen8, 32768), 8192, "room to spare: llama-server's own default, as before")
        // The free margin is not the cache's to spend. Before it an 18 GB Mac had 2353 MiB of cache here.
        XCTAssertEqual(cache(18 * gib, qwen8, 32768), 0, "510 MiB left cannot hold a 4096-token state (576 MiB): off")
        XCTAssertEqual(cache(24 * gib, qwen8, 32768), 6039, "what 32K of context and the margin leave on 24 GB")
        XCTAssertEqual(cache(16 * gib, qwen8, 22528), 0)
        XCTAssertEqual(cache(18 * gib, gptoss, 16384), 0, "a tight fit: nothing is left")
        XCTAssertEqual(cache(32 * gib, qwen35b, 73_728), 0, "the mini's 35B: clamped, so no cache")
        XCTAssertEqual(cache(36 * gib, qwen35b, 262_144), 0, "21 MiB left once the margin is kept")
        // The fixed cost counts (recurrent state and the three checkpoints kept of it).
        XCTAssertEqual(cache(18 * gib, qwen9, 137_216), 0)
        XCTAssertEqual(cache(24 * gib, qwen9, 262_144), 1632)
        // With 250 MiB left: a 4096-token state of this model is 329 MiB, 201 of it fixed, so off.
        let full = 21_481_220_832
        func ramLeaving(_ spare: Int) -> Int {
            var ram = (full + spare) * 10 / 9
            while ram - MemoryBudget.freeMarginBytes(ram) - full < spare { ram += 1 }
            return ram
        }
        XCTAssertEqual(cache(ramLeaving(250 * 1_048_576), qwen9, 262_144), 0)
        XCTAssertEqual(cache(ramLeaving(400 * 1_048_576), qwen9, 262_144), 400)
        XCTAssertEqual(cache(0, qwen8, 32768), 0, "memory unknown")
        XCTAssertEqual(MemoryBudget.promptCacheMaxBytes, 8 * gib)
        XCTAssertEqual(MemoryBudget.promptCacheMinTokens, 4096)
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
        "qwen35-9b",
        "qwen35-4b",
        "gpt-oss-20b",
        "qwen36-35b-a3b",
        "gemma4-12b",
        "gemma4-26b-a4b",
    ]

    /// Mirror of `tests/test_llm_models.py::test_every_curated_model_runs_one_slot`.
    /// A slot divides -c: if Swift launches with more slots than the
    /// registry states, every prompt is budgeted for more context than its
    /// request has. The Python side holds the values and compares this
    /// table with them; this holds that every known model has an entry.
    func testActiveModelParallelSlotsMatchesPythonRegistry() {
        let settings = AppSettings(configURL: configURL)
        let expected: [String: Int] = [
            // One slot everywhere: a slot divides -c, and a request is worth the whole window
            "qwen3-4b": 1,
            "qwen3-8b": 1,
            "qwen35-9b": 1,
            "qwen35-4b": 1,
            "gpt-oss-20b": 1,
            "gemma4-12b": 1,
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
        // fall back to `-np 1` whatever the registry says.
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

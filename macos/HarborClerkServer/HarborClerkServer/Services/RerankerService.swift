import Foundation

final class RerankerService: PythonService {
    init() {
        super.init(name: "Reranker")
    }

    override var executableName: String { "harbor-clerk-reranker" }

    override var extraEnvironment: [String: String] {
        let modelPath = Bundle.main.resourceURL!
            .appendingPathComponent("model/bge-reranker-v2-m3").path
        return [
            // Inference-time knobs the Python side reads from the environment.
            // `pythonEnvironment()` builds a closed dict and does not inherit
            // the process environment, so anything not listed here is
            // unreachable on this platform — and macOS is the only place the
            // MPS allocator code does anything at all.
            "RERANKER_MODEL": modelPath,
            "GPU_CACHE_HIGH_WATER_MB": String(AppSettings.shared.gpuCacheHighWaterMB),
            "HOST": "127.0.0.1",
            "PORT": String(AppSettings.shared.rerankerPort),
        ]
    }

    override func healthCheck() async -> Bool {
        let port = AppSettings.shared.rerankerPort
        guard let url = URL(string: "http://127.0.0.1:\(port)/health") else { return false }
        return await httpProbeOK(url)
    }
}

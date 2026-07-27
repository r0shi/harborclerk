import Foundation

final class EmbedderService: PythonService {
    init() {
        super.init(name: "Embedder")
    }

    override var executableName: String { "harbor-clerk-embedder" }

    override var extraEnvironment: [String: String] {
        let modelPath = Bundle.main.resourceURL!
            .appendingPathComponent("model/granite-embedding-311m-multilingual-r2").path
        return [
            // Inference-time knobs the Python side reads from the environment.
            // `pythonEnvironment()` builds a closed dict and does not inherit
            // the process environment, so anything not listed here is
            // unreachable on this platform — and macOS is the only place the
            // MPS allocator code does anything at all.
            "EMBED_MODEL": modelPath,
            "GPU_CACHE_HIGH_WATER_MB": String(AppSettings.shared.gpuCacheHighWaterMB),
            "HOST": "127.0.0.1",
            "PORT": String(AppSettings.shared.embedderPort),
        ]
    }

    override func healthCheck() async -> Bool {
        let port = AppSettings.shared.embedderPort
        guard let url = URL(string: "http://127.0.0.1:\(port)/health") else { return false }
        return await httpProbeOK(url)
    }
}

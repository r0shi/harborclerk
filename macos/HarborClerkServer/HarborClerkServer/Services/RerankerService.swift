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
            "RERANKER_MODEL": modelPath,
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

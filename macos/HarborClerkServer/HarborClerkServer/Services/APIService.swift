import Foundation

final class APIService: PythonService {
    init() {
        super.init(name: "API")
    }

    override var executableName: String { "harbor-clerk-api" }

    override func healthCheck() async -> Bool {
        // If the process is still running, consider it healthy even if HTTP is slow.
        // The API event loop can be temporarily blocked during research/synthesis
        // (LLM prompt processing, DB commits) — that's not a crash.
        guard process?.isRunning == true else { return false }

        let port = AppSettings.shared.apiPort
        guard let url = URL(string: "http://127.0.0.1:\(port)/api/system/health") else { return true }
        do {
            let config = URLSessionConfiguration.ephemeral
            config.timeoutIntervalForRequest = 8
            let session = URLSession(configuration: config)
            let (_, response) = try await session.data(from: url)
            return (response as? HTTPURLResponse)?.statusCode == 200
        } catch {
            // HTTP failed but process is alive — still healthy
            return true
        }
    }
}

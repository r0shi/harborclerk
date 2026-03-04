import Foundation

final class EmbedderService: PythonService {
    init() {
        super.init(name: "Embedder")
    }

    override var executableName: String { "harbor-clerk-embedder" }

    override var extraEnvironment: [String: String] {
        let modelPath = Bundle.main.resourceURL!
            .appendingPathComponent("model/all-MiniLM-L6-v2").path
        return [
            "EMBED_MODEL": modelPath,
            "HOST": "127.0.0.1",
            "PORT": String(AppSettings.shared.embedderPort),
        ]
    }

    override func healthCheck() async -> Bool {
        let port = AppSettings.shared.embedderPort
        guard let url = URL(string: "http://127.0.0.1:\(port)/health") else { return false }
        do {
            let (_, response) = try await URLSession.shared.data(from: url)
            return (response as? HTTPURLResponse)?.statusCode == 200
        } catch {
            return false
        }
    }
}

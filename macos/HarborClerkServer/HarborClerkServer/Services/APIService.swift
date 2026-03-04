import Foundation

final class APIService: PythonService {
    init() {
        super.init(name: "API")
    }

    override var executableName: String { "harbor-clerk-api" }

    override func healthCheck() async -> Bool {
        let port = AppSettings.shared.apiPort
        guard let url = URL(string: "http://127.0.0.1:\(port)/api/system/health") else { return false }
        do {
            let (_, response) = try await URLSession.shared.data(from: url)
            return (response as? HTTPURLResponse)?.statusCode == 200
        } catch {
            return false
        }
    }
}

import Foundation

/// Polls the backend health endpoint to detect when the server is available.
@MainActor
final class BackendDetector: ObservableObject {
    @Published var isAvailable = false
    @Published var baseURL: URL

    private var timer: Timer?

    init() {
        let port = Self.readPort()
        baseURL = URL(string: "http://localhost:\(port)")!
    }

    func startPolling() {
        checkNow()
        timer = Timer.scheduledTimer(withTimeInterval: 2.0, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in
                self?.checkNow()
            }
        }
    }

    func stopPolling() {
        timer?.invalidate()
        timer = nil
    }

    private func checkNow() {
        let healthURL = baseURL.appendingPathComponent("api/system/health")
        Task {
            do {
                let (_, response) = try await URLSession.shared.data(from: healthURL)
                let ok = (response as? HTTPURLResponse)?.statusCode == 200
                if isAvailable != ok {
                    isAvailable = ok
                }
            } catch {
                if isAvailable {
                    isAvailable = false
                }
            }
        }
    }

    /// Read port from shared config.json.
    private static func readPort() -> Int {
        let appSupport = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first!
        let configURL = appSupport
            .appendingPathComponent("Harbor Clerk")
            .appendingPathComponent("config.json")
        if let data = try? Data(contentsOf: configURL),
           let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let port = json["api_port"] as? Int {
            return port
        }
        return 8100 // default
    }
}

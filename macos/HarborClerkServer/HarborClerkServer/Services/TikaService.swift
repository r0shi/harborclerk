import Foundation

final class TikaService: ManagedService {
    let name = "Tika"
    var state: ServiceState = .stopped
    private var process: Process?

    private var javaBin: URL {
        Bundle.main.resourceURL!.appendingPathComponent("java/bin/java")
    }
    private var tikaJar: URL {
        Bundle.main.resourceURL!.appendingPathComponent("tika/tika-server.jar")
    }
    private var port: Int { AppSettings.shared.tikaPort }

    func start() async throws {
        let proc = Process()
        proc.executableURL = javaBin
        proc.arguments = [
            "-jar", tikaJar.path,
            "--host", "127.0.0.1",
            "--port", String(port),
        ]

        let pipe = LogManager.shared.createPipe(service: name)
        proc.standardOutput = pipe
        proc.standardError = pipe

        proc.terminationHandler = { [weak self] p in
            Task { @MainActor in
                if self?.state == .running {
                    self?.state = .errored
                    LogManager.shared.append(service: "Tika", text: "Process exited unexpectedly (\(p.terminationStatus))")
                }
            }
        }

        try proc.run()
        process = proc
    }

    func stop() {
        state = .stopping
        process?.terminate()
        // Give 5s grace for JVM shutdown, then force kill
        DispatchQueue.global().asyncAfter(deadline: .now() + 5) { [weak self] in
            if self?.process?.isRunning == true {
                self?.process?.interrupt()
            }
        }
        process?.waitUntilExit()
        process = nil
        state = .stopped
    }

    func healthCheck() async -> Bool {
        // Tika returns 200 on GET /tika when ready
        guard let url = URL(string: "http://localhost:\(port)/tika") else { return false }
        do {
            let (_, response) = try await URLSession.shared.data(from: url)
            return (response as? HTTPURLResponse)?.statusCode == 200
        } catch {
            return false
        }
    }
}

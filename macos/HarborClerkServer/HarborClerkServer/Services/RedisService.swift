import Foundation

final class RedisService: ManagedService {
    let name = "Redis"
    var state: ServiceState = .stopped
    private var process: Process?

    private var redisBin: URL {
        Bundle.main.resourceURL!.appendingPathComponent("redis/bin/redis-server")
    }
    private var dataDir: URL { AppSettings.shared.redisDataDir }
    private var port: Int { AppSettings.shared.redisPort }

    func start() async throws {
        try FileManager.default.createDirectory(at: dataDir, withIntermediateDirectories: true)

        let proc = Process()
        proc.executableURL = redisBin
        proc.arguments = [
            "--port", String(port),
            "--bind", "127.0.0.1",
            "--dir", dataDir.path,
            "--daemonize", "no",
            "--save", "60", "1",
        ]

        let pipe = LogManager.shared.createPipe(service: name)
        proc.standardOutput = pipe
        proc.standardError = pipe

        proc.terminationHandler = { [weak self] p in
            Task { @MainActor in
                if self?.state == .running {
                    self?.state = .errored
                    LogManager.shared.append(service: "Redis", text: "Process exited unexpectedly (\(p.terminationStatus))")
                }
            }
        }

        try proc.run()
        process = proc
    }

    func stop() {
        state = .stopping
        process?.terminate()
        // Give 5s grace, then force kill
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
        // Simple TCP check via redis-cli ping equivalent
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/nc")
        proc.arguments = ["-z", "localhost", String(port)]
        proc.standardOutput = FileHandle.nullDevice
        proc.standardError = FileHandle.nullDevice
        try? proc.run()
        proc.waitUntilExit()
        return proc.terminationStatus == 0
    }
}

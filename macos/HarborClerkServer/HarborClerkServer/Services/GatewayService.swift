import Foundation
import os

final class GatewayService: ManagedService {
    let name = "HTTPS Gateway"
    var state: ServiceState = .stopped
    var process: Process?
    var onUnexpectedExit: (@MainActor () -> Void)?

    private var configDir: URL {
        AppSettings.dataDir.appendingPathComponent("caddy")
    }

    private var caddyfileURL: URL {
        configDir.appendingPathComponent("Caddyfile")
    }

    private var dataDir: URL {
        AppSettings.dataDir.appendingPathComponent("caddy-data")
    }

    private var runtimeConfigDir: URL {
        AppSettings.dataDir.appendingPathComponent("caddy-config")
    }

    var processIdentifier: Int32? {
        guard let process, process.isRunning else { return nil }
        return process.processIdentifier
    }
    var holdsLiveProcess: Bool { process?.isRunningAndAlive == true }
    /// The wait `stop()` runs on the signalled process; a test swaps in one
    /// that gives up (#890).
    var waitForExit: ExitWait = Process.realExitWait

    func start() async throws {
        let settings = AppSettings.shared
        guard let executable = GatewayConfig.caddyExecutable() else {
            throw NSError(
                domain: "GatewayService",
                code: 1,
                userInfo: [NSLocalizedDescriptionKey: "Caddy binary not found in app resources or Homebrew"]
            )
        }

        try FileManager.default.createDirectory(at: configDir, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: dataDir, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: runtimeConfigDir, withIntermediateDirectories: true)

        let config = GatewayConfig(
            apiPort: settings.apiPort,
            gatewayPort: settings.gatewayPort,
            hostname: settings.gatewayHostname,
            bindAddresses: settings.gatewayBindAddresses,
            certificateMode: settings.gatewayCertificateMode,
            certificatePath: settings.gatewayCertificatePath,
            privateKeyPath: settings.gatewayPrivateKeyPath,
            logLevel: settings.logLevel
        )
        try config.caddyfile.write(to: caddyfileURL, atomically: true, encoding: .utf8)

        let proc = Process()
        proc.executableURL = executable
        proc.arguments = ["run", "--config", caddyfileURL.path, "--adapter", "caddyfile"]
        proc.environment = [
            "HOME": AppSettings.dataDir.path,
            "XDG_DATA_HOME": dataDir.path,
            "XDG_CONFIG_HOME": runtimeConfigDir.path,
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin",
        ]

        let pipe = Log.createPipe(
            category: "gateway",
            fileURL: settings.logsDir.appendingPathComponent("gateway.log")
        )
        proc.standardOutput = pipe
        proc.standardError = pipe

        proc.terminationHandler = { [weak self] p in
            guard let self else { return }
            Task { @MainActor in
                self.processExited(p)
            }
        }

        try proc.runAsProcessGroupLeader()
        process = proc
    }

    /// An exit of `p` that no stop asked for. Only the process this service
    /// tracks counts: a stop that gave up its wait (#890) can see that
    /// process's exit land later, after a successor is `running`, and acting
    /// on it would mark the healthy successor errored and restart it.
    @MainActor
    func processExited(_ p: Process) {
        guard process === p, state == .running else { return }
        state = .errored
        Log.logger("gateway").error(
            "Process exited unexpectedly: status=\(p.terminationStatus, privacy: .public)"
        )
        onUnexpectedExit?()
    }

    func stop() async {
        state = .stopping
        guard let proc = process, proc.isRunning else {
            process = nil
            state = .stopped
            return
        }

        proc.terminate()
        // A wait that gave up (#890) keeps the process tracked and says
        // `errored`, not `stopped`: see `PythonService.settleStop`.
        if await waitForExit(proc, 10, name, Process.defaultKillWaitSeconds) {
            process = nil
            state = .stopped
        } else {
            state = .errored
        }
    }

    func healthCheck() async -> Bool {
        guard process?.isRunning == true else { return false }
        let settings = AppSettings.shared
        guard let url = URL(string: "\(settings.localMCPBaseURL)/t/__harbor_clerk_gateway_probe__") else { return false }
        let allowedHosts = Set([
            GatewayConfig.normalizedHostname(settings.gatewayHostname),
            "localhost",
            "127.0.0.1",
            "::1",
        ])
        return await httpsProbeOKAllowingLocalCertificate(url, allowedHosts: allowedHosts)
    }
}

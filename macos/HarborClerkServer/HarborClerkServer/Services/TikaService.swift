import Foundation
import os

final class TikaService: ManagedService {
    let name = "Tika"
    var state: ServiceState = .stopped
    let isLaunchdManaged = true

    /// Kept for forceKillEverything compatibility — when the launchd
    /// agent is loaded, returns the launchd-tracked pid via
    /// `agent.status()`. The expensive (async) path is fine because
    /// forceKillEverything is only called during emergency shutdown.
    var processIdentifier: Int32? {
        get async {
            if case .loaded(let pid) = await agent.status() { return pid }
            return nil
        }
    }

    private let agent: LaunchdAgent

    init(launchctl: LaunchctlClient = RealLaunchctlClient()) {
        let plistURL = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/LaunchAgents/com.harborclerk.tika.plist")
        self.agent = LaunchdAgent(label: "com.harborclerk.tika", plistURL: plistURL, launchctl: launchctl)
    }

    func start() async throws {
        let plist = LaunchdPlist.tika(
            bundle: Bundle.main.resourceURL!,
            logsDir: AppSettings.shared.logsDir,
            port: AppSettings.shared.tikaPort,
        )
        try await agent.ensureInstalled(plistContent: plist)
        try await agent.start()
    }

    func stop() async {
        state = .stopping
        do {
            try await agent.stop()
        } catch {
            Log.logger("tika").error(
                "launchctl bootout failed: \(error.localizedDescription, privacy: .public)"
            )
        }
        state = .stopped
    }

    func healthCheck() async -> Bool {
        // Tika returns 200 on GET /tika when ready
        guard let url = URL(string: "http://127.0.0.1:\(AppSettings.shared.tikaPort)/tika") else { return false }
        return await httpProbeOK(url)
    }
}

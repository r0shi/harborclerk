import Foundation
import os

enum LaunchdAgentError: LocalizedError {
    case plistWriteFailed(URL, Error)
    case bootstrapFailed(LaunchctlResult)
    case bootoutFailed(LaunchctlResult)
    case kickstartFailed(LaunchctlResult)

    var errorDescription: String? {
        switch self {
        case .plistWriteFailed(let url, let err): return "failed to write plist at \(url.path): \(err)"
        case .bootstrapFailed(let r):              return "launchctl bootstrap failed (exit \(r.exitCode)): \(r.stderr)"
        case .bootoutFailed(let r):                return "launchctl bootout failed (exit \(r.exitCode)): \(r.stderr)"
        case .kickstartFailed(let r):              return "launchctl kickstart failed (exit \(r.exitCode)): \(r.stderr)"
        }
    }
}

enum LaunchdServiceState: Equatable {
    case loaded(pid: Int32?)   // pid nil if loaded-but-not-running
    case notLoaded
}

/// Wraps the lifecycle of one LaunchAgent: idempotent plist install,
/// start (kickstart), stop (bootout), status (print + parse), uninstall
/// (bootout + delete plist).
final class LaunchdAgent {
    let label: String
    let plistURL: URL
    private let launchctl: LaunchctlClient
    private let log: Logger

    var domainTarget: String { "gui/\(getuid())" }
    var serviceTarget: String { "\(domainTarget)/\(label)" }

    init(label: String, plistURL: URL, launchctl: LaunchctlClient = RealLaunchctlClient()) {
        self.label = label
        self.plistURL = plistURL
        self.launchctl = launchctl
        self.log = Log.logger("launchd-\(label.replacingOccurrences(of: ".", with: "-"))")
    }

    /// Make the plist match `plistContent` on disk AND ensure the service
    /// is loaded in launchd. Two independent goals because they're tracked
    /// in two different places:
    ///   - plist file on disk (under ~/Library/LaunchAgents/)
    ///   - service-target loaded in the user's gui/<uid> domain
    ///
    /// `stop()` calls `bootout`, which unloads the service from the domain
    /// but leaves the plist file alone. So a subsequent launch can see a
    /// matching plist with no loaded service — we must still bootstrap, or
    /// the next `kickstart` will fail with "Could not find service".
    ///
    /// Called by services during their start() — every menubar launch
    /// runs this, so plist drift across upgrades / bundle moves is
    /// self-healing, AND the loaded-state is self-healing across the
    /// stop→start cycle that happens every menubar quit/relaunch.
    func ensureInstalled(plistContent: String) async throws {
        let onDisk = try? String(contentsOf: plistURL, encoding: .utf8)
        let plistMatches = (onDisk == plistContent)

        if !plistMatches {
            log.info("plist content drift — bootout, rewrite, bootstrap")

            // Bootout if currently loaded. Don't error if it wasn't loaded;
            // we just want a known-clean state before the rewrite + bootstrap.
            _ = await launchctl.bootout(serviceTarget: serviceTarget)

            // Make sure the parent directory exists.
            try FileManager.default.createDirectory(
                at: plistURL.deletingLastPathComponent(),
                withIntermediateDirectories: true,
            )

            do {
                try plistContent.write(to: plistURL, atomically: true, encoding: .utf8)
            } catch {
                throw LaunchdAgentError.plistWriteFailed(plistURL, error)
            }

            // We just bootout'd; the service is guaranteed not loaded.
            // Bootstrap unconditionally.
            let bootstrap = await launchctl.bootstrap(domain: domainTarget, plistPath: plistURL)
            guard bootstrap.success else { throw LaunchdAgentError.bootstrapFailed(bootstrap) }
            return
        }

        // Plist matches on disk, but plist-on-disk and service-loaded are
        // independent: `stop()` calls bootout, which unloads the service
        // from the domain but leaves the plist file in place. So the
        // matching-plist path doesn't guarantee a loaded service. Verify
        // and re-bootstrap if needed — otherwise the next kickstart fails
        // with "Could not find service", and the menubar reports the
        // service as errored on every launch after a clean quit.
        let currentState = await status()
        if currentState == .notLoaded {
            log.info("plist matches but service not loaded — bootstrap")
            let bootstrap = await launchctl.bootstrap(domain: domainTarget, plistPath: plistURL)
            guard bootstrap.success else { throw LaunchdAgentError.bootstrapFailed(bootstrap) }
        } else {
            log.debug("plist matches and service is loaded — no action needed")
        }
    }

    /// `launchctl kickstart -k` — starts the service, or restarts it
    /// if already running. After ensureInstalled, this is the verb to
    /// actually launch the process.
    func start() async throws {
        let r = await launchctl.kickstart(serviceTarget: serviceTarget)
        guard r.success else { throw LaunchdAgentError.kickstartFailed(r) }
    }

    /// `launchctl bootout` — stops the service. Does NOT uninstall the
    /// plist or unregister from launchd; that's `uninstall()`.
    func stop() async throws {
        let r = await launchctl.bootout(serviceTarget: serviceTarget)
        // bootout of a not-loaded service may exit non-zero. We treat
        // any "service not found" outcome as success — the goal is
        // "service is not running" and that goal is met.
        if !r.success && !Self.isNotLoadedError(r.stderr) {
            throw LaunchdAgentError.bootoutFailed(r)
        }
    }

    /// True when the stderr indicates the service was already not loaded.
    /// Three forms observed across macOS versions:
    ///   "Could not find service ..." (older / common)
    ///   "Boot-out failed: 36: Operation now in progress" (EBUSY, recent macOS)
    ///   "Boot-out failed: 3: No such process" (ESRCH)
    /// Plus a generic "no such process" fallback.
    private static func isNotLoadedError(_ stderr: String) -> Bool {
        let s = stderr.lowercased()
        return s.contains("could not find service")
            || s.contains("boot-out failed: 36")
            || s.contains("boot-out failed: 3")
            || s.contains("no such process")
    }

    /// Parse `launchctl print <service-target>` for PID + load state.
    /// Returns .notLoaded if launchctl reports no such service.
    func status() async -> LaunchdServiceState {
        let r = await launchctl.print_(serviceTarget: serviceTarget)
        if !r.success {
            // Common failure: "Could not find service ... in domain ..."
            return .notLoaded
        }
        // launchctl print output is human-readable; we look for the
        // "pid = N" line. Absent → loaded but not running. Present → loaded
        // and running. Trim leading whitespace/tabs first so we match
        // "pid =" but NOT "original pid =" (a stopped-but-loaded marker
        // whose stale value could be SIGKILLed against a recycled PID).
        if let pidLine = r.stdout.split(separator: "\n").first(where: {
            $0.trimmingCharacters(in: .whitespacesAndNewlines).hasPrefix("pid =")
        }) {
            let pidStr = pidLine.split(separator: "=").last?.trimmingCharacters(in: .whitespaces) ?? ""
            return .loaded(pid: Int32(pidStr))
        }
        return .loaded(pid: nil)
    }

    /// Stop + delete plist + remove launchd registration. Used by
    /// "Uninstall Services" (future) and at-uninstall scripts.
    func uninstall() async {
        try? await stop()
        try? FileManager.default.removeItem(at: plistURL)
    }
}

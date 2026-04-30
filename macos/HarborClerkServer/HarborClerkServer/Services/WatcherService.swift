import Foundation

/// Managed subprocess running the Python watchdog-based filesystem watcher.
/// Replaces the deleted Swift WatchedFolderManager (FSEvents). Watches every
/// row in `watched_folders` where `enabled=true`; reacts live to API
/// add/remove/enable/disable via PostgreSQL LISTEN.
final class WatcherService: PythonService {
    init() {
        super.init(name: "Watcher")
        // Wait a bit longer on shutdown — the daemon may be mid-flush of a
        // batched scan. Same value as the workers (mid-pipeline).
        shutdownGracePeriod = 10.0
    }

    override var executableName: String { "harbor-clerk-watcher" }
    override var arguments: [String] { [] }
}

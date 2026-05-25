import Foundation

/// Status of the `harbor-clerk` shim at `~/.local/bin/harbor-clerk`.
enum CliShimStatus: Equatable {
    /// No shim file exists (or it exists but isn't ours).
    case notInstalled
    /// Our shim is present and its embedded bundle path matches the current app.
    case installed(path: String)
    /// Our shim is present but points to a different bundle path (app moved / reinstalled).
    case installedOutdated(path: String, currentBundle: String)
}

/// Installs and manages the `harbor-clerk` CLI shim at `~/.local/bin/harbor-clerk`.
///
/// The shim is a small shell script that delegates to the bundled Python venv.
/// `BUNDLE_RESOURCES` is baked in at install time from `Bundle.main.resourceURL`;
/// if the user moves the app they must re-install to refresh the path.
///
/// No privileged code is required — `~/.local/bin` is under the user's home directory.
final class CliShimInstaller {

    // MARK: - Public API

    /// Canonical install path.
    static let shimPath: URL = URL(fileURLWithPath: NSHomeDirectory())
        .appendingPathComponent(".local/bin/harbor-clerk")

    /// Read the shim (if any) and determine its relationship to the current bundle.
    static func currentStatus() -> CliShimStatus {
        let url = shimPath
        guard FileManager.default.fileExists(atPath: url.path) else {
            return .notInstalled
        }

        // Read the file; if unreadable treat as not-ours
        guard let contents = try? String(contentsOf: url, encoding: .utf8) else {
            return .notInstalled
        }

        // The shim must contain our marker line to be ours
        guard contents.contains("# harbor-clerk — installed by Harbor Clerk Server") else {
            // Some other harbor-clerk script lives here — leave it alone
            return .notInstalled
        }

        // Extract the embedded BUNDLE_RESOURCES path
        guard let bundleLine = contents.components(separatedBy: "\n").first(where: { $0.hasPrefix("BUNDLE_RESOURCES=") }) else {
            return .notInstalled
        }
        // Strip 'BUNDLE_RESOURCES=' prefix and any surrounding quotes
        let embeddedBundle = bundleLine
            .dropFirst("BUNDLE_RESOURCES=".count)
            .trimmingCharacters(in: CharacterSet(charactersIn: "\""))

        let currentBundle = Bundle.main.resourceURL!.path

        if embeddedBundle == currentBundle {
            return .installed(path: url.path)
        } else {
            return .installedOutdated(path: url.path, currentBundle: currentBundle)
        }
    }

    /// Write the shim to `~/.local/bin/harbor-clerk`, creating the directory if needed.
    /// Sets executable permissions (0o755) after writing.
    static func install() throws {
        let bundle = Bundle.main.resourceURL!.path
        let shim = makeShimContent(bundleResources: bundle)

        let dir = shimPath.deletingLastPathComponent()
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true, attributes: nil)

        try shim.write(to: shimPath, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: shimPath.path)
    }

    /// Remove the shim if it was installed by Harbor Clerk Server. No-ops on anything else.
    static func uninstall() throws {
        let status = currentStatus()
        switch status {
        case .installed, .installedOutdated:
            try FileManager.default.removeItem(at: shimPath)
        case .notInstalled:
            // Nothing to do — either absent or not ours
            break
        }
    }

    // MARK: - Internal helpers

    /// Generate the shim script content with `BUNDLE_RESOURCES` baked in.
    static func makeShimContent(bundleResources: String) -> String {
        return """
        #!/bin/sh
        # harbor-clerk — installed by Harbor Clerk Server
        # Invokes the bundled Python via Harbor Clerk Server.app's venv.
        # Re-install via Preferences if you move or reinstall the app.
        BUNDLE_RESOURCES="\(bundleResources)"
        export PATH="$BUNDLE_RESOURCES/venv/bin:/usr/bin:/bin"
        export PYTHONPATH="$BUNDLE_RESOURCES/venv/lib"
        exec "$BUNDLE_RESOURCES/venv/bin/python" -m harbor_clerk.cli.main "$@"
        """
    }
}

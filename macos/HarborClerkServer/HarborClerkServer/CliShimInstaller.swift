import Foundation

/// Status of the `harbor-clerk` shim at `~/.local/bin/harbor-clerk`.
enum CliShimStatus: Equatable {
    /// No shim file exists (or it exists but isn't ours).
    case notInstalled
    /// Our shim is present and its embedded bundle path + API URL match current settings.
    case installed(path: String)
    /// Our shim is present but the embedded bundle path or API URL is stale
    /// (app moved, reinstalled, or apiPort changed in Preferences).
    case installedOutdated(path: String, currentBundle: String)
}

/// Installs and manages the `harbor-clerk` CLI shim at `~/.local/bin/harbor-clerk`.
///
/// The shim is a small shell script that delegates to the bundled Python venv.
/// `BUNDLE_RESOURCES` and `HARBOR_CLERK_URL` are baked in at install time from
/// `Bundle.main.resourceURL` and `AppSettings.shared.localMCPBaseURL` respectively.
/// If the user moves the app or changes the gateway port they must re-install via
/// Preferences to refresh the shim — `currentStatus()` reports `installedOutdated`
/// in either case so the UI button surfaces "Re-install".
///
/// The `HARBOR_CLERK_URL` line uses `${VAR:-default}` so the user can still
/// override it in their shell for unusual deployments (e.g. pointing the CLI
/// at a different Harbor Clerk instance on the network).
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
        let currentDefaultURL = AppSettings.shared.localMCPBaseURL

        // Detect "stale URL": the shim's baked-in URL no longer matches the
        // configured local HTTPS gateway. Older shims had no URL line at all
        // or used the direct HTTP API port — treat them as outdated so the
        // user gets prompted to re-install.
        let embeddedDefaultURL = extractEmbeddedDefaultURL(from: contents)

        if embeddedBundle == currentBundle && embeddedDefaultURL == currentDefaultURL {
            return .installed(path: url.path)
        } else {
            return .installedOutdated(path: url.path, currentBundle: currentBundle)
        }
    }

    /// Parse the default URL from the `export HARBOR_CLERK_URL=...` line in a shim.
    /// Returns nil if the URL line is absent or unparseable — `currentStatus`
    /// then treats the shim as outdated.
    static func extractEmbeddedDefaultURL(from shimContents: String) -> String? {
        guard let urlLine = shimContents.components(separatedBy: "\n").first(where: { $0.hasPrefix("export HARBOR_CLERK_URL=") }) else {
            return nil
        }
        // The line shape is: export HARBOR_CLERK_URL="${HARBOR_CLERK_URL:-https://localhost:PORT}"
        // Find the default-value substring after ":-" and return it whole.
        guard let defaultMarker = urlLine.range(of: ":-"),
              let closeBrace = urlLine.range(of: "}", range: defaultMarker.upperBound..<urlLine.endIndex) else {
            return nil
        }
        return String(urlLine[defaultMarker.upperBound..<closeBrace.lowerBound])
    }

    /// Write the shim to `~/.local/bin/harbor-clerk`, creating the directory if needed.
    /// Sets executable permissions (0o755) after writing.
    static func install() throws {
        let bundle = Bundle.main.resourceURL!.path
        let defaultURL = AppSettings.shared.localMCPBaseURL
        let shim = makeShimContent(bundleResources: bundle, defaultURL: defaultURL)

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

    /// Generate the shim script content with `BUNDLE_RESOURCES` and the
    /// default local HTTPS gateway URL baked in. The URL line uses `${VAR:-default}` so a
    /// user can still override `HARBOR_CLERK_URL` in their shell for unusual
    /// deployments (e.g. pointing the CLI at a different Harbor Clerk
    /// instance on the network).
    static func makeShimContent(bundleResources: String, defaultURL: String) -> String {
        return """
        #!/bin/sh
        # harbor-clerk — installed by Harbor Clerk Server
        # Invokes the bundled Python via Harbor Clerk Server.app's venv.
        # Re-install via Preferences if you move/reinstall the app or change the HTTPS gateway port.
        BUNDLE_RESOURCES="\(bundleResources)"
        export PATH="$BUNDLE_RESOURCES/venv/bin:/usr/bin:/bin"
        export PYTHONPATH="$BUNDLE_RESOURCES/venv/lib"
        export PYTHONDONTWRITEBYTECODE=1
        export HARBOR_CLERK_URL="${HARBOR_CLERK_URL:-\(defaultURL)}"
        export HARBOR_CLERK_INSECURE_SKIP_VERIFY="${HARBOR_CLERK_INSECURE_SKIP_VERIFY:-1}"
        exec "$BUNDLE_RESOURCES/venv/bin/python" -m harbor_clerk.cli.main "$@"
        """
    }
}

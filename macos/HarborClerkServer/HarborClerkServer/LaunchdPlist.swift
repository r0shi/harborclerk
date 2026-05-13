import Foundation

/// Generates LaunchAgent plist XML for our two launchd-managed services.
///
/// The plists hold absolute paths to the bundled binaries plus the user's
/// data + logs directories. Because the bundle can live in any location
/// (the user might keep it in /Applications, ~/Applications, or run it
/// from a build directory), plist contents are regenerated on every
/// menubar startup from the current `Bundle.main.resourceURL` and
/// `AppSettings`. LaunchdAgent.ensureInstalled compares the generated
/// content to what's on disk and only bootout-rewrite-bootstraps when
/// something changed.
enum LaunchdPlist {

    /// Postgres LaunchAgent plist contents. The `Program` is `postgres`
    /// directly (NOT `pg_ctl`) so launchd tracks the postmaster's PID.
    /// pg_ctl is a wrapper that starts postgres and exits; if launchd
    /// tracked pg_ctl it would either restart-loop it or consider the
    /// service gone.
    static func postgres(bundle: URL, dataDir: URL, logsDir: URL, port: Int) -> String {
        let postgresBin = bundle.appendingPathComponent("postgres/bin/postgres").path
        let pgBinDir = bundle.appendingPathComponent("postgres/bin").path
        let pgLibDir = bundle.appendingPathComponent("postgres/lib").path
        let pgShareDir = bundle.appendingPathComponent("postgres/share").path
        let logFile = logsDir.appendingPathComponent("postgres-launchd.log").path

        return """
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0">
        <dict>
            <key>Label</key>
            <string>com.harborclerk.postgres</string>
            <key>Program</key>
            <string>\(postgresBin)</string>
            <key>ProgramArguments</key>
            <array>
                <string>\(postgresBin)</string>
                <string>-D</string>
                <string>\(dataDir.path)</string>
                <string>-p</string>
                <string>\(port)</string>
                <string>-k</string>
                <string>/tmp</string>
            </array>
            <key>EnvironmentVariables</key>
            <dict>
                <key>PGDATA</key>
                <string>\(dataDir.path)</string>
                <key>PATH</key>
                <string>\(pgBinDir):/usr/bin:/bin</string>
                <key>LD_LIBRARY_PATH</key>
                <string>\(pgLibDir)</string>
                <key>DYLD_LIBRARY_PATH</key>
                <string>\(pgLibDir)</string>
                <key>PGSHARE</key>
                <string>\(pgShareDir)</string>
            </dict>
            <key>RunAtLoad</key>
            <false/>
            <key>KeepAlive</key>
            <dict>
                <key>SuccessfulExit</key>
                <false/>
            </dict>
            <key>StandardOutPath</key>
            <string>\(logFile)</string>
            <key>StandardErrorPath</key>
            <string>\(logFile)</string>
        </dict>
        </plist>
        """
    }

    /// Tika LaunchAgent plist contents. Same shape as postgres.
    static func tika(bundle: URL, logsDir: URL, port: Int) -> String {
        let javaBin = bundle.appendingPathComponent("java/Contents/Home/bin/java").path
        let javaHome = bundle.appendingPathComponent("java/Contents/Home").path
        let tikaJar = bundle.appendingPathComponent("tika/tika-server.jar").path
        let logFile = logsDir.appendingPathComponent("tika-launchd.log").path

        return """
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0">
        <dict>
            <key>Label</key>
            <string>com.harborclerk.tika</string>
            <key>Program</key>
            <string>\(javaBin)</string>
            <key>ProgramArguments</key>
            <array>
                <string>\(javaBin)</string>
                <string>-jar</string>
                <string>\(tikaJar)</string>
                <string>--host</string>
                <string>127.0.0.1</string>
                <string>--port</string>
                <string>\(port)</string>
            </array>
            <key>EnvironmentVariables</key>
            <dict>
                <key>JAVA_HOME</key>
                <string>\(javaHome)</string>
                <key>PATH</key>
                <string>\(javaHome)/bin:/usr/bin:/bin</string>
            </dict>
            <key>RunAtLoad</key>
            <false/>
            <key>KeepAlive</key>
            <dict>
                <key>SuccessfulExit</key>
                <false/>
            </dict>
            <key>StandardOutPath</key>
            <string>\(logFile)</string>
            <key>StandardErrorPath</key>
            <string>\(logFile)</string>
        </dict>
        </plist>
        """
    }
}

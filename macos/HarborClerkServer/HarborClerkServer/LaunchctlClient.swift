import Foundation

/// Result of a `launchctl` invocation.
struct LaunchctlResult: Equatable {
    let exitCode: Int32
    let stdout: String
    let stderr: String

    var success: Bool { exitCode == 0 }
}

/// Abstraction over `/bin/launchctl` invocations so the LaunchdAgent
/// lifecycle logic is unit-testable. Production uses `RealLaunchctlClient`
/// which shells out via `Process`; tests use `FakeLaunchctlClient` which
/// records invocations and returns scripted results.
protocol LaunchctlClient: AnyObject {
    /// `launchctl bootstrap <domain-target> <plist-path>`
    func bootstrap(domain: String, plistPath: URL) async -> LaunchctlResult

    /// `launchctl bootout <service-target>` where service-target is
    /// `<domain>/<label>` (or pass the plist path — both work; the
    /// service-target form is preferred).
    func bootout(serviceTarget: String) async -> LaunchctlResult

    /// `launchctl kickstart -k <service-target>` — start the service, or
    /// restart if already running (-k for "kill and restart").
    func kickstart(serviceTarget: String) async -> LaunchctlResult

    /// `launchctl print <service-target>` — returns the parsed PID + state.
    /// Result.stdout is the raw output; the caller (LaunchdAgent) parses.
    func print_(serviceTarget: String) async -> LaunchctlResult
}

/// Production implementation that shells out via `Process`.
final class RealLaunchctlClient: LaunchctlClient {
    private static let launchctlPath = "/bin/launchctl"

    func bootstrap(domain: String, plistPath: URL) async -> LaunchctlResult {
        await run(["bootstrap", domain, plistPath.path])
    }

    func bootout(serviceTarget: String) async -> LaunchctlResult {
        await run(["bootout", serviceTarget])
    }

    func kickstart(serviceTarget: String) async -> LaunchctlResult {
        await run(["kickstart", "-k", serviceTarget])
    }

    func print_(serviceTarget: String) async -> LaunchctlResult {
        await run(["print", serviceTarget])
    }

    private func run(_ args: [String]) async -> LaunchctlResult {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: Self.launchctlPath)
        proc.arguments = args
        let stdout = Pipe()
        let stderr = Pipe()
        proc.standardOutput = stdout
        proc.standardError = stderr

        do {
            try proc.run()
        } catch {
            return LaunchctlResult(
                exitCode: -1,
                stdout: "",
                stderr: "failed to launch launchctl: \(error)",
            )
        }

        return await withCheckedContinuation { c in
            DispatchQueue.global().async {
                proc.waitUntilExit()
                let outData = stdout.fileHandleForReading.readDataToEndOfFile()
                let errData = stderr.fileHandleForReading.readDataToEndOfFile()
                c.resume(returning: LaunchctlResult(
                    exitCode: proc.terminationStatus,
                    stdout: String(data: outData, encoding: .utf8) ?? "",
                    stderr: String(data: errData, encoding: .utf8) ?? "",
                ))
            }
        }
    }
}

/// Test implementation that records every invocation and returns
/// caller-scripted results. Default behaviour: every call returns
/// success with empty output.
final class FakeLaunchctlClient: LaunchctlClient {
    /// Recorded invocations, in call order. Format: "verb arg1 arg2 ..."
    /// e.g. "bootstrap gui/501 /Users/alex/Library/LaunchAgents/com.example.plist"
    private(set) var invocations: [String] = []

    /// Override the result for the next matching invocation. Set on a
    /// per-verb basis. If not set, defaults to a success result.
    var nextBootstrapResult: LaunchctlResult?
    var nextBootoutResult: LaunchctlResult?
    var nextKickstartResult: LaunchctlResult?
    var nextPrintResult: LaunchctlResult?

    func bootstrap(domain: String, plistPath: URL) async -> LaunchctlResult {
        invocations.append("bootstrap \(domain) \(plistPath.path)")
        let r = nextBootstrapResult ?? .ok
        nextBootstrapResult = nil
        return r
    }
    func bootout(serviceTarget: String) async -> LaunchctlResult {
        invocations.append("bootout \(serviceTarget)")
        let r = nextBootoutResult ?? .ok
        nextBootoutResult = nil
        return r
    }
    func kickstart(serviceTarget: String) async -> LaunchctlResult {
        invocations.append("kickstart \(serviceTarget)")
        let r = nextKickstartResult ?? .ok
        nextKickstartResult = nil
        return r
    }
    func print_(serviceTarget: String) async -> LaunchctlResult {
        invocations.append("print \(serviceTarget)")
        let r = nextPrintResult ?? .ok
        nextPrintResult = nil
        return r
    }
}

extension LaunchctlResult {
    static let ok = LaunchctlResult(exitCode: 0, stdout: "", stderr: "")
}

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

        return await withCheckedContinuation { c in
            // Three things must complete before we resume:
            //   1. child exits (terminationHandler fires)
            //   2. stdout drain hits EOF
            //   3. stderr drain hits EOF
            // A DispatchGroup gates the resume on all three.
            let group = DispatchGroup()
            let outBox = MutableBox<Data>(.init())
            let errBox = MutableBox<Data>(.init())

            // Drain stdout/stderr on background queues. Without concurrent
            // drains, `launchctl print` output > ~64 KB would block in the
            // child's write(2) (kernel pipe buffer fills).
            group.enter()
            DispatchQueue.global().async {
                outBox.value = stdout.fileHandleForReading.readDataToEndOfFile()
                group.leave()
            }
            group.enter()
            DispatchQueue.global().async {
                errBox.value = stderr.fileHandleForReading.readDataToEndOfFile()
                group.leave()
            }

            // terminationHandler fires from Foundation's internal queue
            // when the child exits, regardless of which thread launched
            // the Process. Replaces `proc.waitUntilExit()` — empirically
            // unreliable when `proc.run()` is called from one thread
            // (MainActor here) and the wait is called from another
            // (DispatchQueue.global()). Witnessed in the wild on first
            // launch: `sample` of the hung menubar showed a worker thread
            // spinning `mach_msg2_trap` inside `waitUntilExit` with no
            // live child process and both pipe drains already finished.
            // The termination signal reached Foundation but the dispatch
            // queue's runloop never woke up.
            group.enter()
            proc.terminationHandler = { _ in group.leave() }

            do {
                try proc.run()
            } catch {
                // terminationHandler will NOT fire if run() threw —
                // balance the third group.enter() manually, and close
                // the read ends so the drains don't block forever.
                try? stdout.fileHandleForReading.close()
                try? stderr.fileHandleForReading.close()
                group.leave()
                c.resume(returning: LaunchctlResult(
                    exitCode: -1,
                    stdout: "",
                    stderr: "failed to launch launchctl: \(error)",
                ))
                return
            }

            // Wait for all three (term + 2 drains) on a background queue
            // so we don't block the calling actor.
            DispatchQueue.global().async {
                group.wait()
                c.resume(returning: LaunchctlResult(
                    exitCode: proc.terminationStatus,
                    stdout: String(data: outBox.value, encoding: .utf8) ?? "",
                    stderr: String(data: errBox.value, encoding: .utf8) ?? "",
                ))
            }
        }
    }
}

/// Reference wrapper so `let` closures can mutate captured state without
/// `inout` gymnastics. Used by `RealLaunchctlClient.run` to assemble
/// stdout/stderr Data across three concurrent dispatch blocks.
///
/// `@unchecked Sendable` because access is serialized by a DispatchGroup —
/// writers leave the group before the consumer thread enters `group.wait()`
/// and reads `.value`. Swift's Sendable checker can't prove this so we
/// assert it ourselves.
///
/// IMPORTANT: never read `.value` before the gating `group.wait()` returns.
/// The DispatchGroup is what synchronizes the writes, not the class itself.
/// A future maintainer adding a read on the producer side (before the
/// consumer's group.wait) would silently introduce a data race that the
/// @unchecked annotation suppresses.
private final class MutableBox<T>: @unchecked Sendable {
    var value: T
    init(_ initial: T) { self.value = initial }
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

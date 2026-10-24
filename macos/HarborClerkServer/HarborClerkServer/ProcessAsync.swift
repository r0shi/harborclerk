import Foundation

extension Process {
    /// Async-await replacement for `try run()` followed by `waitUntilExit()`.
    /// Safe to call from any actor / dispatch queue.
    ///
    /// Uses `terminationHandler`, which Foundation fires from its internal
    /// queue when the child exits. Replaces `waitUntilExit()` because that
    /// method spins a CFRunLoop on the *calling* thread, and the
    /// termination notification doesn't reliably wake up a dispatch-queue
    /// runloop when `run()` and `waitUntilExit()` happen on different
    /// threads. Witnessed in the wild during PR-4 smoke testing:
    /// `launchctl bootout` (a ~7ms child) hung the menubar forever on
    /// first launch — `sample` showed a worker thread spinning
    /// `mach_msg2_trap` inside `waitUntilExit` with no live child and
    /// pipe drains already finished.
    ///
    /// The hypothesis is a race with very-short-lived children: the
    /// termination notification arrives before the wait thread's runloop
    /// has fully attached as a listener. The `terminationHandler` path
    /// has no such race because the handler is installed *before* `run()`
    /// — Foundation guarantees the closure runs when the child exits
    /// regardless of timing or thread.
    ///
    /// `MigrationRunner.runAlembic`, `PostgresService.healthCheck`,
    /// `bundledPgMajorVersion`, `initializeDatabase`, and
    /// `createDatabaseAndExtensions` all previously used the unsafe
    /// pattern; this helper exists so they migrate to one shared
    /// implementation.
    ///
    /// Returns `terminationStatus` on clean exit. Throws if `run()` throws
    /// (e.g. executable not found). The caller still owns stdout/stderr
    /// pipe setup; this helper only manages the lifecycle.
    func runAndAwait() async throws -> Int32 {
        // Edge case: an earlier termination handler could still be set
        // (callers shouldn't rely on this, but be defensive). Replace it
        // — we own the lifecycle for this call.
        try await withCheckedThrowingContinuation { (c: CheckedContinuation<Int32, Error>) in
            terminationHandler = { proc in
                c.resume(returning: proc.terminationStatus)
            }
            do {
                try run()
            } catch {
                // run() failed — terminationHandler will never fire.
                // Clear it (release the captured continuation) and throw.
                terminationHandler = nil
                c.resume(throwing: error)
            }
        }
    }
}

extension Process {
    /// How long a short-lived probe (lsof, `postgres --version`) may run
    /// before `runCapturingOutput` kills it.
    static let probeTimeoutSeconds: TimeInterval = 10

    /// Run a short-lived tool to completion and return what it wrote to
    /// stdout, or `nil` if it could not be launched or had not exited within
    /// `timeout` (it is then SIGKILLed). The caller sets the executable,
    /// arguments and stderr; this sets stdout and the `terminationHandler`.
    ///
    /// Both waits are bounded, which `runAndAwait()` followed by
    /// `readDataToEndOfFile()` is not (#890):
    ///   - the exit wait, by `timeout`: `lsof` can stall in the kernel on a
    ///     wedged file system, and a stop that probes the port behind it
    ///     waited on it with no deadline;
    ///   - the read, by reading only what is already in the pipe. EOF on a
    ///     pipe needs every write end closed, including copies held by
    ///     anything the tool spawned (lsof forks a helper for its kernel
    ///     calls). Once the tool has exited its writes are all in the
    ///     buffer, so a non-blocking drain gets everything without waiting
    ///     on a straggler's copy of the write end.
    ///
    /// For output under the pipe buffer (64 KB) only: the tool is not drained
    /// while it runs, so a larger output would block it in `write(2)` until
    /// the timeout.
    func runCapturingOutput(timeout: TimeInterval) async -> Data? {
        let pipe = Pipe()
        standardOutput = pipe
        let once = ResumeOnce()
        let exited: Bool = await withCheckedContinuation { (c: CheckedContinuation<Bool, Never>) in
            terminationHandler = { _ in
                if once.claim() { c.resume(returning: true) }
            }
            do {
                try run()
            } catch {
                terminationHandler = nil
                if once.claim() { c.resume(returning: false) }
                return
            }
            let pid = processIdentifier
            DispatchQueue.global().asyncAfter(deadline: .now() + timeout) {
                guard once.claim() else { return }
                kill(pid, SIGKILL)
                c.resume(returning: false)
            }
        }
        try? pipe.fileHandleForWriting.close()
        defer { try? pipe.fileHandleForReading.close() }
        guard exited else { return nil }
        return Self.drainWithoutWaiting(pipe.fileHandleForReading.fileDescriptor)
    }

    /// Everything readable on `fd` right now, without waiting for EOF.
    static func drainWithoutWaiting(_ fd: Int32) -> Data {
        let flags = fcntl(fd, F_GETFL)
        if flags >= 0 { _ = fcntl(fd, F_SETFL, flags | O_NONBLOCK) }
        var out = Data()
        var buf = [UInt8](repeating: 0, count: 4096)
        while true {
            let n = read(fd, &buf, buf.count)
            if n > 0 {
                out.append(buf, count: n)
            } else if n < 0, errno == EINTR {
                continue
            } else {
                break  // 0 = EOF, or EAGAIN: nothing more without waiting
            }
        }
        return out
    }
}

/// First caller wins; for resuming a continuation from whichever of several
/// racing paths (exit, timeout) gets there first.
final class ResumeOnce: @unchecked Sendable {
    private let lock = NSLock()
    private var claimed = false

    func claim() -> Bool {
        lock.withLock {
            if claimed { return false }
            claimed = true
            return true
        }
    }
}

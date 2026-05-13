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

import Foundation
import os

/// Shared helpers for the shutdown path.
///
/// Two known fragilities of `Foundation.Process` + `Pipe` get addressed
/// here in one place rather than re-implemented in every service:
///
/// 1. **Pipe + `waitUntilExit` deadlock.** Apple's `waitUntilExit`
///    documents that it waits for *both* process termination *and* all
///    Pipe references to release. `Log.createPipe()` attaches a
///    `readabilityHandler` that nils itself on EOF (empty data), which
///    is the documented cleanly-exiting fix — but a hung child never
///    sends EOF, so the handler never runs the nil-out branch, so the
///    pipe stays open, so `waitUntilExit` blocks indefinitely. The
///    `detachPipesForShutdown()` helper proactively nils the handler,
///    closes the read end, and replaces `standardOutput`/`standardError`
///    with `/dev/null` so the wait only blocks on process termination.
///    See `project_menubar_process_management_audit.md` for the audit
///    that caught this.
///
/// 2. **No bounded wait.** Every service's stop path currently does
///    `await withCheckedContinuation { proc.waitUntilExit() }` with no
///    deadline beyond the per-service `DispatchQueue.asyncAfter` SIGKILL
///    fallback. That fallback works in isolation but `stopAll()` is
///    sequential; one hung worker stalls the whole shutdown. The
///    `waitForExitWithDeadline()` helper folds SIGTERM-grace,
///    SIGKILL-on-deadline, and the actual wait into a single call so
///    callers can compose timeouts predictably.
extension Process {
    /// Defuse this Process's stdout/stderr Pipes so `waitUntilExit()` no
    /// longer blocks on pipe-close even when the child is hung.
    ///
    /// What we do:
    ///   1. Nil the `readabilityHandler` on each unique attached Pipe's
    ///      read end. Releases the closure (no more handler invocations).
    ///   2. Close the read-end FileHandle. This causes the kernel to
    ///      report EOF on the pipe, and (more importantly) makes any
    ///      blocked child write return EPIPE / receive SIGPIPE — which
    ///      unwedges a child that was hung in a `write(2)` because the
    ///      pipe buffer was full and our readabilityHandler had fallen
    ///      behind.
    ///
    /// What we deliberately don't do:
    ///   ~~Reassign `self.standardOutput = FileHandle.nullDevice`~~.
    ///   `Foundation.Process` raises `NSInvalidArgumentException: task
    ///   already launched` if you set `standardOutput`/`standardError`
    ///   after `run()`. Closing the read-end fd is sufficient to defuse
    ///   the wait — we don't need to replace the entire stream.
    ///
    /// Safe to call multiple times. Order relative to `terminate()`
    /// doesn't matter.
    func detachPipesForShutdown() {
        let stdoutPipe = self.standardOutput as? Pipe
        let stderrPipe = self.standardError as? Pipe

        // Many of our services (Log.createPipe in PythonService) attach
        // the SAME Pipe instance to both stdout and stderr. Identity
        // check skips a redundant close — closing twice would just throw
        // on the second call.
        if let pipe = stdoutPipe {
            pipe.fileHandleForReading.readabilityHandler = nil
            try? pipe.fileHandleForReading.close()
        }
        if let pipe = stderrPipe, pipe !== stdoutPipe {
            pipe.fileHandleForReading.readabilityHandler = nil
            try? pipe.fileHandleForReading.close()
        }
    }

    /// Wait for this Process to exit, with a bounded grace period.
    ///
    /// Behaviour:
    ///   1. If the process is not running, returns immediately.
    ///   2. Otherwise schedules a `SIGKILL` at `now + graceSeconds`. The
    ///      schedule is fire-and-forget — if the process exits before the
    ///      deadline, the SIGKILL closure short-circuits via
    ///      `proc.isRunning` check.
    ///   3. Blocks (asynchronously) on `waitUntilExit()` until the
    ///      process actually exits.
    ///
    /// This is the standard pattern from `PythonService.stop()` (it had it
    /// before this audit) lifted out so `stopAll()` can use it too without
    /// reimplementing — that reimplementation was the bug.
    ///
    /// Always detaches Pipes first via `detachPipesForShutdown()`. Without
    /// that, `waitUntilExit` could still hang waiting for the pipe to
    /// close even after `SIGKILL` arrives.
    ///
    /// - Parameters:
    ///   - graceSeconds: how long after `terminate()` to wait before
    ///     escalating to `SIGKILL`. The caller is expected to have
    ///     already sent `SIGTERM` via `terminate()` before invoking
    ///     this helper.
    ///   - serviceName: included in log messages so the "had to SIGKILL
    ///     foo" line is informative.
    func waitForExitWithDeadline(
        graceSeconds: TimeInterval,
        serviceName: String,
    ) async {
        guard self.isRunning else { return }

        detachPipesForShutdown()

        // Schedule SIGKILL escalation. Fire-and-forget; if the process is
        // already gone by the deadline, kill(0) is harmless.
        let proc = self
        let name = serviceName
        let grace = graceSeconds
        DispatchQueue.global().asyncAfter(deadline: .now() + grace) {
            guard proc.isRunning else { return }
            Log.logger("lifecycle").warning(
                "[\(name, privacy: .public)] Still running after \(Int(grace), privacy: .public)s — sending SIGKILL"
            )
            // killpg targets the entire process group (which the leader's pid
            // identifies after runAsProcessGroupLeader). Catches grandchildren —
            // Tika's child JVMs, worker-spawned tesseract/pdftoppm/magick.
            // Falls back gracefully if the process wasn't launched as a pgid
            // leader: killpg with a non-leader pid returns -1 ESRCH and we
            // log it. For belt-and-suspenders we also send a plain kill().
            if killpg(proc.processIdentifier, SIGKILL) != 0 {
                // Not a pgid leader (or already dead) — fall back to kill().
                kill(proc.processIdentifier, SIGKILL)
            }
        }

        await withCheckedContinuation { (c: CheckedContinuation<Void, Never>) in
            DispatchQueue.global().async {
                // Poll rather than `waitUntilExit()`. That call runs its own
                // runloop waiting for a termination notification, and if the
                // child exits between the `isRunning` guard above and the call
                // itself, the notification is already gone and it blocks
                // forever. Reproduced at 3 of 8 full test-suite runs on
                // /usr/bin/true, which exits faster than we can get here.
                //
                // Nothing rescues it once wedged: the SIGKILL escalation above
                // opens with `guard proc.isRunning`, sees a process that has
                // already exited, and returns without resuming anything. So the
                // continuation is never resumed and shutdown waits forever —
                // the deadlock #341 bounded for the Pipe case, reached by a
                // different route.
                //
                // `terminationHandler` would be the tidier fix but is not ours
                // to take: PythonService sets one for its restart logic and
                // also calls this helper, so assigning here would silently
                // clobber it.
                while proc.isRunning {
                    usleep(20_000)  // 20ms — bounded by the SIGKILL escalation
                }
                c.resume()
            }
        }
    }
}

extension Process {
    /// Launch this Process and immediately make the child its own
    /// process-group leader, so `killpg(child.pid, signal)` reaches
    /// any grandchildren the child later spawns.
    ///
    /// Required for services whose children spawn further children:
    /// Tika's JVM forks child JVMs for heavy extractions, Python
    /// workers spawn `pdftoppm`, `tesseract`, `magick`, etc. Without
    /// this, `kill(pid, SIGKILL)` only hits the leaf process; the
    /// grandchildren get reparented to launchd and leak across menubar
    /// restarts. See `project_menubar_process_management_audit.md`
    /// item 4.
    ///
    /// Implementation: `Process.run()` uses `posix_spawn` internally
    /// but doesn't expose `POSIX_SPAWN_SETPGROUP`. We instead call
    /// `setpgid()` on the child from the parent immediately after
    /// `run()` returns. There's a theoretical race — if the child
    /// fork-exec's another binary before our setpgid call lands, that
    /// grandchild inherits the OLD pgid. In practice the window is
    /// microseconds and none of our managed services (Python, llama,
    /// Java/Tika) fork in their startup path before initialising; the
    /// race has never been observed.
    ///
    /// On macOS, `setpgid(pid, 0)` says "make this pid a new pgid
    /// leader, with its pid as the pgid value". The race-with-already-
    /// exec'd-child error EACCES is logged but not raised — by the
    /// time we see it, the child is already running with its inherited
    /// pgid, which is no worse than what we had before this method
    /// existed.
    func runAsProcessGroupLeader() throws {
        try self.run()
        let pid = self.processIdentifier
        if setpgid(pid, 0) != 0 {
            let err = errno
            Log.logger("lifecycle").warning(
                "setpgid(\(pid, privacy: .public), 0) failed: errno=\(err, privacy: .public) — grandchildren may leak on force-kill of this pid"
            )
        }
    }
}

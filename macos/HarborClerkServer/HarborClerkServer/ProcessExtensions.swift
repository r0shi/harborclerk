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
///    callers can compose timeouts predictably. The wait after the SIGKILL
///    is bounded too (#890): a process Foundation never reports as exited
///    costs `killWaitSeconds`, not the rest of the shutdown.
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

    /// How long the wait goes on after the SIGKILL before it gives up. A
    /// SIGKILLed process is normally gone, and Foundation has noticed, within
    /// milliseconds; this is the bound for when it has not (#890).
    static let defaultKillWaitSeconds: TimeInterval = 5

    /// The real wait, in the shape every service's `waitForExit` seam takes.
    static let realExitWait: ExitWait = {
        await $0.waitForExitWithDeadline(graceSeconds: $1, serviceName: $2, killWaitSeconds: $3)
    }

    /// Foundation reports it running and the kernel agrees. `isRunning` alone
    /// can stay true over a process that is gone, when Foundation never
    /// noticed the exit (#890); then a stop's wait gave up on it and every
    /// later start refused it, for good. Used for the stop's exit signal
    /// (`waitForExitWithDeadline`) and for each service's `holdsLiveProcess`.
    var isRunningAndAlive: Bool {
        isRunning && ProcessLiveness.isAlive(processIdentifier)
    }

    /// Wait for this Process to exit, with a bounded grace period and a
    /// bounded wait after the SIGKILL.
    ///
    /// Behaviour:
    ///   1. If the process is not running, returns `true` immediately.
    ///   2. Otherwise polls `isRunning`. At `now + graceSeconds` it sends
    ///      `SIGKILL` to the process group.
    ///   3. At `graceSeconds + killWaitSeconds` it stops waiting, logs an
    ///      error and returns `false`: the process may still be alive, or
    ///      Foundation may never have noticed its exit. Either way a stop
    ///      must not wait on it forever (#890: a CI run hung 18 minutes
    ///      after "sending SIGKILL").
    ///
    /// This is the standard pattern from `PythonService.stop()` (it had it
    /// before this audit) lifted out so `stopAll()` can use it too without
    /// reimplementing — that reimplementation was the bug.
    ///
    /// Always detaches Pipes first via `detachPipesForShutdown()`.
    ///
    /// - Parameters:
    ///   - graceSeconds: how long after `terminate()` to wait before
    ///     escalating to `SIGKILL`. The caller is expected to have
    ///     already sent `SIGTERM` via `terminate()` before invoking
    ///     this helper.
    ///   - serviceName: included in log messages so the "had to SIGKILL
    ///     foo" line is informative.
    ///   - killWaitSeconds: how long after the SIGKILL to keep waiting.
    /// - Returns: whether the process was seen to exit. `false` means the
    ///   caller is still holding a process Foundation reports as running,
    ///   and must keep it tracked and not report a clean stop. Not
    ///   discardable, so every caller decides.
    func waitForExitWithDeadline(
        graceSeconds: TimeInterval,
        serviceName: String,
        killWaitSeconds: TimeInterval = Process.defaultKillWaitSeconds,
    ) async -> Bool {
        guard self.isRunningAndAlive else { return true }

        detachPipesForShutdown()

        let proc = self
        return await withCheckedContinuation { (c: CheckedContinuation<Bool, Never>) in
            DispatchQueue.global().async {
                c.resume(returning: Process.pollUntilExited(
                    isRunning: { proc.isRunningAndAlive },
                    sendKill: {
                        // killpg targets the entire process group (which the
                        // leader's pid identifies after runAsProcessGroupLeader).
                        // Catches grandchildren — Tika's child JVMs,
                        // worker-spawned tesseract/pdftoppm/magick. Falls back
                        // to a plain kill() if the process is not a group
                        // leader (or is already dead).
                        if killpg(proc.processIdentifier, SIGKILL) != 0 {
                            kill(proc.processIdentifier, SIGKILL)
                        }
                    },
                    graceSeconds: graceSeconds,
                    killWaitSeconds: killWaitSeconds,
                    serviceName: serviceName
                ))
            }
        }
    }

    /// The wait behind `waitForExitWithDeadline`, separated from `Process` so a
    /// test can drive it with a process that never reports its exit, which no
    /// real child can be made to do on demand. Blocks the calling thread; run
    /// it off the cooperative pool.
    ///
    /// Polls rather than calling `waitUntilExit()`. That call waits on a
    /// termination notification, and if the child exits between an
    /// `isRunning` check and the call, the notification can already be gone
    /// and it blocks forever. Reproduced at 3 of 8 full test-suite runs on
    /// /usr/bin/true, which exits faster than we can get here.
    /// `terminationHandler` would be the tidier signal but is not ours to
    /// take: PythonService and LlamaService set one for their restart logic
    /// and also call this helper, so assigning here would clobber it.
    ///
    /// Every exit from the loop is on a deadline or an observed exit: there is
    /// no path that waits on the process alone.
    static func pollUntilExited(
        isRunning: () -> Bool,
        sendKill: () -> Void,
        graceSeconds: TimeInterval,
        killWaitSeconds: TimeInterval,
        serviceName: String,
        pollInterval: useconds_t = 20_000,
    ) -> Bool {
        let killAt = Date().addingTimeInterval(graceSeconds)
        let giveUpAt = killAt.addingTimeInterval(killWaitSeconds)
        var killed = false
        while isRunning() {
            let now = Date()
            if !killed, now >= killAt {
                Log.logger("lifecycle").warning(
                    "[\(serviceName, privacy: .public)] Still running after \(Int(graceSeconds), privacy: .public)s — sending SIGKILL"
                )
                sendKill()
                killed = true
            } else if killed, now >= giveUpAt {
                Log.logger("lifecycle").error(
                    "[\(serviceName, privacy: .public)] Still reported running \(Int(killWaitSeconds), privacy: .public)s after SIGKILL — giving up the wait"
                )
                return false
            }
            usleep(pollInterval)
        }
        return true
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
    /// Implementation: `setpgid()` on the child from the parent immediately
    /// after `run()` returns. Measured 2026-09-28 (LlamaServiceStopTests pins
    /// it): Foundation's `Process.run()` on macOS already spawns the child as
    /// its own process-group leader (`getpgid(pid) == pid`), so the setpgid
    /// here is a no-op and the "grandchild inherits the old pgid" race below
    /// cannot occur through `Process`. It stays because the guarantee is
    /// Foundation's, not documented, and this is where a change would be
    /// caught (the warning fires). Earlier text here said `run()` did not
    /// make the child a leader; that was assumed, not measured.
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

/// The wait a stop runs on the process it signalled: grace, SIGKILL, bounded
/// wait; returns whether the exit was seen. Every service that stops a child
/// takes one as its `waitForExit` seam, defaulting to `Process.realExitWait`,
/// so a test can stand in a wait that gives up, which no real child can be
/// made to cause on demand.
typealias ExitWait = (_ proc: Process, _ grace: TimeInterval, _ name: String, _ killWait: TimeInterval) async -> Bool

/// Whether a pid names a live process, asked of the kernel rather than of
/// Foundation's bookkeeping.
enum ProcessLiveness {
    /// Gone if `kill(pid, 0)` fails with ESRCH (no such process: exited and
    /// reaped), or if the process is a zombie (exited, not yet reaped by
    /// Foundation). Alive otherwise, including EPERM (it exists, it is not
    /// ours to signal). Never reaps: a `waitpid` here would take the exit
    /// status Foundation is waiting to collect.
    ///
    /// The zombie check reads `p_stat` through `sysctl(KERN_PROC_PID)`.
    /// `proc_pidinfo(PROC_PIDTBSDINFO)` cannot do it: measured on macOS 27, it
    /// fails with ESRCH on a zombie, while `kill(pid, 0)` still succeeds.
    static func isAlive(_ pid: pid_t) -> Bool {
        guard pid > 0 else { return false }
        if kill(pid, 0) != 0 {
            return errno != ESRCH
        }
        var kp = kinfo_proc()
        var len = MemoryLayout<kinfo_proc>.size
        var mib: [Int32] = [CTL_KERN, KERN_PROC, KERN_PROC_PID, pid]
        if sysctl(&mib, 4, &kp, &len, nil, 0) == 0, len > 0, kp.kp_proc.p_stat == SZOMB {
            return false
        }
        return true
    }
}

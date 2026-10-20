import Foundation
import os

/// Base class for Python subprocess services (API, Worker, Embedder).
class PythonService: ManagedService {
    let name: String
    var state: ServiceState = .stopped
    var process: Process?
    var holdsLiveProcess: Bool { process?.isRunningAndAlive == true }
    var baseEnvironment: [String: String] = [:]
    /// The settings a service's own environment and health check read. A test
    /// points it at a config file of its own; the default is the live app's.
    var settingsOverride: AppSettings?
    var settings: AppSettings { settingsOverride ?? AppSettings.shared }
    /// Spawns the configured process. A test replaces it to capture the
    /// process `start()` built (its environment above all) without spawning.
    var launcher: (Process) throws -> Void = { try $0.runAsProcessGroupLeader() }

    /// Seconds to wait after SIGTERM before sending SIGKILL.
    /// API server needs more time when mid-research (SSE streams, agent threads, DB sessions).
    var shutdownGracePeriod: TimeInterval = 30.0
    /// The wait `stop()` (and `ServiceManager.stopWorkersInParallel`) runs on
    /// the signalled process; a test swaps in one that gives up (#890).
    var waitForExit: ExitWait = Process.realExitWait
    /// Called after process exits unexpectedly and internal restarts are exhausted.
    var onUnexpectedExit: (@MainActor () -> Void)?

    private var restartCount = 0
    private let maxRestarts = 3

    init(name: String) {
        self.name = name
    }

    /// Subclasses override to provide the executable and arguments.
    var executableName: String { fatalError("Subclasses must override") }
    var arguments: [String] { [] }
    var extraEnvironment: [String: String] { [:] }

    func start() async throws {
        let bundle = Bundle.main.resourceURL!
        let venvBin = bundle.appendingPathComponent("venv/bin")
        let executable = venvBin.appendingPathComponent(executableName)

        let proc = Process()
        proc.executableURL = executable
        proc.arguments = arguments

        proc.environment = launchEnvironment()

        let category = name.lowercased()
        let pipe = Log.createPipe(category: category)
        proc.standardOutput = pipe
        proc.standardError = pipe

        let serviceLogger = Log.logger(category)
        proc.terminationHandler = { [weak self] p in
            guard let self = self else { return }
            Task { @MainActor in
                await self.processExited(p, logger: serviceLogger)
            }
        }

        try launcher(proc)
        process = proc
    }

    /// An exit of `p` that no stop asked for: restart, or after `maxRestarts`
    /// mark the service errored. Only the process this service tracks counts:
    /// a stop that gave up its wait (#890) can see that process's exit land
    /// later, after a successor is `running`, and acting on it would restart
    /// the healthy successor.
    @MainActor
    func processExited(_ p: Process, logger: Logger) async {
        guard process === p else { return }
        if state == .running && restartCount < maxRestarts {
            restartCount += 1
            let delay = pow(2.0, Double(restartCount))
            logger.error("Process exited (\(p.terminationStatus, privacy: .public)), restarting in \(Int(delay), privacy: .public)s (attempt \(self.restartCount, privacy: .public)/\(self.maxRestarts, privacy: .public))")
            try? await Task.sleep(for: .seconds(delay))
            try? await start()
        } else if state == .running {
            state = .errored
            logger.error("Process exited, max restarts reached")
            onUnexpectedExit?()
        }
    }

    /// The environment the process is launched with: the shared base dict,
    /// then this service's overrides, then the runtime cache variables, last,
    /// so neither of the others can point a cache back into the signed bundle
    /// (#846).
    func launchEnvironment() -> [String: String] {
        var env = baseEnvironment
        for (k, v) in extraEnvironment {
            env[k] = v
        }
        return ServiceManager.applyingRuntimeCacheRules(to: env)
    }

    /// Reset the restart counter. Called by ServiceManager after health check passes.
    func resetRestartCount() {
        restartCount = 0
    }

    func stop() async {
        state = .stopping
        guard let proc = process, proc.isRunning else {
            state = .stopped
            return
        }
        proc.terminate() // SIGTERM
        // Use the shared deadline helper — detaches pipes (defusing the
        // Pipe+waitUntilExit deadlock described in
        // project_menubar_process_management_audit.md), schedules SIGKILL
        // after the grace period, then waits.
        let exited = await waitForExit(proc, shutdownGracePeriod, name, Process.defaultKillWaitSeconds)
        settleStop(exited: exited)
    }

    /// The end of a stop that signalled the tracked process and waited on it.
    /// Seen to exit: untrack it and report `stopped`. Not seen to exit (the
    /// wait gave up, #890): keep it tracked so `holdsLiveProcess` and the
    /// force-stop pass still see it, and report `errored`, which the relaunch
    /// paths (`stopForRelaunch`) send back through `stop()`; `stopped` would
    /// let a start spawn a second one beside it. Also the end of the parallel
    /// worker stops in `ServiceManager`.
    func settleStop(exited: Bool) {
        if exited {
            process = nil
            state = .stopped
        } else {
            state = .errored
        }
    }

    func healthCheck() async -> Bool {
        // Default: no health check, subclasses override
        return process?.isRunning == true
    }
}

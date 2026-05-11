import Foundation
import os

/// Periodically polls service health checks and updates states.
@MainActor
final class HealthChecker {
    private let serviceManager: ServiceManager
    private var timer: Timer?
    private let interval: TimeInterval = 10
    /// Set to true to skip health checks during targeted restarts.
    var paused = false
    /// Per-service pause — used during auto-restart to avoid re-triggering.
    var pausedServices: Set<String> = []

    /// Consecutive health check failure count per service name.
    private var failureCounts: [String: Int] = [:]
    /// Number of consecutive failures before marking a service as errored.
    /// Set to 6 (60s) to avoid false positives when API is busy with research/synthesis.
    private let consecutiveFailuresBeforeError = 6 // 60s at 10s interval

    /// Tasks for auto-restarts dispatched by `checkAll()`. Tracked so
    /// `ServiceManager.stopAll()` and `restartForChangedSettings()` can
    /// call `cancelInFlightRestarts()` before they begin mutating
    /// service state — without this, a restart Task that already
    /// passed its `.shutdownPending` gate could call `start()` on a
    /// service the orchestrator already moved on from, leaving an
    /// orphaned child the menubar's `Process` ref doesn't track.
    private var taskHandles: [UUID: Task<Void, Never>] = [:]

    /// Test-only: number of in-flight auto-restart Tasks.
    var inFlightTaskCount: Int { taskHandles.count }

    /// Test-only entry point: synchronously runs one health-check pass.
    func tickForTesting() async { await checkAll() }

    init(serviceManager: ServiceManager) {
        self.serviceManager = serviceManager
    }

    func startPolling() {
        timer = Timer.scheduledTimer(withTimeInterval: interval, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in
                await self?.checkAll()
            }
        }
    }

    func stopPolling() {
        timer?.invalidate()
        timer = nil
    }

    private func checkAll() async {
        guard !paused else { return }
        var changed = false
        for service in serviceManager.services {
            guard service.state == .running else {
                // Reset stale counters for non-running services
                failureCounts[service.name] = nil
                continue
            }
            guard !pausedServices.contains(service.name) else { continue }

            let healthy = await service.healthCheck()
            // Re-check after await — state may have changed during shutdown
            guard service.state == .running else {
                failureCounts[service.name] = nil
                continue
            }
            if healthy {
                failureCounts[service.name] = nil
            } else {
                let count = (failureCounts[service.name] ?? 0) + 1
                failureCounts[service.name] = count
                Log.logger("health").warning(
                    "[\(service.name, privacy: .public)] Health check failed (\(count, privacy: .public)/\(self.consecutiveFailuresBeforeError, privacy: .public))")
                if count >= self.consecutiveFailuresBeforeError {
                    service.state = .errored
                    failureCounts[service.name] = nil
                    changed = true
                    Log.logger("health").error(
                        "[\(service.name, privacy: .public)] \(self.consecutiveFailuresBeforeError, privacy: .public) consecutive failures — marked errored")
                    // Dispatch the auto-restart as a tracked Task instead
                    // of awaiting inline. See `taskHandles` doc for the
                    // race this closes. `defer` can't directly mutate
                    // MainActor-isolated state, so the cleanup hop is
                    // wrapped in a Task — safe even if the parent is
                    // cancelled, because cancellation propagates inside
                    // `attemptAutoRestart`'s awaits, not the cleanup hop.
                    let id = UUID()
                    let svc = service
                    let sm = serviceManager
                    taskHandles[id] = Task { @MainActor in
                        defer { Task { @MainActor in self.taskHandles[id] = nil } }
                        await sm.attemptAutoRestart(svc)
                    }
                }
            }
        }
        if changed {
            serviceManager.notifyStateChanged()
        }
    }
}

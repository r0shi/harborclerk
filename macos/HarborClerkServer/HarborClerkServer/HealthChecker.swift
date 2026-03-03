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
    private let consecutiveFailuresBeforeError = 3 // 30s at 10s interval

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
                    await serviceManager.attemptAutoRestart(service)
                }
            }
        }
        if changed {
            serviceManager.notifyStateChanged()
        }
    }
}

import Foundation
import os

/// Periodically polls service health checks and updates states.
@MainActor
final class HealthChecker {
    private let serviceManager: ServiceManager
    private var timer: Timer?
    private let interval: TimeInterval = 10

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
        var changed = false
        for service in serviceManager.services {
            guard service.state == .running else { continue }
            let healthy = await service.healthCheck()
            if !healthy {
                service.state = .errored
                changed = true
                Log.logger("health").error("[\(service.name, privacy: .public)] Health check failed")
            }
        }
        if changed {
            serviceManager.notifyStateChanged()
        }
    }
}

import Foundation

/// Captures stdout/stderr from subprocesses into a ring buffer.
/// Publishes objectWillChange at most every 250ms to avoid saturating the main thread.
final class LogManager: ObservableObject {
    static let shared = LogManager()

    private(set) var lines: [LogLine] = []

    private let maxLines = 2000
    private let lock = NSLock()
    private var pendingNotify: DispatchWorkItem?
    private let throttleInterval: TimeInterval = 0.25

    struct LogLine: Identifiable {
        let id = UUID()
        let timestamp: Date
        let service: String
        let text: String
    }

    func append(service: String, text: String) {
        let newLines = text.components(separatedBy: .newlines)
            .filter { !$0.isEmpty }
            .map { LogLine(timestamp: Date(), service: service, text: $0) }

        let update = { [self] in
            lock.lock()
            lines.append(contentsOf: newLines)
            if lines.count > maxLines {
                lines.removeFirst(lines.count - maxLines)
            }
            lock.unlock()
            scheduleNotify()
        }

        if Thread.isMainThread {
            update()
        } else {
            DispatchQueue.main.async(execute: update)
        }
    }

    func clear() {
        lock.lock()
        lines.removeAll()
        lock.unlock()
        // Immediate notify for user-initiated action
        pendingNotify?.cancel()
        pendingNotify = nil
        objectWillChange.send()
    }

    /// Set up a pipe to capture output from a Process and feed it to the log.
    func createPipe(service: String) -> Pipe {
        let pipe = Pipe()
        pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else { return }
            self?.append(service: service, text: text)
        }
        return pipe
    }

    // MARK: - Throttled Notify

    private func scheduleNotify() {
        // Already have a pending notification — coalesce
        if pendingNotify != nil { return }

        let work = DispatchWorkItem { [weak self] in
            self?.pendingNotify = nil
            self?.objectWillChange.send()
        }
        pendingNotify = work
        DispatchQueue.main.asyncAfter(deadline: .now() + throttleInterval, execute: work)
    }
}

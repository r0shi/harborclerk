import Foundation

/// Captures stdout/stderr from subprocesses into a ring buffer.
@MainActor
final class LogManager: ObservableObject {
    static let shared = LogManager()

    @Published var lines: [LogLine] = []

    private let maxLines = 2000

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

        lines.append(contentsOf: newLines)
        if lines.count > maxLines {
            lines.removeFirst(lines.count - maxLines)
        }
    }

    func clear() {
        lines.removeAll()
    }

    /// Set up a pipe to capture output from a Process and feed it to the log.
    nonisolated func createPipe(service: String) -> Pipe {
        let pipe = Pipe()
        pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else { return }
            Task { @MainActor [weak self] in
                self?.append(service: service, text: text)
            }
        }
        return pipe
    }
}

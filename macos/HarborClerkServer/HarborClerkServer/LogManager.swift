import Foundation
import os

enum Log {
    static let subsystem = "com.harborclerk.server"

    static func logger(_ category: String) -> Logger {
        Logger(subsystem: subsystem, category: category)
    }

    /// Creates a Pipe that forwards subprocess output line-by-line to os.Logger.
    static func createPipe(category: String) -> Pipe {
        let logger = Logger(subsystem: subsystem, category: category)
        let pipe = Pipe()
        pipe.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else { return }
            for line in text.components(separatedBy: .newlines) where !line.isEmpty {
                logger.info("\(line, privacy: .public)")
            }
        }
        return pipe
    }
}

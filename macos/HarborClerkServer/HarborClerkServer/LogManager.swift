import Foundation
import os

enum Log {
    static let subsystem = "com.harborclerk.server"

    static func logger(_ category: String) -> Logger {
        Logger(subsystem: subsystem, category: category)
    }

    /// Creates a Pipe that forwards subprocess output line-by-line to os.Logger.
    ///
    /// Some native subprocesses (notably Caddy and llama-server) do not use
    /// Harbor Clerk's Python logging setup, so they need Swift-side file
    /// capture for the Service Logs page. When `fileURL` is provided, raw
    /// subprocess bytes are appended to that file as well.
    static func createPipe(category: String, fileURL: URL? = nil) -> Pipe {
        let logger = Logger(subsystem: subsystem, category: category)
        let pipe = Pipe()
        let fileHandle = openLogFile(fileURL)
        pipe.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            guard !data.isEmpty else {
                // EOF: nil out handler so the pipe fully closes.
                // Without this, waitUntilExit() deadlocks because it waits
                // for the pipe's read end to close.
                handle.readabilityHandler = nil
                try? fileHandle?.close()
                return
            }
            if let fileHandle {
                try? fileHandle.write(contentsOf: data)
            }
            guard let text = String(data: data, encoding: .utf8) else { return }
            for line in text.components(separatedBy: .newlines) where !line.isEmpty {
                logger.info("\(line, privacy: .public)")
            }
        }
        return pipe
    }

    private static func openLogFile(_ fileURL: URL?) -> FileHandle? {
        guard let fileURL else { return nil }
        do {
            try FileManager.default.createDirectory(
                at: fileURL.deletingLastPathComponent(),
                withIntermediateDirectories: true
            )
            rotateLogIfNeeded(fileURL, maxBytes: 5 * 1024 * 1024, keep: 3)
            if !FileManager.default.fileExists(atPath: fileURL.path) {
                FileManager.default.createFile(atPath: fileURL.path, contents: nil)
            }
            let handle = try FileHandle(forWritingTo: fileURL)
            try handle.seekToEnd()
            return handle
        } catch {
            logger(categoryForFile(fileURL)).warning(
                "Could not open subprocess log file \(fileURL.path, privacy: .public): \(error.localizedDescription, privacy: .public)"
            )
            return nil
        }
    }

    private static func rotateLogIfNeeded(_ fileURL: URL, maxBytes: UInt64, keep: Int) {
        guard let size = (try? FileManager.default.attributesOfItem(atPath: fileURL.path)[.size]) as? UInt64,
              size >= maxBytes else {
            return
        }

        for index in stride(from: keep, through: 1, by: -1) {
            let source = index == 1 ? fileURL : URL(fileURLWithPath: "\(fileURL.path).\(index - 1)")
            let destination = URL(fileURLWithPath: "\(fileURL.path).\(index)")
            if FileManager.default.fileExists(atPath: destination.path) {
                try? FileManager.default.removeItem(at: destination)
            }
            if FileManager.default.fileExists(atPath: source.path) {
                try? FileManager.default.moveItem(at: source, to: destination)
            }
        }
    }

    private static func categoryForFile(_ fileURL: URL?) -> String {
        guard let fileURL else { return "logs" }
        return fileURL.deletingPathExtension().lastPathComponent
    }
}

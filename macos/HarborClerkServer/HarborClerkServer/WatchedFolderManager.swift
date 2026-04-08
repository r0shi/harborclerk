import CryptoKit
import Foundation
import UniformTypeIdentifiers
import os

// MARK: - WatchedFolderManager

/// Manages FSEventStream instances for watched folders and communicates
/// file changes to the Python API.
@MainActor
final class WatchedFolderManager {
    static let shared = WatchedFolderManager()

    private var watchers: [UUID: FolderWatcher] = [:]
    private var apiBaseURL: URL?
    private var authToken: String?
    private var allowedExtensions: Set<String> = []
    fileprivate var isRunning = false

    /// Batch size for initial directory scans.
    private let scanBatchSize = 20
    /// Delay between batches during initial scan (100ms).
    private let scanBatchDelay: TimeInterval = 0.1

    private init() {}

    // MARK: - Lifecycle

    /// Start watching all enabled folders. Called after API is confirmed healthy.
    func start(apiBaseURL: URL, authToken: String) {
        self.apiBaseURL = apiBaseURL
        self.authToken = authToken
        isRunning = true

        Task {
            await fetchAllowedExtensions()
            await fetchAndStartWatchers()
        }
    }

    /// Stop all watchers and reset state.
    func stop() {
        isRunning = false
        for (_, watcher) in watchers {
            watcher.stop()
        }
        watchers.removeAll()
        apiBaseURL = nil
        authToken = nil
    }

    // MARK: - Auth Token Generation

    /// Mint a short-lived JWT for API calls using the shared secret key.
    /// Uses a synthetic service user UUID so the token passes `require_user`.
    static func mintServiceToken() -> String? {
        let secretKey = AppSettings.shared.secretKey
        guard !secretKey.isEmpty else { return nil }

        // Synthetic service user UUID (deterministic, not a real DB user — but
        // the watch endpoints only need `require_user`, which just checks JWT validity)
        let serviceUserId = "00000000-0000-0000-0000-000000000001"

        let now = Int(Date().timeIntervalSince1970)
        let exp = now + 3600 // 1 hour

        let header = Self.base64url(json: ["alg": "HS256", "typ": "JWT"])
        let payload = Self.base64url(json: [
            "sub": serviceUserId,
            "role": "admin",
            "type": "access",
            "exp": exp,
        ] as [String: Any])

        let signingInput = "\(header).\(payload)"
        guard let keyData = secretKey.data(using: .utf8),
              let inputData = signingInput.data(using: .utf8) else { return nil }

        let key = SymmetricKey(data: keyData)
        let signature = HMAC<SHA256>.authenticationCode(for: inputData, using: key)
        let sig = Data(signature).base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")

        return "\(signingInput).\(sig)"
    }

    private static func base64url(json: [String: Any]) -> String {
        guard let data = try? JSONSerialization.data(withJSONObject: json) else { return "" }
        return data.base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }

    // MARK: - API Communication

    /// Fetch allowed file extensions from the API.
    private func fetchAllowedExtensions() async {
        guard let url = effectiveAPIBase?.appendingPathComponent("api/watch/allowed-extensions") else { return }

        do {
            let (data, _) = try await apiRequest(url: url, method: "GET")
            // API returns a JSON array of extensions with dots: [".pdf", ".docx", ...]
            if let extensions = try? JSONSerialization.jsonObject(with: data) as? [String] {
                // Strip leading dots for comparison with URL.pathExtension
                allowedExtensions = Set(extensions.map { $0.hasPrefix(".") ? String($0.dropFirst()) : $0 })
                Log.logger("watcher").info("Loaded \(extensions.count, privacy: .public) allowed extensions")
            }
        } catch {
            Log.logger("watcher").error("Failed to fetch allowed extensions: \(error.localizedDescription, privacy: .public)")
            // Fallback: common document extensions (without dots)
            allowedExtensions = Set(["pdf", "doc", "docx", "txt", "md", "csv", "xlsx", "pptx",
                                     "odt", "rtf", "epub", "html", "eml", "jpg", "jpeg", "png", "tiff"])
        }
    }

    /// Fetch folders from the API and start watchers for enabled ones.
    private func fetchAndStartWatchers() async {
        guard let url = effectiveAPIBase?.appendingPathComponent("api/watch/folders") else { return }

        do {
            let (data, _) = try await apiRequest(url: url, method: "GET")
            guard let folders = try? JSONSerialization.jsonObject(with: data) as? [[String: Any]] else { return }

            for folder in folders {
                guard let idStr = folder["folder_id"] as? String,
                      let id = UUID(uuidString: idStr),
                      let path = folder["path"] as? String,
                      let enabled = folder["enabled"] as? Bool,
                      enabled else { continue }

                let recursive = folder["recursive"] as? Bool ?? true
                let lastEventId = folder["last_event_id"] as? UInt64 ?? FSEventsGetCurrentEventId()

                startWatcher(id: id, path: path, recursive: recursive, lastEventId: lastEventId)
            }
        } catch {
            Log.logger("watcher").error("Failed to fetch watched folders: \(error.localizedDescription, privacy: .public)")
        }
    }

    // MARK: - Watcher Management

    /// Start an FSEventStream watcher for a single folder.
    func startWatcher(id: UUID, path: String, recursive: Bool, lastEventId: UInt64) {
        // Stop existing watcher for this folder if any
        watchers[id]?.stop()

        let watcher = FolderWatcher(
            folderId: id,
            path: path,
            recursive: recursive,
            lastEventId: lastEventId,
            delegate: self
        )
        watchers[id] = watcher
        watcher.start()
        Log.logger("watcher").info("Started watching: \(path, privacy: .public)")
    }

    /// Stop the watcher for a specific folder.
    func stopWatcher(id: UUID) {
        watchers[id]?.stop()
        watchers.removeValue(forKey: id)
    }

    // MARK: - Initial Scan

    /// Recursively scan a directory and ingest files in batches.
    func performInitialScan(folderId: UUID, path: String, recursive: Bool) async {
        let baseURL = URL(fileURLWithPath: path)
        let fm = FileManager.default

        var files: [URL] = []
        if let enumerator = fm.enumerator(
            at: baseURL,
            includingPropertiesForKeys: [.isRegularFileKey, .contentTypeKey, .isDirectoryKey],
            options: recursive ? [.skipsHiddenFiles] : [.skipsHiddenFiles, .skipsSubdirectoryDescendants]
        ) {
            for case let fileURL as URL in enumerator {
                guard let values = try? fileURL.resourceValues(forKeys: [.isRegularFileKey]),
                      values.isRegularFile == true else { continue }
                if isAllowedFile(url: fileURL) {
                    files.append(fileURL)
                }
            }
        }

        Log.logger("watcher").info("Initial scan of \(path, privacy: .public): \(files.count, privacy: .public) files")

        // Process in batches
        for batchStart in stride(from: 0, to: files.count, by: scanBatchSize) {
            let batchEnd = min(batchStart + scanBatchSize, files.count)
            let batch = Array(files[batchStart..<batchEnd])

            for fileURL in batch {
                await ingestFile(folderId: folderId, basePath: path, fileURL: fileURL)
            }

            if batchEnd < files.count {
                try? await Task.sleep(for: .milliseconds(Int(scanBatchDelay * 1000)))
            }
        }
    }

    // MARK: - File Type Detection

    /// Check if a file is allowed based on UTType or extension fallback.
    func isAllowedFile(url: URL) -> Bool {
        // Try UTType first
        if let values = try? url.resourceValues(forKeys: [.contentTypeKey]),
           let contentType = values.contentType {
            let ext = contentType.preferredFilenameExtension ?? url.pathExtension
            return allowedExtensions.contains(ext.lowercased())
        }

        // Fall back to extension
        let ext = url.pathExtension.lowercased()
        return !ext.isEmpty && allowedExtensions.contains(ext)
    }

    // MARK: - File Hashing

    /// Compute SHA256 of a file using CryptoKit.
    static func sha256(of url: URL) -> String? {
        guard let data = try? Data(contentsOf: url) else { return nil }
        let digest = SHA256.hash(data: data)
        return digest.map { String(format: "%02x", $0) }.joined()
    }

    // MARK: - Bookmark Management

    /// Create a security-scoped bookmark for a file URL.
    static func createBookmark(for url: URL) -> Data? {
        try? url.bookmarkData(
            options: [.withSecurityScope],
            includingResourceValuesForKeys: nil,
            relativeTo: nil
        )
    }

    /// Resolve a bookmark back to a URL.
    static func resolveBookmark(_ data: Data) -> URL? {
        var isStale = false
        guard let url = try? URL(
            resolvingBookmarkData: data,
            options: [.withSecurityScope],
            relativeTo: nil,
            bookmarkDataIsStale: &isStale
        ) else { return nil }

        if isStale {
            Log.logger("watcher").warning("Stale bookmark resolved to: \(url.path, privacy: .public)")
        }
        return url
    }

    // MARK: - API Calls (fileprivate for FolderWatcher access)

    /// Resolve the API base URL — prefer the configured one, fall back to localhost.
    private var effectiveAPIBase: URL? {
        apiBaseURL ?? URL(string: "http://localhost:\(AppSettings.shared.apiPort)")
    }

    /// Notify the API about a new or modified file.
    fileprivate func ingestFile(folderId: UUID, basePath: String, fileURL: URL) async {
        guard let apiBase = effectiveAPIBase else { return }

        let relativePath = String(fileURL.path.dropFirst(basePath.count))
            .trimmingCharacters(in: CharacterSet(charactersIn: "/"))

        guard let sha256 = Self.sha256(of: fileURL) else {
            Log.logger("watcher").warning("Cannot hash file: \(fileURL.path, privacy: .public)")
            return
        }

        let bookmarkData = Self.createBookmark(for: fileURL)
        let bookmarkBase64 = bookmarkData?.base64EncodedString() ?? ""

        // Detect MIME type
        let mimeType = Self.mimeType(for: fileURL)

        let body: [String: Any] = [
            "folder_id": folderId.uuidString,
            "relative_path": relativePath,
            "sha256": sha256,
            "bookmark_data": bookmarkBase64,
            "source_path": fileURL.path,
            "mime_type": mimeType,
        ]

        let url = apiBase.appendingPathComponent("api/watch/ingest")
        do {
            let _ = try await apiRequest(url: url, method: "POST", body: body)
            Log.logger("watcher").debug("Ingested: \(relativePath, privacy: .public)")
        } catch {
            Log.logger("watcher").error("Ingest failed for \(relativePath, privacy: .public): \(error.localizedDescription, privacy: .public)")
        }
    }

    /// Notify the API about a removed file.
    fileprivate func removeFile(folderId: UUID, basePath: String, relativePath: String) async {
        guard let apiBase = effectiveAPIBase else { return }

        let body: [String: Any] = [
            "folder_id": folderId.uuidString,
            "relative_path": relativePath,
        ]

        let url = apiBase.appendingPathComponent("api/watch/remove")
        do {
            let _ = try await apiRequest(url: url, method: "POST", body: body)
            Log.logger("watcher").debug("Removed: \(relativePath, privacy: .public)")
        } catch {
            Log.logger("watcher").error("Remove failed for \(relativePath, privacy: .public): \(error.localizedDescription, privacy: .public)")
        }
    }

    /// Notify the API about a renamed file.
    fileprivate func renameFile(folderId: UUID, basePath: String, oldRelativePath: String, newRelativePath: String, fileURL: URL) async {
        guard let apiBase = effectiveAPIBase else { return }

        let bookmarkData = Self.createBookmark(for: fileURL)
        let bookmarkBase64 = bookmarkData?.base64EncodedString() ?? ""

        let body: [String: Any] = [
            "folder_id": folderId.uuidString,
            "old_relative_path": oldRelativePath,
            "new_relative_path": newRelativePath,
            "bookmark_data": bookmarkBase64,
            "source_path": fileURL.path,
        ]

        let url = apiBase.appendingPathComponent("api/watch/rename")
        do {
            let _ = try await apiRequest(url: url, method: "POST", body: body)
            Log.logger("watcher").debug("Renamed: \(oldRelativePath, privacy: .public) -> \(newRelativePath, privacy: .public)")
        } catch {
            Log.logger("watcher").error("Rename failed: \(error.localizedDescription, privacy: .public)")
        }
    }

    /// Update last_event_id for a folder.
    fileprivate func updateLastEventId(folderId: UUID, eventId: UInt64) async {
        guard let apiBase = effectiveAPIBase else { return }

        let body: [String: Any] = ["last_event_id": Int(eventId)]
        let url = apiBase.appendingPathComponent("api/watch/folders/\(folderId.uuidString)")

        do {
            let _ = try await apiRequest(url: url, method: "PATCH", body: body)
        } catch {
            Log.logger("watcher").error("Failed to update last_event_id: \(error.localizedDescription, privacy: .public)")
        }
    }

    // MARK: - MIME Type Detection

    /// Detect MIME type using UTType with fallback.
    static func mimeType(for url: URL) -> String {
        if let values = try? url.resourceValues(forKeys: [.contentTypeKey]),
           let contentType = values.contentType,
           let mime = contentType.preferredMIMEType {
            return mime
        }

        // Extension-based fallback
        let ext = url.pathExtension.lowercased()
        if let uttype = UTType(filenameExtension: ext),
           let mime = uttype.preferredMIMEType {
            return mime
        }

        return "application/octet-stream"
    }

    // MARK: - HTTP Helper

    private func apiRequest(url: URL, method: String, body: [String: Any]? = nil) async throws -> (Data, URLResponse) {
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")

        // Use current token or mint a fresh one
        let token = authToken ?? Self.mintServiceToken() ?? ""
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")

        if let body {
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
        }

        return try await URLSession.shared.data(for: request)
    }
}

// MARK: - FolderWatcher

/// Wraps an FSEventStream for a single watched directory.
/// Must call `stop()` explicitly on the main thread before releasing.
final class FolderWatcher {
    let folderId: UUID
    let path: String
    let recursive: Bool
    private var lastEventId: UInt64
    private weak var delegate: WatchedFolderManager?

    private var streamRef: FSEventStreamRef?
    /// Tracks recently-seen paths for rename coalescing.
    private var recentRenames: [String: Date] = [:]

    init(folderId: UUID, path: String, recursive: Bool, lastEventId: UInt64, delegate: WatchedFolderManager) {
        self.folderId = folderId
        self.path = path
        self.recursive = recursive
        self.lastEventId = lastEventId
        self.delegate = delegate
    }

    func start() {
        let pathCF = path as CFString
        let pathsToWatch = [pathCF] as CFArray

        var context = FSEventStreamContext()
        context.info = Unmanaged.passUnretained(self).toOpaque()

        let flags: FSEventStreamCreateFlags = UInt32(
            kFSEventStreamCreateFlagUseCFTypes |
            kFSEventStreamCreateFlagFileEvents |
            kFSEventStreamCreateFlagNoDefer
        )

        guard let stream = FSEventStreamCreate(
            nil,
            FolderWatcher.eventCallback,
            &context,
            pathsToWatch,
            lastEventId,
            1.0, // latency in seconds
            flags
        ) else {
            Log.logger("watcher").error("Failed to create FSEventStream for \(self.path, privacy: .public)")
            return
        }

        streamRef = stream
        FSEventStreamScheduleWithRunLoop(stream, CFRunLoopGetMain(), CFRunLoopMode.defaultMode.rawValue)
        FSEventStreamStart(stream)
    }

    func stop() {
        guard let stream = streamRef else { return }
        FSEventStreamStop(stream)
        FSEventStreamInvalidate(stream)
        FSEventStreamRelease(stream)
        streamRef = nil
    }

    // MARK: - FSEvents Callback

    private static let eventCallback: FSEventStreamCallback = {
        (streamRef, clientCallbackInfo, numEvents, eventPaths, eventFlags, eventIds) in

        guard let info = clientCallbackInfo else { return }
        let watcher = Unmanaged<FolderWatcher>.fromOpaque(info).takeUnretainedValue()

        guard let paths = unsafeBitCast(eventPaths, to: NSArray.self) as? [String] else { return }
        let flags = UnsafeBufferPointer(start: eventFlags, count: numEvents)
        let ids = UnsafeBufferPointer(start: eventIds, count: numEvents)

        var events: [(path: String, flags: FSEventStreamEventFlags, id: UInt64)] = []
        for i in 0..<numEvents {
            events.append((path: paths[i], flags: flags[i], id: ids[i]))
        }

        let folderId = watcher.folderId
        let basePath = watcher.path

        Task { @MainActor in
            guard let delegate = watcher.delegate, delegate.isRunning else { return }

            for event in events {
                await watcher.handleEvent(
                    path: event.path,
                    flags: event.flags,
                    eventId: event.id,
                    delegate: delegate,
                    basePath: basePath,
                    folderId: folderId
                )
            }

            // Update last event ID to the highest seen
            if let maxId = events.map(\.id).max() {
                watcher.lastEventId = maxId
                await delegate.updateLastEventId(folderId: folderId, eventId: maxId)
            }
        }
    }

    @MainActor
    private func handleEvent(
        path eventPath: String,
        flags: FSEventStreamEventFlags,
        eventId: UInt64,
        delegate: WatchedFolderManager,
        basePath: String,
        folderId: UUID
    ) async {
        let url = URL(fileURLWithPath: eventPath)

        // Skip directories
        let isDir = (flags & UInt32(kFSEventStreamEventFlagItemIsDir)) != 0
        if isDir { return }

        // Skip hidden files
        if url.lastPathComponent.hasPrefix(".") { return }

        let isCreated = (flags & UInt32(kFSEventStreamEventFlagItemCreated)) != 0
        let isModified = (flags & UInt32(kFSEventStreamEventFlagItemModified)) != 0
        let isRemoved = (flags & UInt32(kFSEventStreamEventFlagItemRemoved)) != 0
        let isRenamed = (flags & UInt32(kFSEventStreamEventFlagItemRenamed)) != 0

        let relativePath = String(eventPath.dropFirst(basePath.count))
            .trimmingCharacters(in: CharacterSet(charactersIn: "/"))

        if isRemoved {
            await delegate.removeFile(folderId: folderId, basePath: basePath, relativePath: relativePath)
            return
        }

        if isRenamed {
            // FSEvents fires rename for both old and new paths.
            // If the file exists at this path, it's the "new" side of the rename.
            if FileManager.default.fileExists(atPath: eventPath) {
                // Check if we have a recent rename event for a missing file — that's the "old" side
                let cutoff = Date().addingTimeInterval(-2.0)
                let oldPath = recentRenames.first { $0.value > cutoff && !FileManager.default.fileExists(atPath: basePath + "/" + $0.key) }
                if let (oldRelativePath, _) = oldPath {
                    recentRenames.removeValue(forKey: oldRelativePath)
                    if delegate.isAllowedFile(url: url) {
                        await delegate.renameFile(
                            folderId: folderId,
                            basePath: basePath,
                            oldRelativePath: oldRelativePath,
                            newRelativePath: relativePath,
                            fileURL: url
                        )
                    }
                } else if delegate.isAllowedFile(url: url) {
                    // No matching old path found — treat as new file
                    await delegate.ingestFile(folderId: folderId, basePath: basePath, fileURL: url)
                }
            } else {
                // File doesn't exist — this is the "old" side of a rename
                recentRenames[relativePath] = Date()
                // Clean up old entries
                let cutoff = Date().addingTimeInterval(-5.0)
                recentRenames = recentRenames.filter { $0.value > cutoff }
            }
            return
        }

        if isCreated || isModified {
            guard delegate.isAllowedFile(url: url) else { return }
            await delegate.ingestFile(folderId: folderId, basePath: basePath, fileURL: url)
        }
    }

}

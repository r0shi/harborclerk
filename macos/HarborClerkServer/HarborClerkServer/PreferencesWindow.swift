import SwiftUI
import UniformTypeIdentifiers

private let defaultPorts: [String: Int] = [
    "api": 8100,
    "postgres": 5433,
    "tika": 9998,
    "embedder": 8101,
    "llama": 8102,
]

private let modelOptions: [(id: String, name: String)] = [
    ("", "None"),
    ("qwen3-8b", "Qwen3 8B (5.0 GB)"),
    ("qwen3-4b", "Qwen3 4B (2.5 GB)"),
    ("phi4-mini", "Phi-4 Mini 3.8B (2.5 GB)"),
    ("deepseek-r1-0528-8b", "DeepSeek R1 0528 8B (5.0 GB)"),
    ("gemma3-4b", "Gemma 3 4B (2.5 GB)"),
    ("smollm3-3b", "SmolLM3 3B (1.9 GB)"),
    ("gpt-oss-20b", "GPT-OSS 20B (11.6 GB)"),
    ("qwen3-30b-a3b", "Qwen3 30B-A3B Instruct (17.3 GB)"),
    ("llama3.1-8b", "Llama 3.1 8B Instruct (4.9 GB)"),
]

struct WatchedFolderInfo: Identifiable {
    let id: UUID
    let path: String
    var enabled: Bool
    var fileCount: Int
    var lastScanAt: Date?
}

struct PreferencesWindow: View {
    @State private var allowRemoteWeb = AppSettings.shared.allowRemoteWeb
    @State private var allowRemoteMCP = AppSettings.shared.allowRemoteMCP
    @State private var workerPreset = AppSettings.shared.workerPreset
    @State private var apiPortText = String(AppSettings.shared.apiPort)
    @State private var postgresPortText = String(AppSettings.shared.postgresPort)
    @State private var tikaPortText = String(AppSettings.shared.tikaPort)
    @State private var embedderPortText = String(AppSettings.shared.embedderPort)
    @State private var llamaPortText = String(AppSettings.shared.llamaPort)
    @State private var llmModelId = AppSettings.shared.llmModelId
    @State private var logLevel = AppSettings.shared.logLevel
    @State private var needsRestart = false
    @State private var watchedFolders: [WatchedFolderInfo] = []
    @State private var isLoadingFolders = false
    @State private var folderPendingDelete: UUID?

    // Snapshot of initial values for cancel/dirty detection
    @State private var initial: Snapshot = Snapshot()

    struct Snapshot {
        var allowRemoteWeb = AppSettings.shared.allowRemoteWeb
        var allowRemoteMCP = AppSettings.shared.allowRemoteMCP
        var workerPreset = AppSettings.shared.workerPreset
        var apiPort = String(AppSettings.shared.apiPort)
        var postgresPort = String(AppSettings.shared.postgresPort)
        var tikaPort = String(AppSettings.shared.tikaPort)
        var embedderPort = String(AppSettings.shared.embedderPort)
        var llamaPort = String(AppSettings.shared.llamaPort)
        var llmModelId = AppSettings.shared.llmModelId
        var logLevel = AppSettings.shared.logLevel
    }

    var body: some View {
        VStack(spacing: 0) {
            // Title area
            HStack {
                Text("Preferences")
                    .font(.title2)
                    .fontWeight(.semibold)
                Spacer()
            }
            .padding(.horizontal, 24)
            .padding(.top, 16)
            .padding(.bottom, 8)

            Form {
                Section {
                    if watchedFolders.isEmpty && !isLoadingFolders {
                        Text("No folders being watched.")
                            .foregroundStyle(.secondary)
                            .font(.callout)
                    } else if isLoadingFolders {
                        HStack {
                            ProgressView()
                                .controlSize(.small)
                            Text("Loading...")
                                .foregroundStyle(.secondary)
                        }
                    }

                    ForEach($watchedFolders) { $folder in
                        watchedFolderRow(folder: $folder)
                    }

                    Button {
                        addWatchedFolder()
                    } label: {
                        Label("Add Folder...", systemImage: "plus")
                    }
                    .buttonStyle(.borderless)
                } header: {
                    Text("Watched Folders")
                        .font(.subheadline)
                        .fontWeight(.medium)
                        .textCase(nil)
                }

                Section {
                    Toggle("Allow remote browser connections", isOn: $allowRemoteWeb)
                        .onChange(of: allowRemoteWeb) { _, _ in markDirty() }
                    Text("Let users on your network access Harbor Clerk via a web browser.")
                        .font(.caption)
                        .foregroundStyle(.secondary)

                    Toggle("Allow remote model connections (MCP)", isOn: $allowRemoteMCP)
                        .onChange(of: allowRemoteMCP) { _, _ in markDirty() }
                    Text("Let AI models on your network query your documents.")
                        .font(.caption)
                        .foregroundStyle(.secondary)

                    if !allowRemoteWeb && !allowRemoteMCP {
                        Text("Harbor Clerk is only accessible from this Mac.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                } header: {
                    Text("Network Access")
                        .font(.subheadline)
                        .fontWeight(.medium)
                        .textCase(nil)
                }

                Section {
                    Picker("Worker preset", selection: $workerPreset) {
                        Text("Quiet").tag("quiet")
                        Text("Balanced").tag("balanced")
                        Text("Fast").tag("fast")
                    }
                    .onChange(of: workerPreset) { _, _ in markDirty() }
                } header: {
                    Text("Performance")
                        .font(.subheadline)
                        .fontWeight(.medium)
                        .textCase(nil)
                }

                Section {
                    Picker("Model", selection: $llmModelId) {
                        ForEach(modelOptions, id: \.id) { option in
                            Text(option.name).tag(option.id)
                        }
                    }
                    .onChange(of: llmModelId) { _, _ in markDirty() }
                    Text("Select a model for the built-in chat. Models are downloaded from HuggingFace via the web UI.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } header: {
                    Text("Local LLM")
                        .font(.subheadline)
                        .fontWeight(.medium)
                        .textCase(nil)
                }

                Section {
                    portRow(label: "API port", text: $apiPortText, key: "api")
                    portRow(label: "PostgreSQL port", text: $postgresPortText, key: "postgres")
                    portRow(label: "Tika port", text: $tikaPortText, key: "tika")
                    portRow(label: "Embedder port", text: $embedderPortText, key: "embedder")
                    portRow(label: "LLM port", text: $llamaPortText, key: "llama")
                    Picker("Log level", selection: $logLevel) {
                        Text("DEBUG").tag("DEBUG")
                        Text("INFO").tag("INFO")
                        Text("WARNING").tag("WARNING")
                        Text("ERROR").tag("ERROR")
                    }
                    .onChange(of: logLevel) { _, _ in markDirty() }
                } header: {
                    Text("Advanced")
                        .font(.subheadline)
                        .fontWeight(.medium)
                        .textCase(nil)
                }
            }
            .formStyle(.grouped)

            // Restart banner
            if needsRestart {
                restartBanner
                    .padding(.horizontal, 20)
                    .padding(.bottom, 16)
                    .transition(.move(edge: .bottom).combined(with: .opacity))
            }
        }
        .frame(width: 480, height: needsRestart ? 750 : 700)
        .animation(.easeInOut(duration: 0.25), value: needsRestart)
        .onAppear {
            captureInitial()
            loadWatchedFolders()
        }
    }

    // MARK: - Restart Banner

    private var restartBanner: some View {
        HStack(spacing: 12) {
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.title3)
                .foregroundStyle(.white)
            Text("Restart services to apply changes.")
                .font(.callout)
                .fontWeight(.medium)
                .foregroundStyle(.white)
            Spacer()
            Button("Cancel") {
                withAnimation { revertToInitial() }
            }
            .buttonStyle(.bordered)
            .tint(.white)
            .controlSize(.small)

            Button("Restart Now") {
                let changed = changedSettingKeys()
                applyToSettings()
                needsRestart = false
                captureInitial()
                NotificationCenter.default.post(
                    name: .preferencesRequestRestart,
                    object: nil,
                    userInfo: ["changedKeys": changed]
                )
            }
            .buttonStyle(.borderedProminent)
            .tint(.white)
            .controlSize(.small)
            .keyboardShortcut(.defaultAction)
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
        .background(
            LinearGradient(
                colors: [.orange, .orange.opacity(0.85)],
                startPoint: .leading,
                endPoint: .trailing
            )
        )
        .clipShape(RoundedRectangle(cornerRadius: 10))
    }

    // MARK: - Port Row

    @ViewBuilder
    private func portRow(label: String, text: Binding<String>, key: String) -> some View {
        HStack {
            Text(label)
            Spacer()
            TextField("", text: text)
                .frame(width: 72)
                .multilineTextAlignment(.trailing)
                .textFieldStyle(.roundedBorder)
                .font(.system(.body, design: .monospaced))
                .onChange(of: text.wrappedValue) { _, newValue in
                    if let port = Int(newValue), port > 0, port <= 65535 {
                        markDirty()
                    }
                }
            if let def = defaultPorts[key], Int(text.wrappedValue) != def {
                Button {
                    text.wrappedValue = String(def)
                } label: {
                    Image(systemName: "arrow.counterclockwise")
                        .font(.caption)
                }
                .buttonStyle(.borderless)
                .help("Reset to default (\(def))")
            }
        }
    }

    // MARK: - Watched Folder Row

    @ViewBuilder
    private func watchedFolderRow(folder: Binding<WatchedFolderInfo>) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 8) {
                Image(systemName: "folder.fill")
                    .foregroundStyle(.secondary)
                    .font(.callout)

                VStack(alignment: .leading, spacing: 2) {
                    Text(truncatedPath(folder.wrappedValue.path))
                        .font(.callout)
                        .lineLimit(1)
                        .truncationMode(.middle)
                        .help(folder.wrappedValue.path)

                    HStack(spacing: 8) {
                        Text("\(folder.wrappedValue.fileCount) files")
                            .font(.caption)
                            .foregroundStyle(.secondary)

                        if let lastScan = folder.wrappedValue.lastScanAt {
                            Text("Scanned \(lastScan, style: .relative) ago")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    }
                }

                Spacer()

                Toggle("", isOn: folder.enabled)
                    .toggleStyle(.switch)
                    .controlSize(.small)
                    .labelsHidden()
                    .onChange(of: folder.wrappedValue.enabled) { _, newValue in
                        toggleFolder(id: folder.wrappedValue.id, enabled: newValue)
                    }

                Button {
                    rescanFolder(id: folder.wrappedValue.id)
                } label: {
                    Image(systemName: "arrow.clockwise")
                        .font(.caption)
                }
                .buttonStyle(.borderless)
                .help("Rescan folder")

                Button {
                    folderPendingDelete = folder.wrappedValue.id
                } label: {
                    Image(systemName: "trash")
                        .font(.caption)
                        .foregroundStyle(.red)
                }
                .buttonStyle(.borderless)
                .help("Remove folder")
            }
        }
        .alert("Remove Watched Folder?", isPresented: Binding(
            get: { folderPendingDelete == folder.wrappedValue.id },
            set: { if !$0 { folderPendingDelete = nil } }
        )) {
            Button("Remove", role: .destructive) {
                deleteFolder(id: folder.wrappedValue.id)
                folderPendingDelete = nil
            }
            Button("Cancel", role: .cancel) {
                folderPendingDelete = nil
            }
        } message: {
            Text("This will remove the folder from watching and delete all its indexed documents. This cannot be undone.")
        }
    }

    private func truncatedPath(_ path: String) -> String {
        let components = path.components(separatedBy: "/")
        if components.count <= 4 { return path }
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        if path.hasPrefix(home) {
            return "~" + String(path.dropFirst(home.count))
        }
        return path
    }

    // MARK: - Watched Folder Actions

    private func loadWatchedFolders() {
        isLoadingFolders = true
        let port = AppSettings.shared.apiPort
        guard let url = URL(string: "http://localhost:\(port)/api/watch/folders") else {
            isLoadingFolders = false
            return
        }

        Task {
            defer { isLoadingFolders = false }

            var request = URLRequest(url: url)
            request.httpMethod = "GET"
            if let token = WatchedFolderManager.mintServiceToken() {
                request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
            }

            guard let (data, _) = try? await URLSession.shared.data(for: request),
                  let folders = try? JSONSerialization.jsonObject(with: data) as? [[String: Any]] else { return }

            let isoFormatter = ISO8601DateFormatter()
            isoFormatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]

            watchedFolders = folders.compactMap { dict in
                guard let idStr = dict["folder_id"] as? String,
                      let id = UUID(uuidString: idStr),
                      let path = dict["path"] as? String else { return nil }

                let enabled = dict["enabled"] as? Bool ?? true
                let fileCount = dict["file_count"] as? Int ?? 0
                let lastScanAt: Date? = {
                    if let str = dict["last_scan_at"] as? String {
                        return isoFormatter.date(from: str)
                    }
                    return nil
                }()

                return WatchedFolderInfo(
                    id: id,
                    path: path,
                    enabled: enabled,
                    fileCount: fileCount,
                    lastScanAt: lastScanAt
                )
            }
        }
    }

    private func addWatchedFolder() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.message = "Choose a folder to watch for documents"
        panel.prompt = "Watch"

        guard panel.runModal() == .OK, let folderURL = panel.url else { return }

        let path = folderURL.path
        let bookmarkData = try? folderURL.bookmarkData(
            options: [.withSecurityScope],
            includingResourceValuesForKeys: nil,
            relativeTo: nil
        )
        let bookmarkBase64 = bookmarkData?.base64EncodedString() ?? ""

        let port = AppSettings.shared.apiPort
        guard let url = URL(string: "http://localhost:\(port)/api/watch/folders") else { return }

        Task {
            var request = URLRequest(url: url)
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            if let token = WatchedFolderManager.mintServiceToken() {
                request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
            }

            let body: [String: Any] = [
                "path": path,
                "bookmark_data": bookmarkBase64,
                "recursive": true,
            ]
            request.httpBody = try? JSONSerialization.data(withJSONObject: body)

            guard let (data, response) = try? await URLSession.shared.data(for: request),
                  let httpResponse = response as? HTTPURLResponse,
                  httpResponse.statusCode == 200 || httpResponse.statusCode == 201,
                  let dict = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let idStr = dict["folder_id"] as? String,
                  let id = UUID(uuidString: idStr) else { return }

            let newFolder = WatchedFolderInfo(
                id: id,
                path: path,
                enabled: true,
                fileCount: 0,
                lastScanAt: nil
            )
            watchedFolders.append(newFolder)

            // Initial scan of existing files, then start watching for changes
            await WatchedFolderManager.shared.performInitialScan(folderId: id, path: path, recursive: true)
            let lastEventId = FSEventsGetCurrentEventId()
            await MainActor.run {
                WatchedFolderManager.shared.startWatcher(
                    id: id,
                    path: path,
                    recursive: true,
                    lastEventId: lastEventId
                )
            }
        }
    }

    private func toggleFolder(id: UUID, enabled: Bool) {
        let port = AppSettings.shared.apiPort
        guard let url = URL(string: "http://localhost:\(port)/api/watch/folders/\(id.uuidString)") else { return }

        Task {
            var request = URLRequest(url: url)
            request.httpMethod = "PATCH"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            if let token = WatchedFolderManager.mintServiceToken() {
                request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
            }
            request.httpBody = try? JSONSerialization.data(withJSONObject: ["enabled": enabled])

            let _ = try? await URLSession.shared.data(for: request)

            if enabled {
                if let folder = watchedFolders.first(where: { $0.id == id }) {
                    WatchedFolderManager.shared.startWatcher(
                        id: id,
                        path: folder.path,
                        recursive: true,
                        lastEventId: FSEventsGetCurrentEventId()
                    )
                }
            } else {
                WatchedFolderManager.shared.stopWatcher(id: id)
            }
        }
    }

    private func rescanFolder(id: UUID) {
        let port = AppSettings.shared.apiPort
        guard let url = URL(string: "http://localhost:\(port)/api/watch/folders/\(id.uuidString)/rescan") else { return }

        Task {
            var request = URLRequest(url: url)
            request.httpMethod = "POST"
            if let token = WatchedFolderManager.mintServiceToken() {
                request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
            }

            let _ = try? await URLSession.shared.data(for: request)
            // Reload to get updated file counts
            loadWatchedFolders()
        }
    }

    private func deleteFolder(id: UUID) {
        let port = AppSettings.shared.apiPort
        guard let url = URL(string: "http://localhost:\(port)/api/watch/folders/\(id.uuidString)") else { return }

        Task {
            var request = URLRequest(url: url)
            request.httpMethod = "DELETE"
            if let token = WatchedFolderManager.mintServiceToken() {
                request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
            }

            let _ = try? await URLSession.shared.data(for: request)

            WatchedFolderManager.shared.stopWatcher(id: id)
            watchedFolders.removeAll { $0.id == id }
        }
    }

    // MARK: - State management

    private func markDirty() {
        needsRestart = true
    }

    /// Write all local @State values to AppSettings (single save).
    private func applyToSettings() {
        let settings = AppSettings.shared
        settings.allowRemoteWeb = allowRemoteWeb
        settings.allowRemoteMCP = allowRemoteMCP
        settings.workerPreset = workerPreset
        if let p = Int(apiPortText), p > 0, p <= 65535 { settings.apiPort = p }
        if let p = Int(postgresPortText), p > 0, p <= 65535 { settings.postgresPort = p }
        if let p = Int(tikaPortText), p > 0, p <= 65535 { settings.tikaPort = p }
        if let p = Int(embedderPortText), p > 0, p <= 65535 { settings.embedderPort = p }
        if let p = Int(llamaPortText), p > 0, p <= 65535 { settings.llamaPort = p }
        settings.llmModelId = llmModelId
        settings.logLevel = logLevel
    }

    private func captureInitial() {
        initial = Snapshot(
            allowRemoteWeb: allowRemoteWeb,
            allowRemoteMCP: allowRemoteMCP,
            workerPreset: workerPreset,
            apiPort: apiPortText,
            postgresPort: postgresPortText,
            tikaPort: tikaPortText,
            embedderPort: embedderPortText,
            llamaPort: llamaPortText,
            llmModelId: llmModelId,
            logLevel: logLevel
        )
    }

    private func revertToInitial() {
        allowRemoteWeb = initial.allowRemoteWeb
        allowRemoteMCP = initial.allowRemoteMCP
        workerPreset = initial.workerPreset
        apiPortText = initial.apiPort
        postgresPortText = initial.postgresPort
        tikaPortText = initial.tikaPort
        embedderPortText = initial.embedderPort
        llamaPortText = initial.llamaPort
        llmModelId = initial.llmModelId
        logLevel = initial.logLevel
        needsRestart = false
    }

    private func changedSettingKeys() -> Set<String> {
        var keys = Set<String>()
        if allowRemoteWeb != initial.allowRemoteWeb { keys.insert("allow_remote_web") }
        if allowRemoteMCP != initial.allowRemoteMCP { keys.insert("allow_remote_mcp") }
        if workerPreset != initial.workerPreset { keys.insert("worker_preset") }
        if apiPortText != initial.apiPort { keys.insert("api_port") }
        if postgresPortText != initial.postgresPort { keys.insert("postgres_port") }
        if tikaPortText != initial.tikaPort { keys.insert("tika_port") }
        if embedderPortText != initial.embedderPort { keys.insert("embedder_port") }
        if llamaPortText != initial.llamaPort { keys.insert("llama_port") }
        if llmModelId != initial.llmModelId { keys.insert("llm_model_id") }
        if logLevel != initial.logLevel { keys.insert("log_level") }
        return keys
    }
}

extension Notification.Name {
    static let preferencesRequestRestart = Notification.Name("preferencesRequestRestart")
}

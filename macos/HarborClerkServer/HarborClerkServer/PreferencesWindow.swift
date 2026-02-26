import SwiftUI

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
    ("llama3.2-3b", "Llama 3.2 3B Instruct (2.0 GB)"),
    ("mistral-7b", "Mistral 7B Instruct v0.3 (4.1 GB)"),
    ("deepseek-r1-8b", "DeepSeek R1 8B (4.9 GB)"),
]

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
        .frame(width: 480, height: needsRestart ? 650 : 600)
        .animation(.easeInOut(duration: 0.25), value: needsRestart)
        .onAppear { captureInitial() }
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

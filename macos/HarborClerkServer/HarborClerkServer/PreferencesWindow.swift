import SwiftUI

private let defaultPorts: [String: Int] = [
    "api": 8100,
    "postgres": 5433,
    "redis": 6380,
    "embedder": 8101,
    "llama": 8102,
]

private let modelOptions: [(id: String, name: String)] = [
    ("", "None"),
    ("qwen2.5-7b", "Qwen 2.5 7B Instruct (4.4 GB)"),
    ("qwen2.5-3b", "Qwen 2.5 3B Instruct (2.0 GB)"),
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
    @State private var redisPortText = String(AppSettings.shared.redisPort)
    @State private var embedderPortText = String(AppSettings.shared.embedderPort)
    @State private var llamaPortText = String(AppSettings.shared.llamaPort)
    @State private var llmModelId = AppSettings.shared.llmModelId
    @State private var logLevel = AppSettings.shared.logLevel
    @State private var needsRestart = false

    // Snapshot of initial values for cancel
    @State private var initial: Snapshot = Snapshot()

    struct Snapshot {
        var allowRemoteWeb = AppSettings.shared.allowRemoteWeb
        var allowRemoteMCP = AppSettings.shared.allowRemoteMCP
        var workerPreset = AppSettings.shared.workerPreset
        var apiPort = String(AppSettings.shared.apiPort)
        var postgresPort = String(AppSettings.shared.postgresPort)
        var redisPort = String(AppSettings.shared.redisPort)
        var embedderPort = String(AppSettings.shared.embedderPort)
        var llamaPort = String(AppSettings.shared.llamaPort)
        var llmModelId = AppSettings.shared.llmModelId
        var logLevel = AppSettings.shared.logLevel
    }

    var body: some View {
        Form {
            Section("Network Access") {
                Toggle("Allow remote browser connections", isOn: $allowRemoteWeb)
                    .onChange(of: allowRemoteWeb) { _, newValue in
                        AppSettings.shared.allowRemoteWeb = newValue
                        needsRestart = true
                    }
                Text("Let users on your network access Harbor Clerk via a web browser.")
                    .font(.caption)
                    .foregroundStyle(.secondary)

                Toggle("Allow remote model connections (MCP)", isOn: $allowRemoteMCP)
                    .onChange(of: allowRemoteMCP) { _, newValue in
                        AppSettings.shared.allowRemoteMCP = newValue
                        needsRestart = true
                    }
                Text("Let AI models on your network query your documents.")
                    .font(.caption)
                    .foregroundStyle(.secondary)

                if !allowRemoteWeb && !allowRemoteMCP {
                    Text("Harbor Clerk is only accessible from this Mac.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }

            Section("Performance") {
                Picker("Worker preset", selection: $workerPreset) {
                    Text("Quiet").tag("quiet")
                    Text("Balanced").tag("balanced")
                    Text("Fast").tag("fast")
                }
                .onChange(of: workerPreset) { _, newValue in
                    AppSettings.shared.workerPreset = newValue
                    needsRestart = true
                }
            }

            Section("Local LLM") {
                Picker("Model", selection: $llmModelId) {
                    ForEach(modelOptions, id: \.id) { option in
                        Text(option.name).tag(option.id)
                    }
                }
                .onChange(of: llmModelId) { _, newValue in
                    AppSettings.shared.llmModelId = newValue
                    needsRestart = true
                }
                Text("Select a model for the built-in chat. Models are downloaded from HuggingFace via the web UI.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            Section("Advanced") {
                portRow(label: "API port", text: $apiPortText, key: "api") { port in
                    AppSettings.shared.apiPort = port
                }
                portRow(label: "PostgreSQL port", text: $postgresPortText, key: "postgres") { port in
                    AppSettings.shared.postgresPort = port
                }
                portRow(label: "Redis port", text: $redisPortText, key: "redis") { port in
                    AppSettings.shared.redisPort = port
                }
                portRow(label: "Embedder port", text: $embedderPortText, key: "embedder") { port in
                    AppSettings.shared.embedderPort = port
                }
                portRow(label: "LLM port", text: $llamaPortText, key: "llama") { port in
                    AppSettings.shared.llamaPort = port
                }
                Picker("Log level", selection: $logLevel) {
                    Text("DEBUG").tag("DEBUG")
                    Text("INFO").tag("INFO")
                    Text("WARNING").tag("WARNING")
                    Text("ERROR").tag("ERROR")
                }
                .onChange(of: logLevel) { _, newValue in
                    AppSettings.shared.logLevel = newValue
                    needsRestart = true
                }
            }

            if needsRestart {
                Section {
                    HStack {
                        Image(systemName: "exclamationmark.triangle")
                            .foregroundStyle(.orange)
                        Text("Restart services to apply changes.")
                            .font(.callout)
                        Spacer()
                        Button("Cancel Changes") {
                            revertToInitial()
                        }
                        Button("Restart Now") {
                            needsRestart = false
                            captureInitial()
                            NotificationCenter.default.post(
                                name: .preferencesRequestRestart, object: nil
                            )
                        }
                        .keyboardShortcut(.defaultAction)
                    }
                }
            }
        }
        .formStyle(.grouped)
        .frame(width: 480, height: needsRestart ? 640 : 590)
        .onAppear { captureInitial() }
    }

    @ViewBuilder
    private func portRow(label: String, text: Binding<String>, key: String, save: @escaping (Int) -> Void) -> some View {
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
                        save(port)
                        needsRestart = true
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

    private func captureInitial() {
        initial = Snapshot(
            allowRemoteWeb: allowRemoteWeb,
            allowRemoteMCP: allowRemoteMCP,
            workerPreset: workerPreset,
            apiPort: apiPortText,
            postgresPort: postgresPortText,
            redisPort: redisPortText,
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
        redisPortText = initial.redisPort
        embedderPortText = initial.embedderPort
        llamaPortText = initial.llamaPort
        llmModelId = initial.llmModelId
        logLevel = initial.logLevel

        // Write reverted values back to settings
        AppSettings.shared.allowRemoteWeb = initial.allowRemoteWeb
        AppSettings.shared.allowRemoteMCP = initial.allowRemoteMCP
        AppSettings.shared.workerPreset = initial.workerPreset
        if let p = Int(initial.apiPort) { AppSettings.shared.apiPort = p }
        if let p = Int(initial.postgresPort) { AppSettings.shared.postgresPort = p }
        if let p = Int(initial.redisPort) { AppSettings.shared.redisPort = p }
        if let p = Int(initial.embedderPort) { AppSettings.shared.embedderPort = p }
        if let p = Int(initial.llamaPort) { AppSettings.shared.llamaPort = p }
        AppSettings.shared.llmModelId = initial.llmModelId
        AppSettings.shared.logLevel = initial.logLevel

        needsRestart = false
    }
}

extension Notification.Name {
    static let preferencesRequestRestart = Notification.Name("preferencesRequestRestart")
}

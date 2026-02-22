import SwiftUI

struct PreferencesWindow: View {
    @State private var allowRemoteWeb = AppSettings.shared.allowRemoteWeb
    @State private var allowRemoteMCP = AppSettings.shared.allowRemoteMCP
    @State private var workerPreset = AppSettings.shared.workerPreset
    @State private var apiPort = AppSettings.shared.apiPort
    @State private var postgresPort = AppSettings.shared.postgresPort
    @State private var redisPort = AppSettings.shared.redisPort
    @State private var embedderPort = AppSettings.shared.embedderPort
    @State private var logLevel = AppSettings.shared.logLevel
    @State private var needsRestart = false

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

            Section("Advanced") {
                HStack {
                    Text("API port")
                    Spacer()
                    TextField("", value: $apiPort, format: .number)
                        .frame(width: 80)
                        .multilineTextAlignment(.trailing)
                        .onChange(of: apiPort) { _, newValue in
                            AppSettings.shared.apiPort = newValue
                            needsRestart = true
                        }
                }
                HStack {
                    Text("PostgreSQL port")
                    Spacer()
                    TextField("", value: $postgresPort, format: .number)
                        .frame(width: 80)
                        .multilineTextAlignment(.trailing)
                        .onChange(of: postgresPort) { _, newValue in
                            AppSettings.shared.postgresPort = newValue
                            needsRestart = true
                        }
                }
                HStack {
                    Text("Redis port")
                    Spacer()
                    TextField("", value: $redisPort, format: .number)
                        .frame(width: 80)
                        .multilineTextAlignment(.trailing)
                        .onChange(of: redisPort) { _, newValue in
                            AppSettings.shared.redisPort = newValue
                            needsRestart = true
                        }
                }
                HStack {
                    Text("Embedder port")
                    Spacer()
                    TextField("", value: $embedderPort, format: .number)
                        .frame(width: 80)
                        .multilineTextAlignment(.trailing)
                        .onChange(of: embedderPort) { _, newValue in
                            AppSettings.shared.embedderPort = newValue
                            needsRestart = true
                        }
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
                        Button("Restart Now") {
                            needsRestart = false
                            NotificationCenter.default.post(
                                name: .preferencesRequestRestart, object: nil
                            )
                        }
                    }
                }
            }
        }
        .formStyle(.grouped)
        .frame(width: 450, height: needsRestart ? 520 : 480)
    }
}

extension Notification.Name {
    static let preferencesRequestRestart = Notification.Name("preferencesRequestRestart")
}

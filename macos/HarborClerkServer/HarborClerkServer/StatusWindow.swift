import SwiftUI
import UniformTypeIdentifiers

struct StatusWindow: View {
    @ObservedObject var serviceManager: ServiceManager
    @ObservedObject var logManager = LogManager.shared
    @State private var logsExpanded = false

    var body: some View {
        VStack(spacing: 0) {
            // Services table
            List {
                Section("Services") {
                    ForEach(serviceManager.services.indices, id: \.self) { i in
                        let service = serviceManager.services[i]
                        HStack {
                            stateIndicator(service.state)
                            Text(service.name)
                                .font(.system(.body, design: .monospaced))
                            Spacer()
                            Text(service.state.rawValue)
                                .font(.caption)
                                .foregroundColor(.secondary)
                        }
                    }
                }
            }
            .frame(minHeight: 180, maxHeight: 200)

            Divider()

            // Logs disclosure
            VStack(alignment: .leading, spacing: 0) {
                DisclosureGroup(isExpanded: $logsExpanded) {
                    ScrollViewReader { proxy in
                        ScrollView([.vertical, .horizontal]) {
                            LazyVStack(alignment: .leading, spacing: 1) {
                                ForEach(logManager.lines) { line in
                                    HStack(alignment: .top, spacing: 8) {
                                        Text(line.service)
                                            .font(.system(.caption, design: .monospaced))
                                            .foregroundColor(.accentColor)
                                            .frame(width: 80, alignment: .trailing)
                                        Text(line.text)
                                            .font(.system(.caption, design: .monospaced))
                                            .foregroundColor(.primary)
                                            .textSelection(.enabled)
                                            .lineLimit(nil)
                                            .fixedSize(horizontal: false, vertical: true)
                                    }
                                    .id(line.id)
                                }
                            }
                            .padding(.horizontal, 4)
                            .padding(.vertical, 2)
                        }
                        .frame(height: 200)
                        .onChange(of: logManager.lines.count) {
                            if let last = logManager.lines.last {
                                proxy.scrollTo(last.id, anchor: .bottom)
                            }
                        }
                    }

                    HStack {
                        Spacer()
                        Button("Save Logs...") {
                            saveLogs()
                        }
                        .buttonStyle(.borderless)
                        Button("Clear") {
                            logManager.clear()
                        }
                        .buttonStyle(.borderless)
                    }
                    .padding(.top, 4)
                } label: {
                    Text("Logs (\(logManager.lines.count))")
                        .font(.headline)
                }
            }
            .padding(.horizontal)
            .padding(.vertical, 8)

            Divider()

            // Controls
            HStack {
                Button("Start All") {
                    Task { await serviceManager.startAll() }
                }
                Button("Stop All") {
                    serviceManager.stopAll()
                }
                Spacer()
            }
            .padding()
        }
    }

    @ViewBuilder
    private func stateIndicator(_ state: ServiceState) -> some View {
        Circle()
            .fill(colorForState(state))
            .frame(width: 10, height: 10)
    }

    private func colorForState(_ state: ServiceState) -> Color {
        switch state {
        case .stopped: .gray
        case .starting, .stopping: .yellow
        case .running: .green
        case .errored: .red
        }
    }

    private func saveLogs() {
        let panel = NSSavePanel()
        panel.allowedContentTypes = [.plainText]
        panel.nameFieldStringValue = "harbor-clerk-logs.txt"
        panel.begin { response in
            guard response == .OK, let url = panel.url else { return }
            let text = logManager.lines.map { line in
                let ts = ISO8601DateFormatter().string(from: line.timestamp)
                return "\(ts) [\(line.service)] \(line.text)"
            }.joined(separator: "\n")
            try? text.write(to: url, atomically: true, encoding: .utf8)
        }
    }
}

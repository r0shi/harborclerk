import SwiftUI

struct StatusWindow: View {
    @ObservedObject var serviceManager: ServiceManager
    @ObservedObject var logManager = LogManager.shared

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
            .frame(height: 200)

            Divider()

            // Logs
            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text("Logs")
                        .font(.headline)
                    Spacer()
                    Button("Clear") {
                        logManager.clear()
                    }
                    .buttonStyle(.borderless)
                }
                .padding(.horizontal)
                .padding(.top, 8)

                ScrollViewReader { proxy in
                    ScrollView {
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
                                }
                                .id(line.id)
                            }
                        }
                        .padding(.horizontal)
                    }
                    .onChange(of: logManager.lines.count) {
                        if let last = logManager.lines.last {
                            proxy.scrollTo(last.id, anchor: .bottom)
                        }
                    }
                }
            }

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
}

import SwiftUI
import UniformTypeIdentifiers

struct StatusWindow: View {
    @ObservedObject var serviceManager: ServiceManager
    @ObservedObject var logManager = LogManager.shared
    @State private var logsExpanded = false

    private var overallState: ServiceState {
        serviceManager.overallState
    }

    var body: some View {
        VStack(spacing: 0) {
            // Header
            headerBar
                .padding(.horizontal, 20)
                .padding(.top, 16)
                .padding(.bottom, 12)

            // Service cards
            serviceCards
                .padding(.horizontal, 20)

            Spacer(minLength: 8)

            // Logs section
            logsSection
                .padding(.horizontal, 20)
                .padding(.bottom, 8)

            // Control bar
            controlBar
                .padding(.horizontal, 20)
                .padding(.bottom, 16)
        }
        .frame(minWidth: 560, minHeight: 420)
        .background(.clear)
    }

    // MARK: - Header

    private var headerBar: some View {
        HStack {
            Text("Harbor Clerk Server")
                .font(.title2)
                .fontWeight(.semibold)
            Spacer()
            statusPill
        }
    }

    private var statusPill: some View {
        HStack(spacing: 6) {
            Circle()
                .fill(pillColor)
                .frame(width: 8, height: 8)
                .overlay(
                    Circle()
                        .fill(pillColor.opacity(0.4))
                        .frame(width: 16, height: 16)
                        .opacity(isTransient(overallState) ? 1 : 0)
                        .animation(.easeInOut(duration: 1.0).repeatForever(autoreverses: true), value: overallState)
                )
            Text(pillLabel)
                .font(.caption)
                .fontWeight(.medium)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 5)
        .background(pillColor.opacity(0.15))
        .clipShape(Capsule())
    }

    private var pillColor: Color {
        switch overallState {
        case .running: .green
        case .starting, .stopping: .orange
        case .errored: .red
        case .stopped: .gray
        }
    }

    private var pillLabel: String {
        switch overallState {
        case .running: "All Running"
        case .starting: "Starting"
        case .stopping: "Stopping"
        case .errored: "Error"
        case .stopped: "Stopped"
        }
    }

    // MARK: - Service Cards

    private var serviceCards: some View {
        VStack(spacing: 0) {
            ForEach(serviceManager.services.indices, id: \.self) { i in
                let service = serviceManager.services[i]
                serviceRow(service)
                if i < serviceManager.services.count - 1 {
                    Divider()
                        .padding(.leading, 32)
                }
            }
        }
        .padding(.vertical, 4)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 10))
    }

    private func serviceRow(_ service: any ManagedService) -> some View {
        HStack(spacing: 10) {
            // Animated status dot
            ZStack {
                Circle()
                    .fill(colorForState(service.state).opacity(0.3))
                    .frame(width: 18, height: 18)
                    .opacity(isTransient(service.state) ? 1 : 0)
                    .animation(
                        isTransient(service.state)
                            ? .easeInOut(duration: 1.0).repeatForever(autoreverses: true)
                            : .default,
                        value: service.state
                    )
                Circle()
                    .fill(colorForState(service.state))
                    .frame(width: 8, height: 8)
            }
            .frame(width: 18, height: 18)

            Text(service.name)
                .font(.body)
            Spacer()
            Text(service.state.rawValue.capitalized)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 7)
        .contentShape(Rectangle())
    }

    // MARK: - Logs

    private var logsSection: some View {
        VStack(spacing: 0) {
            // Logs toggle bar
            Button {
                withAnimation(.easeInOut(duration: 0.2)) {
                    logsExpanded.toggle()
                }
            } label: {
                HStack {
                    Image(systemName: logsExpanded ? "chevron.down" : "chevron.right")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .frame(width: 12)
                    Text("Logs")
                        .font(.subheadline)
                        .fontWeight(.medium)
                    Text("(\(logManager.lines.count))")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Spacer()
                    if logsExpanded {
                        Button {
                            saveLogs()
                        } label: {
                            Image(systemName: "square.and.arrow.down")
                                .font(.caption)
                        }
                        .buttonStyle(.borderless)
                        .help("Save Logs")

                        Button {
                            logManager.clear()
                        } label: {
                            Image(systemName: "trash")
                                .font(.caption)
                        }
                        .buttonStyle(.borderless)
                        .help("Clear Logs")
                    }
                }
                .padding(.horizontal, 14)
                .padding(.vertical, 8)
            }
            .buttonStyle(.plain)

            if logsExpanded {
                logTerminalView
            }
        }
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 10))
    }

    private var logTerminalView: some View {
        ScrollViewReader { proxy in
            ScrollView([.vertical, .horizontal]) {
                LazyVStack(alignment: .leading, spacing: 1) {
                    ForEach(logManager.lines) { line in
                        HStack(alignment: .top, spacing: 8) {
                            Text(line.service)
                                .font(.system(.caption, design: .monospaced))
                                .foregroundStyle(.cyan)
                                .frame(width: 80, alignment: .trailing)
                            Text(line.text)
                                .font(.system(.caption, design: .monospaced))
                                .foregroundStyle(.primary.opacity(0.9))
                                .textSelection(.enabled)
                                .lineLimit(nil)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        .id(line.id)
                    }
                }
                .padding(.horizontal, 10)
                .padding(.vertical, 6)
            }
            .frame(height: 180)
            .background(.black.opacity(0.55))
            .clipShape(RoundedRectangle(cornerRadius: 6))
            .padding(.horizontal, 8)
            .padding(.bottom, 8)
            .onChange(of: logManager.lines.count) {
                if let last = logManager.lines.last {
                    proxy.scrollTo(last.id, anchor: .bottom)
                }
            }
        }
    }

    // MARK: - Controls

    private var controlBar: some View {
        HStack(spacing: 12) {
            Button {
                Task { await serviceManager.startAll() }
            } label: {
                Label("Start All", systemImage: "play.fill")
                    .frame(minWidth: 100)
            }
            .controlSize(.large)
            .buttonStyle(.borderedProminent)
            .tint(.accentColor)

            Button {
                serviceManager.stopAll()
            } label: {
                Label("Stop All", systemImage: "stop.fill")
                    .frame(minWidth: 100)
            }
            .controlSize(.large)
            .buttonStyle(.bordered)

            Spacer()
        }
    }

    // MARK: - Helpers

    private func colorForState(_ state: ServiceState) -> Color {
        switch state {
        case .stopped: .gray
        case .starting, .stopping: .orange
        case .running: .green
        case .errored: .red
        }
    }

    private func isTransient(_ state: ServiceState) -> Bool {
        state == .starting || state == .stopping
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

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

            // Service cards (scrollable if many services)
            ScrollView {
                serviceCards
            }
            .padding(.horizontal, 20)

            // Logs section (flexible height when expanded)
            logsSection
                .padding(.horizontal, 20)
                .padding(.top, 8)
                .padding(.bottom, 8)

            // Control bar (always visible at bottom)
            controlBar
                .padding(.horizontal, 20)
                .padding(.bottom, 16)
        }
        .frame(minWidth: 600, minHeight: 480)
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
                .frame(width: 56, alignment: .trailing)

            // Per-service controls
            HStack(spacing: 4) {
                Button {
                    Task { await serviceManager.startService(service) }
                } label: {
                    Image(systemName: "play.fill")
                        .font(.caption2)
                        .frame(width: 20, height: 20)
                }
                .buttonStyle(.borderless)
                .disabled(service.state == .running || service.state == .starting)
                .help("Start")

                Button {
                    serviceManager.stopService(service)
                } label: {
                    Image(systemName: "stop.fill")
                        .font(.caption2)
                        .frame(width: 20, height: 20)
                }
                .buttonStyle(.borderless)
                .disabled(service.state == .stopped || service.state == .stopping)
                .help("Stop")

                Button {
                    Task { await serviceManager.restartService(service) }
                } label: {
                    Image(systemName: "arrow.trianglehead.2.counterclockwise")
                        .font(.caption2)
                        .frame(width: 20, height: 20)
                }
                .buttonStyle(.borderless)
                .disabled(service.state == .stopped || service.state == .starting || service.state == .stopping)
                .help("Restart")
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 5)
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
                            copyLogs()
                        } label: {
                            Image(systemName: "doc.on.doc")
                                .font(.caption)
                        }
                        .buttonStyle(.borderless)
                        .help("Copy Logs")

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
                    .frame(minHeight: 120, maxHeight: 250)
            }
        }
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 10))
    }

    private var logTerminalView: some View {
        ScrollViewReader { proxy in
            // Use a single selectable Text for multi-line select + Cmd+C/Cmd+A
            ScrollView([.vertical, .horizontal]) {
                Text(logTextAttributed)
                    .font(.system(.caption, design: .monospaced))
                    .textSelection(.enabled)
                    .padding(.horizontal, 10)
                    .padding(.vertical, 6)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .id("logContent")
            }
            .background(.black.opacity(0.55))
            .clipShape(RoundedRectangle(cornerRadius: 6))
            .padding(.horizontal, 8)
            .padding(.bottom, 8)
            .onChange(of: logManager.lines.count) {
                withAnimation {
                    proxy.scrollTo("logContent", anchor: .bottom)
                }
            }
        }
    }

    private var logTextAttributed: AttributedString {
        var result = AttributedString()
        for line in logManager.lines {
            var service = AttributedString(line.service.padding(toLength: 12, withPad: " ", startingAt: 0))
            service.foregroundColor = .cyan
            var text = AttributedString(line.text + "\n")
            text.foregroundColor = .init(white: 0.9)
            result += service + text
        }
        return result
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

    private func copyLogs() {
        let text = logManager.lines.map { line in
            let ts = ISO8601DateFormatter().string(from: line.timestamp)
            return "\(ts) [\(line.service)] \(line.text)"
        }.joined(separator: "\n")
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
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

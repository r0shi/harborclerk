import SwiftUI
import UniformTypeIdentifiers

struct LogWindow: View {
    @ObservedObject var logManager = LogManager.shared
    @State private var selectedService: String = "All"
    @State private var autoScroll = true

    private var serviceNames: [String] {
        var names = Set<String>()
        for line in logManager.lines {
            names.insert(line.service)
        }
        return ["All"] + names.sorted()
    }

    private var filteredLines: [LogManager.LogLine] {
        if selectedService == "All" {
            return logManager.lines
        }
        return logManager.lines.filter { $0.service == selectedService }
    }

    var body: some View {
        VStack(spacing: 0) {
            toolbar
                .padding(.horizontal, 16)
                .padding(.top, 12)
                .padding(.bottom, 8)

            logTerminalView
                .padding(.horizontal, 16)
                .padding(.bottom, 16)
        }
        .frame(minWidth: 700, minHeight: 400)
    }

    // MARK: - Toolbar

    private var toolbar: some View {
        HStack(spacing: 12) {
            Picker("Service", selection: $selectedService) {
                ForEach(serviceNames, id: \.self) { name in
                    Text(name).tag(name)
                }
            }
            .frame(width: 160)

            Text("\(filteredLines.count) lines")
                .font(.caption)
                .foregroundStyle(.secondary)

            Spacer()

            Button {
                copyLogs()
            } label: {
                Image(systemName: "doc.on.doc")
            }
            .buttonStyle(.borderless)
            .help("Copy Logs")

            Button {
                saveLogs()
            } label: {
                Image(systemName: "square.and.arrow.down")
            }
            .buttonStyle(.borderless)
            .help("Save Logs")

            Button {
                logManager.clear()
            } label: {
                Image(systemName: "trash")
            }
            .buttonStyle(.borderless)
            .help("Clear Logs")
        }
    }

    // MARK: - Terminal View

    private var logTerminalView: some View {
        ScrollViewReader { proxy in
            ScrollView([.vertical, .horizontal]) {
                Text(logTextAttributed)
                    .font(.system(.caption, design: .monospaced))
                    .textSelection(.enabled)
                    .padding(.horizontal, 10)
                    .padding(.vertical, 6)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .id("logBottom")
            }
            .background(.black.opacity(0.55))
            .clipShape(RoundedRectangle(cornerRadius: 8))
            .onChange(of: filteredLines.count) {
                if autoScroll {
                    proxy.scrollTo("logBottom", anchor: .bottom)
                }
            }
        }
    }

    private var logTextAttributed: AttributedString {
        var result = AttributedString()
        for line in filteredLines {
            var service = AttributedString(line.service.padding(toLength: 12, withPad: " ", startingAt: 0))
            service.foregroundColor = .cyan
            var text = AttributedString(line.text + "\n")
            text.foregroundColor = .init(white: 0.9)
            result += service + text
        }
        return result
    }

    // MARK: - Actions

    private func copyLogs() {
        let text = filteredLines.map { line in
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
            let text = filteredLines.map { line in
                let ts = ISO8601DateFormatter().string(from: line.timestamp)
                return "\(ts) [\(line.service)] \(line.text)"
            }.joined(separator: "\n")
            try? text.write(to: url, atomically: true, encoding: .utf8)
        }
    }
}

import Darwin
import SwiftUI

// MARK: - CLI Shim Row

/// Displays the shim install status and Install / Re-install / Uninstall controls.
/// Shown inside the Network Access section, below the enableCliAccess toggle.
private struct CliShimRow: View {
    @State private var status: CliShimStatus = CliShimInstaller.currentStatus()
    @State private var actionError: String? = nil
    private let pathSnippet = #"export PATH="$HOME/.local/bin:$PATH""#

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            // Status line
            HStack(spacing: 6) {
                statusIcon
                Text(statusLabel)
                    .font(.callout)
                    .foregroundStyle(.primary)
                Spacer()
                actionButton
            }

            // Error (if last action failed)
            if let err = actionError {
                Text(err)
                    .font(.caption)
                    .foregroundStyle(.red)
            }

            // PATH snippet
            HStack(spacing: 0) {
                Text(pathSnippet)
                    .font(.system(.caption, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
                Spacer()
                CopySnippetButton(text: pathSnippet)
            }
            .padding(6)
            .background(Color(NSColor.controlBackgroundColor))
            .clipShape(RoundedRectangle(cornerRadius: 6))

            Text("Add the snippet above to ~/.zshrc (or ~/.bashrc) if harbor-clerk isn't found on your PATH.")
                .font(.caption)
                .foregroundStyle(.secondary)

            // API key is the per-client credential — the shim bakes in the
            // server URL, but each agent still needs its own key.
            Text("Each agent also needs an API key — pass `--api-key hc_...` or set `HARBOR_CLERK_API_KEY`. Mint a key in System Settings → API Keys (in the web app).")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .padding(.vertical, 4)
    }

    // MARK: - Sub-views

    @ViewBuilder
    private var statusIcon: some View {
        switch status {
        case .installed:
            Image(systemName: "checkmark.circle.fill")
                .foregroundStyle(.green)
        case .installedOutdated:
            Image(systemName: "exclamationmark.triangle.fill")
                .foregroundStyle(.orange)
        case .notInstalled:
            Image(systemName: "circle")
                .foregroundStyle(.secondary)
        }
    }

    private var statusLabel: String {
        switch status {
        case .installed(let path):
            return "Installed at \(path)"
        case .installedOutdated:
            return "Installed but stale — re-install to update"
        case .notInstalled:
            return "Not installed"
        }
    }

    @ViewBuilder
    private var actionButton: some View {
        switch status {
        case .installed:
            Button("Uninstall") { runAction { try CliShimInstaller.uninstall() } }
                .controlSize(.small)
                .buttonStyle(.bordered)
        case .installedOutdated:
            Button("Re-install") { runAction { try CliShimInstaller.install() } }
                .controlSize(.small)
                .buttonStyle(.borderedProminent)
        case .notInstalled:
            Button("Install") { runAction { try CliShimInstaller.install() } }
                .controlSize(.small)
                .buttonStyle(.borderedProminent)
        }
    }

    // MARK: - Action helper

    private func runAction(_ block: () throws -> Void) {
        actionError = nil
        do {
            try block()
        } catch {
            actionError = error.localizedDescription
        }
        status = CliShimInstaller.currentStatus()
    }
}

/// Compact inline copy button for the PATH snippet.
private struct CopySnippetButton: View {
    let text: String
    @State private var copied = false

    var body: some View {
        Button(copied ? "Copied!" : "Copy") {
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(text, forType: .string)
            copied = true
            DispatchQueue.main.asyncAfter(deadline: .now() + 2) { copied = false }
        }
        .controlSize(.mini)
        .buttonStyle(.borderless)
        .foregroundStyle(.secondary)
    }
}

private struct LocalNetworkInterface: Identifiable {
    let id = UUID()
    let name: String
    let address: String
    let isTailscale: Bool

    var label: String {
        isTailscale ? "Tailscale \(address)" : "\(name) \(address)"
    }
}

private func localNetworkInterfaces() -> [LocalNetworkInterface] {
    var pointer: UnsafeMutablePointer<ifaddrs>?
    guard getifaddrs(&pointer) == 0, let first = pointer else { return [] }
    defer { freeifaddrs(pointer) }

    var interfaces: [LocalNetworkInterface] = []
    var current: UnsafeMutablePointer<ifaddrs>? = first
    while let item = current {
        defer { current = item.pointee.ifa_next }
        let interface = item.pointee
        guard let addr = interface.ifa_addr else { continue }
        let family = addr.pointee.sa_family
        guard family == UInt8(AF_INET) || family == UInt8(AF_INET6) else { continue }

        var host = [CChar](repeating: 0, count: Int(NI_MAXHOST))
        let length = family == UInt8(AF_INET) ? socklen_t(MemoryLayout<sockaddr_in>.size) : socklen_t(MemoryLayout<sockaddr_in6>.size)
        guard getnameinfo(addr, length, &host, socklen_t(host.count), nil, 0, NI_NUMERICHOST) == 0 else { continue }
        let address = String(cString: host)
        let name = String(cString: interface.ifa_name)
        let flags = Int32(interface.ifa_flags)
        let isLoopback = (flags & IFF_LOOPBACK) != 0
        guard !isLoopback else { continue }
        interfaces.append(
            LocalNetworkInterface(
                name: name,
                address: address,
                isTailscale: isTailscaleAddress(address) || name.lowercased().contains("tailscale")
            )
        )
    }
    return interfaces
}

private func isTailscaleAddress(_ address: String) -> Bool {
    let parts = address.split(separator: ".").compactMap { Int($0) }
    guard parts.count == 4 else { return false }
    return parts[0] == 100 && (64...127).contains(parts[1])
}

private let defaultPorts: [String: Int] = [
    "api": 8100,
    "gateway": GatewayConfig.defaultPort,
    "postgres": 5433,
    "tika": 9998,
    "embedder": 8101,
    "reranker": 8201,
    "llama": 8102,
]

private let modelOptions: [(id: String, name: String)] = [
    ("", "None"),
    ("qwen3-8b", "Qwen3 8B (5.0 GB)"),
    ("qwen3-4b", "Qwen3 4B (2.5 GB)"),
    ("gpt-oss-20b", "GPT-OSS 20B (11.6 GB)"),
    ("qwen36-35b-a3b", "Qwen3.6 35B-A3B (22.1 GB)"),
    ("gemma4-26b-a4b", "Gemma 4 26B-A4B (17.0 GB)"),
]

struct PreferencesWindow: View {
    @State private var allowRemoteWeb = AppSettings.shared.allowRemoteWeb
    @State private var allowRemoteMCP = AppSettings.shared.allowRemoteMCP
    @State private var enableCliAccess = AppSettings.shared.enableCliAccess
    @State private var enableCliAccessOnOpen = AppSettings.shared.enableCliAccess
    @State private var workerPreset = AppSettings.shared.workerPreset
    @State private var apiPortText = String(AppSettings.shared.apiPort)
    @State private var gatewayPortText = String(AppSettings.shared.gatewayPort)
    @State private var gatewayHostnameText = AppSettings.shared.gatewayHostname
    @State private var gatewayBindAddressesText = AppSettings.shared.gatewayBindAddresses.joined(separator: ", ")
    @State private var gatewayCertificateMode = AppSettings.shared.gatewayCertificateMode.rawValue
    @State private var gatewayCertificatePath = AppSettings.shared.gatewayCertificatePath
    @State private var gatewayPrivateKeyPath = AppSettings.shared.gatewayPrivateKeyPath
    @State private var detectedInterfaces: [LocalNetworkInterface] = []
    @State private var postgresPortText = String(AppSettings.shared.postgresPort)
    @State private var tikaPortText = String(AppSettings.shared.tikaPort)
    @State private var embedderPortText = String(AppSettings.shared.embedderPort)
    @State private var rerankerPortText = String(AppSettings.shared.rerankerPort)
    @State private var rerankerEnabled = AppSettings.shared.rerankerEnabled
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
        var gatewayPort = String(AppSettings.shared.gatewayPort)
        var gatewayHostname = AppSettings.shared.gatewayHostname
        var gatewayBindAddresses = AppSettings.shared.gatewayBindAddresses.joined(separator: ", ")
        var gatewayCertificateMode = AppSettings.shared.gatewayCertificateMode.rawValue
        var gatewayCertificatePath = AppSettings.shared.gatewayCertificatePath
        var gatewayPrivateKeyPath = AppSettings.shared.gatewayPrivateKeyPath
        var postgresPort = String(AppSettings.shared.postgresPort)
        var tikaPort = String(AppSettings.shared.tikaPort)
        var embedderPort = String(AppSettings.shared.embedderPort)
        var rerankerPort = String(AppSettings.shared.rerankerPort)
        var rerankerEnabled = AppSettings.shared.rerankerEnabled
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
                    Toggle("Allow harbor-clerk CLI from agentic tools", isOn: $enableCliAccess)
                        .onChange(of: enableCliAccess) { _, newValue in
                            // Applied immediately — no restart required. The API server
                            // re-reads config.json on every CLI request, so this takes
                            // effect within seconds without touching the restart banner.
                            AppSettings.shared.enableCliAccess = newValue
                        }
                    Text("Master switch for the harbor-clerk CLI (Claude Code, Codex, etc.). Like the MCP toggle above, individual clients still authenticate with API keys minted in System Settings → API Keys. No service restart required.")
                        .font(.caption)
                        .foregroundStyle(.secondary)

                    // Shim install is a client-side convenience — it drops a
                    // small shell script at ~/.local/bin/harbor-clerk that
                    // delegates to the bundled Python. The CLI toggle above
                    // is the actual access gate (server-side 403). Decoupling
                    // them means the Install button always fires; a user can
                    // pre-stage the binary and flip the toggle later.
                    CliShimRow()

                    VStack(alignment: .leading, spacing: 4) {
                        Text("Local MCP URL")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        Text(effectiveGatewayURL)
                            .font(.system(.caption, design: .monospaced))
                            .textSelection(.enabled)
                        Text("Uses Harbor Clerk's local HTTPS gateway. External binds expose only MCP token paths, not the web app.")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    .padding(.vertical, 2)

                    TextField("Gateway hostname", text: $gatewayHostnameText)
                        .textFieldStyle(.roundedBorder)
                        .onChange(of: gatewayHostnameText) { _, _ in markDirty() }
                    Text("Use localhost for same-Mac clients, a Tailscale/MagicDNS name for tailnet clients, or a real hostname when using a custom certificate.")
                        .font(.caption)
                        .foregroundStyle(.secondary)

                    HStack {
                        Text("Bind addresses")
                        Spacer()
                        TextField("", text: $gatewayBindAddressesText)
                            .textFieldStyle(.roundedBorder)
                            .font(.system(.body, design: .monospaced))
                            .frame(width: 220)
                            .onChange(of: gatewayBindAddressesText) { _, _ in markDirty() }
                    }
                    HStack {
                        Button("Loopback") {
                            gatewayBindAddressesText = GatewayConfig.defaultBindAddresses.joined(separator: ", ")
                            markDirty()
                        }
                        .controlSize(.small)
                        Button("All interfaces") {
                            gatewayBindAddressesText = "0.0.0.0, ::"
                            markDirty()
                        }
                        .controlSize(.small)
                        ForEach(Array(detectedInterfaces.filter(\.isTailscale).prefix(1))) { iface in
                            Button(iface.label) {
                                gatewayBindAddressesText = iface.address
                                markDirty()
                            }
                            .controlSize(.small)
                        }
                    }
                    Text(gatewayBindHelpText)
                        .font(.caption)
                        .foregroundStyle(gatewayUsesExternalBind ? .orange : .secondary)

                    Picker("Certificate", selection: $gatewayCertificateMode) {
                        Text("Generate local self-signed").tag(GatewayCertificateMode.internal.rawValue)
                        Text("Use certificate files").tag(GatewayCertificateMode.custom.rawValue)
                    }
                    .onChange(of: gatewayCertificateMode) { _, _ in markDirty() }

                    if gatewayCertificateMode == GatewayCertificateMode.custom.rawValue {
                        certificatePathRow(label: "Certificate", text: $gatewayCertificatePath)
                        certificatePathRow(label: "Private key", text: $gatewayPrivateKeyPath)
                        if !gatewayCustomCertificateComplete {
                            Text("Choose both a certificate file and private key before restarting the gateway.")
                                .font(.caption)
                                .foregroundStyle(.orange)
                        }
                        Text("ACME/Let's Encrypt automation is deferred; Harbor Clerk only reads the files you choose here.")
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
                    portRow(label: "HTTPS gateway port", text: $gatewayPortText, key: "gateway")
                    portRow(label: "PostgreSQL port", text: $postgresPortText, key: "postgres")
                    portRow(label: "Tika port", text: $tikaPortText, key: "tika")
                    portRow(label: "Embedder port", text: $embedderPortText, key: "embedder")
                    portRow(label: "Reranker port", text: $rerankerPortText, key: "reranker")
                    Toggle("Enable reranker", isOn: $rerankerEnabled)
                        .onChange(of: rerankerEnabled) { _, _ in markDirty() }
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
        .frame(width: 560, height: needsRestart ? 720 : 670)
        .animation(.easeInOut(duration: 0.25), value: needsRestart)
        .onAppear {
            // Sync enableCliAccess from AppSettings in case it changed since the view was created
            enableCliAccess = AppSettings.shared.enableCliAccess
            enableCliAccessOnOpen = AppSettings.shared.enableCliAccess
            detectedInterfaces = localNetworkInterfaces()
            captureInitial()
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
            .disabled(!gatewaySettingsValid)
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

    @ViewBuilder
    private func certificatePathRow(label: String, text: Binding<String>) -> some View {
        HStack {
            Text(label)
            Spacer()
            TextField("", text: text)
                .textFieldStyle(.roundedBorder)
                .font(.system(.body, design: .monospaced))
                .frame(width: 250)
                .onChange(of: text.wrappedValue) { _, _ in markDirty() }
            Button("Choose") {
                if let path = chooseCertificateFile() {
                    text.wrappedValue = path
                    markDirty()
                }
            }
            .controlSize(.small)
        }
    }

    private var effectiveGatewayURL: String {
        GatewayConfig.localBaseURL(
            hostname: gatewayHostnameText,
            gatewayPort: Int(gatewayPortText) ?? GatewayConfig.defaultPort
        )
    }

    private var gatewayBindAddresses: [String] {
        parseBindAddresses(gatewayBindAddressesText)
    }

    private var gatewayUsesExternalBind: Bool {
        !GatewayConfig.exposesFullApp(bindAddresses: gatewayBindAddresses)
    }

    private var gatewayBindHelpText: String {
        if gatewayUsesExternalBind {
            return "External binds expose only /mcp and /t API-key MCP paths. The web app, setup, status, and OAuth endpoints stay off this listener."
        }
        return "Loopback binds expose the local gateway only from this Mac."
    }

    private var gatewayCustomCertificateComplete: Bool {
        gatewayCertificateMode != GatewayCertificateMode.custom.rawValue
            || (!gatewayCertificatePath.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                && !gatewayPrivateKeyPath.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
    }

    private var gatewaySettingsValid: Bool {
        !GatewayConfig.normalizedHostname(gatewayHostnameText).isEmpty
            && !gatewayBindAddresses.isEmpty
            && gatewayCustomCertificateComplete
    }

    private func parseBindAddresses(_ value: String) -> [String] {
        let parts = value
            .split { $0 == "," || $0 == " " || $0 == "\n" || $0 == "\t" }
            .map { String($0).trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
        return GatewayConfig.normalizedBindAddresses(parts)
    }

    private func chooseCertificateFile() -> String? {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        panel.prompt = "Choose"
        return panel.runModal() == .OK ? panel.url?.path : nil
    }

    // MARK: - State management

    private func markDirty() {
        // Recompute against the initial snapshot rather than unconditionally
        // setting true. Two reasons:
        //   1. Cancel: revertToInitial() writes every @State back to its
        //      initial value. Each write fires its .onChange handler, which
        //      calls markDirty(). If markDirty just set true, those late
        //      handlers would clobber the explicit `needsRestart = false`
        //      revertToInitial set first, leaving the banner stuck open.
        //   2. Manual revert: if the user toggles a setting and then
        //      toggles it back, the banner correctly dismisses instead of
        //      staying open over no actual change.
        needsRestart = isDirty()
    }

    private func isDirty() -> Bool {
        return allowRemoteWeb != initial.allowRemoteWeb
            || allowRemoteMCP != initial.allowRemoteMCP
            || workerPreset != initial.workerPreset
            || apiPortText != initial.apiPort
            || gatewayPortText != initial.gatewayPort
            || gatewayHostnameText != initial.gatewayHostname
            || gatewayBindAddressesText != initial.gatewayBindAddresses
            || gatewayCertificateMode != initial.gatewayCertificateMode
            || gatewayCertificatePath != initial.gatewayCertificatePath
            || gatewayPrivateKeyPath != initial.gatewayPrivateKeyPath
            || postgresPortText != initial.postgresPort
            || tikaPortText != initial.tikaPort
            || embedderPortText != initial.embedderPort
            || rerankerPortText != initial.rerankerPort
            || rerankerEnabled != initial.rerankerEnabled
            || llamaPortText != initial.llamaPort
            || llmModelId != initial.llmModelId
            || logLevel != initial.logLevel
    }

    /// Write all local @State values to AppSettings (single save).
    private func applyToSettings() {
        let settings = AppSettings.shared
        settings.allowRemoteWeb = allowRemoteWeb
        settings.allowRemoteMCP = allowRemoteMCP
        settings.workerPreset = workerPreset
        if let p = Int(apiPortText), p > 0, p <= 65535 { settings.apiPort = p }
        if let p = Int(gatewayPortText), p > 0, p <= 65535 { settings.gatewayPort = p }
        settings.gatewayHostname = gatewayHostnameText
        settings.gatewayBindAddresses = gatewayBindAddresses
        settings.gatewayCertificateMode = GatewayCertificateMode(rawValue: gatewayCertificateMode) ?? .internal
        settings.gatewayCertificatePath = gatewayCertificatePath
        settings.gatewayPrivateKeyPath = gatewayPrivateKeyPath
        if let p = Int(postgresPortText), p > 0, p <= 65535 { settings.postgresPort = p }
        if let p = Int(tikaPortText), p > 0, p <= 65535 { settings.tikaPort = p }
        if let p = Int(embedderPortText), p > 0, p <= 65535 { settings.embedderPort = p }
        if let p = Int(rerankerPortText), p > 0, p <= 65535 { settings.rerankerPort = p }
        settings.rerankerEnabled = rerankerEnabled
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
            gatewayPort: gatewayPortText,
            gatewayHostname: gatewayHostnameText,
            gatewayBindAddresses: gatewayBindAddressesText,
            gatewayCertificateMode: gatewayCertificateMode,
            gatewayCertificatePath: gatewayCertificatePath,
            gatewayPrivateKeyPath: gatewayPrivateKeyPath,
            postgresPort: postgresPortText,
            tikaPort: tikaPortText,
            embedderPort: embedderPortText,
            rerankerPort: rerankerPortText,
            rerankerEnabled: rerankerEnabled,
            llamaPort: llamaPortText,
            llmModelId: llmModelId,
            logLevel: logLevel
        )
        // Track the enableCliAccess baseline for Cancel revert
        enableCliAccessOnOpen = enableCliAccess
    }

    private func revertToInitial() {
        allowRemoteWeb = initial.allowRemoteWeb
        allowRemoteMCP = initial.allowRemoteMCP
        workerPreset = initial.workerPreset
        apiPortText = initial.apiPort
        gatewayPortText = initial.gatewayPort
        gatewayHostnameText = initial.gatewayHostname
        gatewayBindAddressesText = initial.gatewayBindAddresses
        gatewayCertificateMode = initial.gatewayCertificateMode
        gatewayCertificatePath = initial.gatewayCertificatePath
        gatewayPrivateKeyPath = initial.gatewayPrivateKeyPath
        postgresPortText = initial.postgresPort
        tikaPortText = initial.tikaPort
        embedderPortText = initial.embedderPort
        rerankerPortText = initial.rerankerPort
        rerankerEnabled = initial.rerankerEnabled
        llamaPortText = initial.llamaPort
        llmModelId = initial.llmModelId
        logLevel = initial.logLevel
        // enableCliAccess is applied immediately when toggled, so Cancel must
        // also write the original value back to config.json to undo the change.
        enableCliAccess = enableCliAccessOnOpen
        AppSettings.shared.enableCliAccess = enableCliAccessOnOpen
        needsRestart = false
    }

    private func changedSettingKeys() -> Set<String> {
        var keys = Set<String>()
        if allowRemoteWeb != initial.allowRemoteWeb { keys.insert("allow_remote_web") }
        if allowRemoteMCP != initial.allowRemoteMCP { keys.insert("allow_remote_mcp") }
        if workerPreset != initial.workerPreset { keys.insert("worker_preset") }
        if apiPortText != initial.apiPort { keys.insert("api_port") }
        if gatewayPortText != initial.gatewayPort { keys.insert("gateway_port") }
        if gatewayHostnameText != initial.gatewayHostname { keys.insert("gateway_hostname") }
        if gatewayBindAddressesText != initial.gatewayBindAddresses { keys.insert("gateway_bind_addresses") }
        if gatewayCertificateMode != initial.gatewayCertificateMode { keys.insert("gateway_certificate_mode") }
        if gatewayCertificatePath != initial.gatewayCertificatePath { keys.insert("gateway_certificate_path") }
        if gatewayPrivateKeyPath != initial.gatewayPrivateKeyPath { keys.insert("gateway_private_key_path") }
        if postgresPortText != initial.postgresPort { keys.insert("postgres_port") }
        if tikaPortText != initial.tikaPort { keys.insert("tika_port") }
        if embedderPortText != initial.embedderPort { keys.insert("embedder_port") }
        if rerankerPortText != initial.rerankerPort { keys.insert("reranker_port") }
        if rerankerEnabled != initial.rerankerEnabled { keys.insert("reranker_enabled") }
        if llamaPortText != initial.llamaPort { keys.insert("llama_port") }
        if llmModelId != initial.llmModelId { keys.insert("llm_model_id") }
        if logLevel != initial.logLevel { keys.insert("log_level") }
        return keys
    }
}

extension Notification.Name {
    static let preferencesRequestRestart = Notification.Name("preferencesRequestRestart")
}

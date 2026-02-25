import AppKit
import SwiftUI

struct StatusWindow: View {
    @ObservedObject var serviceManager: ServiceManager

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

            Spacer(minLength: 8)

            // Control bar (always visible at bottom)
            controlBar
                .padding(.horizontal, 20)
                .padding(.bottom, 16)
        }
        .frame(minWidth: 600, minHeight: 380)
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

            Button {
                if let url = NSWorkspace.shared.urlForApplication(
                    withBundleIdentifier: "com.apple.Console"
                ) {
                    NSWorkspace.shared.openApplication(at: url, configuration: .init())
                }
            } label: {
                Label("View Logs in Console", systemImage: "terminal")
            }
            .controlSize(.large)
            .buttonStyle(.bordered)
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
}

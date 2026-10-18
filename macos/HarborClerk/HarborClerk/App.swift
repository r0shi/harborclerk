import SwiftUI

@main
struct HarborClerkApp: App {
    var body: some Scene {
        WindowGroup {
            if Self.showsLiveRoot(environment: ProcessInfo.processInfo.environment) {
                ContentView()
            } else {
                InertHostView()
            }
        }
        .defaultSize(width: 1200, height: 800)
        .commands {
            CommandMenu("Navigate") {
                Button("Back") {
                    NotificationCenter.default.post(name: .webViewGoBack, object: nil)
                }
                .keyboardShortcut("[", modifiers: .command)

                Button("Forward") {
                    NotificationCenter.default.post(name: .webViewGoForward, object: nil)
                }
                .keyboardShortcut("]", modifiers: .command)
            }
            CommandMenu("View") {
                Button("Reload Page") {
                    NotificationCenter.default.post(name: .webViewReload, object: nil)
                }
                .keyboardShortcut("r", modifiers: .command)

                Divider()

                Button("Zoom In") {
                    NotificationCenter.default.post(name: .webViewZoomIn, object: nil)
                }
                .keyboardShortcut("+", modifiers: .command)

                Button("Zoom Out") {
                    NotificationCenter.default.post(name: .webViewZoomOut, object: nil)
                }
                .keyboardShortcut("-", modifiers: .command)

                Button("Actual Size") {
                    NotificationCenter.default.post(name: .webViewZoomReset, object: nil)
                }
                .keyboardShortcut("0", modifiers: .command)
            }
        }
    }
}

extension HarborClerkApp {
    /// The first of two start-nothing guards: with `XCTestConfigurationFilePath`
    /// set (the XCTest runner, notarize.sh's launch probe) the window holds
    /// `InertHostView`, never `ContentView`, which polls the server, and
    /// whose AuthManager reads the Keychain, logs in and writes the session
    /// cookie into the WebKit store. AuthManager checks the same switch again
    /// (the second guard), so breaking either one alone reaches nothing live.
    static func showsLiveRoot(environment: [String: String]) -> Bool {
        environment["XCTestConfigurationFilePath"] == nil
    }
}

/// Which root views have appeared in this process. Read by the test that the
/// test host shows the inert one; nothing in the app uses it.
@MainActor
enum RootViewLog {
    static var inertAppeared = false
    static var liveAppeared = false
}

/// The whole window under the start-nothing switch: no polling, no Keychain,
/// no network, no WebKit.
struct InertHostView: View {
    var body: some View {
        Text("Harbor Clerk")
            .frame(minWidth: 200, minHeight: 100)
            .onAppear { RootViewLog.inertAppeared = true }
    }
}

extension Notification.Name {
    static let webViewGoBack = Notification.Name("webViewGoBack")
    static let webViewGoForward = Notification.Name("webViewGoForward")
    static let webViewZoomIn = Notification.Name("webViewZoomIn")
    static let webViewZoomOut = Notification.Name("webViewZoomOut")
    static let webViewZoomReset = Notification.Name("webViewZoomReset")
    static let webViewReload = Notification.Name("webViewReload")
}

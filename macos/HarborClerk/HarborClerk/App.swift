import SwiftUI

@main
struct HarborClerkApp: App {
    var body: some Scene {
        WindowGroup {
            ContentView()
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

extension Notification.Name {
    static let webViewGoBack = Notification.Name("webViewGoBack")
    static let webViewGoForward = Notification.Name("webViewGoForward")
    static let webViewZoomIn = Notification.Name("webViewZoomIn")
    static let webViewZoomOut = Notification.Name("webViewZoomOut")
    static let webViewZoomReset = Notification.Name("webViewZoomReset")
}

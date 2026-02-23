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
        }
    }
}

extension Notification.Name {
    static let webViewGoBack = Notification.Name("webViewGoBack")
    static let webViewGoForward = Notification.Name("webViewGoForward")
}

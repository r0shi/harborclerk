import SwiftUI

@main
struct HarborClerkServerApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var appDelegate

    var body: some Scene {
        // No default window — menubar-only agent app
        Settings {
            EmptyView()
        }
    }
}

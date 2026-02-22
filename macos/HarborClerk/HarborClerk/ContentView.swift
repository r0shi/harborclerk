import SwiftUI
import WebKit

struct ContentView: View {
    @StateObject private var detector = BackendDetector()

    var body: some View {
        Group {
            if detector.isAvailable {
                WebView(url: detector.baseURL)
                    .ignoresSafeArea()
            } else {
                VStack(spacing: 20) {
                    Image(systemName: "server.rack")
                        .font(.system(size: 48))
                        .foregroundColor(.secondary)

                    Text("Waiting for Harbor Clerk Server...")
                        .font(.title2)

                    Text("Please start Harbor Clerk Server to continue.")
                        .foregroundColor(.secondary)

                    Button("Launch Harbor Clerk Server") {
                        launchServer()
                    }
                    .buttonStyle(.borderedProminent)
                    .controlSize(.large)

                    ProgressView()
                        .controlSize(.small)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .onAppear { detector.startPolling() }
        .onDisappear { detector.stopPolling() }
    }

    private func launchServer() {
        let bundleID = "com.harborclerk.HarborClerkServer"
        if let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: bundleID) {
            NSWorkspace.shared.openApplication(at: url, configuration: .init())
        }
    }
}

/// NSViewRepresentable wrapper for WKWebView.
struct WebView: NSViewRepresentable {
    let url: URL

    func makeNSView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        let webView = WKWebView(frame: .zero, configuration: config)
        webView.load(URLRequest(url: url))
        return webView
    }

    func updateNSView(_ webView: WKWebView, context: Context) {
        // Reload only if URL changed
        if webView.url != url {
            webView.load(URLRequest(url: url))
        }
    }
}

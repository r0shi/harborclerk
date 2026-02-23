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

    func makeCoordinator() -> Coordinator {
        Coordinator()
    }

    func makeNSView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        // Use the default (persistent) data store so credentials and
        // AutoFill state are preserved across launches.
        config.websiteDataStore = .default()
        // Allow text interaction so the system credential provider
        // (Keychain, 1Password, etc.) can detect and fill form fields.
        config.preferences.isTextInteractionEnabled = true

        let webView = WKWebView(frame: .zero, configuration: config)
        webView.uiDelegate = context.coordinator
        webView.load(URLRequest(url: url))

        // Store reference for navigation commands
        context.coordinator.webView = webView

        return webView
    }

    func updateNSView(_ webView: WKWebView, context: Context) {
        if webView.url != url {
            webView.load(URLRequest(url: url))
        }
    }

    class Coordinator: NSObject, WKUIDelegate {
        weak var webView: WKWebView?
        private var observers: [NSObjectProtocol] = []

        override init() {
            super.init()

            observers.append(
                NotificationCenter.default.addObserver(
                    forName: .webViewGoBack, object: nil, queue: .main
                ) { [weak self] _ in
                    self?.webView?.goBack()
                }
            )
            observers.append(
                NotificationCenter.default.addObserver(
                    forName: .webViewGoForward, object: nil, queue: .main
                ) { [weak self] _ in
                    self?.webView?.goForward()
                }
            )
        }

        deinit {
            for observer in observers {
                NotificationCenter.default.removeObserver(observer)
            }
        }

        // Respond to new-window requests (e.g. target="_blank" links)
        // by loading them in the same web view.
        func webView(
            _ webView: WKWebView,
            createWebViewWith configuration: WKWebViewConfiguration,
            for navigationAction: WKNavigationAction,
            windowFeatures: WKWindowFeatures
        ) -> WKWebView? {
            if navigationAction.targetFrame == nil || navigationAction.targetFrame?.isMainFrame == false {
                webView.load(navigationAction.request)
            }
            return nil
        }
    }
}

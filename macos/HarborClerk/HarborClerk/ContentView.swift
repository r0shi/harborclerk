import SwiftUI
import WebKit

struct ContentView: View {
    @StateObject private var detector = BackendDetector()
    @StateObject private var authManager: AuthManager

    init() {
        let port = BackendDetector.readPort()
        let url = URL(string: "http://localhost:\(port)")!
        _authManager = StateObject(wrappedValue: AuthManager(baseURL: url))
    }

    var body: some View {
        Group {
            switch authManager.state {
            case .waitingForServer:
                WaitingView(launchServer: launchServer)

            case .checkingAuth:
                ProgressView("Signing in...")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)

            case .loginRequired(let errorMessage):
                LoginView(errorMessage: errorMessage)
                    .environmentObject(authManager)

            case .loggingIn:
                ProgressView("Signing in...")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)

            case .needsSetup:
                WebView(url: authManager.baseURL, authManager: authManager)
                    .ignoresSafeArea()

            case .authenticated:
                WebView(url: authManager.baseURL, authManager: authManager)
                    .ignoresSafeArea()
            }
        }
        .onAppear { detector.startPolling() }
        .onDisappear { detector.stopPolling() }
        .onChange(of: detector.isAvailable) { _, available in
            if available {
                Task { await authManager.onServerBecameAvailable() }
            } else {
                authManager.state = .waitingForServer
            }
        }
    }

    private func launchServer() {
        let bundleID = "com.harborclerk.HarborClerkServer"
        if let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: bundleID) {
            NSWorkspace.shared.openApplication(at: url, configuration: .init())
        }
    }
}

// MARK: - Waiting View

private struct WaitingView: View {
    let launchServer: () -> Void

    var body: some View {
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

// MARK: - WebView

/// NSViewRepresentable wrapper for WKWebView.
struct WebView: NSViewRepresentable {
    let url: URL
    let authManager: AuthManager

    func makeCoordinator() -> Coordinator {
        Coordinator(authManager: authManager)
    }

    func makeNSView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()
        config.preferences.isTextInteractionEnabled = true

        let webView = WKWebView(frame: .zero, configuration: config)
        webView.uiDelegate = context.coordinator
        webView.navigationDelegate = context.coordinator
        webView.load(URLRequest(url: url))

        context.coordinator.webView = webView
        return webView
    }

    func updateNSView(_ webView: WKWebView, context: Context) {
        // Only reload if the base URL changed (not on every SwiftUI update)
    }

    class Coordinator: NSObject, WKUIDelegate, WKNavigationDelegate {
        weak var webView: WKWebView?
        private var observers: [NSObjectProtocol] = []
        private let authManager: AuthManager

        init(authManager: AuthManager) {
            self.authManager = authManager
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

        // Intercept navigation to /login — means the web UI logged out (or setup finished)
        func webView(
            _ webView: WKWebView,
            decidePolicyFor navigationAction: WKNavigationAction,
            decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
        ) {
            if let navURL = navigationAction.request.url,
               navURL.path == "/login" || navURL.path == "/login/" {
                decisionHandler(.cancel)
                Task { @MainActor in
                    if case .needsSetup = authManager.state {
                        authManager.handleSetupComplete()
                    } else {
                        authManager.handleWebLogout()
                    }
                }
                return
            }
            decisionHandler(.allow)
        }
    }
}

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
        webView.allowsMagnification = true
        webView.allowsBackForwardNavigationGestures = true
        webView.uiDelegate = context.coordinator
        webView.load(URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData))

        context.coordinator.webView = webView
        context.coordinator.startObservingURL()
        return webView
    }

    func updateNSView(_ webView: WKWebView, context: Context) {
        // Only reload if the base URL changed (not on every SwiftUI update)
    }

    class Coordinator: NSObject, WKUIDelegate {
        weak var webView: WKWebView?
        private var observers: [NSObjectProtocol] = []
        private var urlObservation: NSKeyValueObservation?
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
            observers.append(
                NotificationCenter.default.addObserver(
                    forName: .webViewZoomIn, object: nil, queue: .main
                ) { [weak self] _ in
                    guard let wv = self?.webView else { return }
                    wv.magnification = min(wv.magnification + 0.1, 3.0)
                }
            )
            observers.append(
                NotificationCenter.default.addObserver(
                    forName: .webViewZoomOut, object: nil, queue: .main
                ) { [weak self] _ in
                    guard let wv = self?.webView else { return }
                    wv.magnification = max(wv.magnification - 0.1, 0.5)
                }
            )
            observers.append(
                NotificationCenter.default.addObserver(
                    forName: .webViewZoomReset, object: nil, queue: .main
                ) { [weak self] _ in
                    self?.webView?.magnification = 1.0
                }
            )
        }

        /// Observe the webView's URL via KVO to detect client-side (pushState)
        /// navigations to /login, which WKNavigationDelegate doesn't catch.
        func startObservingURL() {
            urlObservation = webView?.observe(\.url, options: [.new]) { [weak self] _, change in
                guard let self,
                      let newURL = change.newValue ?? nil,
                      let path = newURL.path.nilIfEmpty
                else { return }

                if path == "/login" || path == "/login/" {
                    Task { @MainActor in
                        if case .needsSetup = self.authManager.state {
                            self.authManager.handleSetupComplete()
                        } else {
                            self.authManager.handleWebLogout()
                        }
                    }
                }
            }
        }

        deinit {
            urlObservation?.invalidate()
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
    }
}

private extension String {
    var nilIfEmpty: String? { isEmpty ? nil : self }
}

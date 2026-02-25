import Foundation
import WebKit

enum AuthState: Equatable {
    case waitingForServer
    case checkingAuth
    case loginRequired(errorMessage: String?)
    case loggingIn
    case authenticated
    /// First-time setup — load the web UI directly so the user can create the admin account.
    case needsSetup
}

/// Owns the authentication state machine: Keychain auto-login, API calls, cookie injection.
@MainActor
final class AuthManager: ObservableObject {
    @Published var state: AuthState = .waitingForServer

    let baseURL: URL

    init(baseURL: URL) {
        self.baseURL = baseURL
    }

    // MARK: - Server became available

    func onServerBecameAvailable() async {
        state = .checkingAuth

        // 1. Check if first-time setup is needed
        if await needsSetup() {
            state = .needsSetup
            return
        }

        // 2. Try auto-login from Keychain
        if let creds = KeychainManager.load() {
            do {
                try await performLogin(email: creds.email, password: creds.password)
                return // state is now .authenticated
            } catch {
                // Saved credentials are stale — clear and show login
                KeychainManager.delete()
            }
        }

        state = .loginRequired(errorMessage: nil)
    }

    // MARK: - Manual login

    func login(email: String, password: String, rememberMe: Bool) async {
        state = .loggingIn
        do {
            try await performLogin(email: email, password: password)
            if rememberMe {
                KeychainManager.save(email: email, password: password)
            }
        } catch let error as LoginError {
            state = .loginRequired(errorMessage: error.message)
        } catch {
            state = .loginRequired(errorMessage: "Connection failed. Is the server running?")
        }
    }

    // MARK: - Logout (called when web UI navigates to /login)

    func handleWebLogout() {
        // Clear WKWebView cookies
        let dataStore = WKWebsiteDataStore.default()
        dataStore.httpCookieStore.getAllCookies { cookies in
            for cookie in cookies where cookie.name == "refresh_token" {
                dataStore.httpCookieStore.delete(cookie)
            }
        }
        state = .loginRequired(errorMessage: nil)
    }

    // MARK: - After setup completes (web navigated to /login)

    func handleSetupComplete() {
        state = .loginRequired(errorMessage: nil)
    }

    // MARK: - Private

    private func needsSetup() async -> Bool {
        let url = baseURL.appendingPathComponent("api/system/setup-status")
        guard let (data, response) = try? await URLSession.shared.data(from: url),
              (response as? HTTPURLResponse)?.statusCode == 200,
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let needs = json["needs_setup"] as? Bool
        else {
            return false
        }
        return needs
    }

    private func performLogin(email: String, password: String) async throws {
        let url = baseURL.appendingPathComponent("api/auth/login")
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")

        let body = ["email": email, "password": password]
        request.httpBody = try JSONSerialization.data(withJSONObject: body)

        let (_, response) = try await URLSession.shared.data(for: request)
        guard let httpResponse = response as? HTTPURLResponse else {
            throw LoginError(message: "Unexpected response")
        }

        if httpResponse.statusCode == 401 {
            throw LoginError(message: "Invalid email or password")
        }
        if httpResponse.statusCode == 403 {
            throw LoginError(message: "Account disabled")
        }
        guard httpResponse.statusCode == 200 else {
            throw LoginError(message: "Login failed (HTTP \(httpResponse.statusCode))")
        }

        // Transfer the refresh_token cookie from URLSession's shared cookie storage
        // into WKWebView's cookie store so the React SPA can authenticate.
        await injectCookiesIntoWebView()
        state = .authenticated
    }

    private func injectCookiesIntoWebView() async {
        guard let cookies = HTTPCookieStorage.shared.cookies(for: baseURL) else { return }

        let webCookieStore = WKWebsiteDataStore.default().httpCookieStore
        for cookie in cookies {
            // If the server hasn't been updated yet and sends Secure on http://localhost,
            // create a non-Secure copy so the cookie actually sticks in WKWebView.
            var props = cookie.properties ?? [:]
            if baseURL.scheme == "http" {
                props.removeValue(forKey: .secure)
            }
            if let fixedCookie = HTTPCookie(properties: props) {
                await webCookieStore.setCookie(fixedCookie)
            } else {
                await webCookieStore.setCookie(cookie)
            }
        }
    }
}

private struct LoginError: Error {
    let message: String
}

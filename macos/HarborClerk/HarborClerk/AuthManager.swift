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

        // Use a session that does NOT handle cookies itself, so the raw
        // Set-Cookie header is preserved in the response for us to parse.
        let ephemeral = URLSessionConfiguration.ephemeral
        ephemeral.httpCookieAcceptPolicy = .never
        ephemeral.httpShouldSetCookies = false
        let session = URLSession(configuration: ephemeral)

        let (data, response) = try await session.data(for: request)
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

        // Parse the JSON to verify login succeeded (we don't need the access_token
        // since the SPA will get its own via the refresh cookie).
        guard let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              json["access_token"] != nil else {
            throw LoginError(message: "Unexpected response format")
        }

        // Extract Set-Cookie from response headers and inject into WKWebView.
        await injectCookiesFromResponse(httpResponse, url: url)
        state = .authenticated
    }

    private func injectCookiesFromResponse(_ response: HTTPURLResponse, url: URL) async {
        // Extract the raw Set-Cookie header value.
        let headerFields = response.allHeaderFields
        var setCookieValue: String? = nil
        for (key, value) in headerFields {
            if let k = key as? String, k.lowercased() == "set-cookie", let v = value as? String {
                setCookieValue = v
                break
            }
        }
        guard let rawHeader = setCookieValue else { return }

        // HTTPCookie.cookies(withResponseHeaderFields:for:) silently rejects
        // Secure cookies when the URL scheme is http. We must parse manually.
        let parts = rawHeader.split(separator: ";").map { $0.trimmingCharacters(in: .whitespaces) }
        guard let nameValue = parts.first, let eqIdx = nameValue.firstIndex(of: "=") else { return }

        let cookieName = String(nameValue[nameValue.startIndex..<eqIdx])
        let cookieValue = String(nameValue[nameValue.index(after: eqIdx)...])

        var props: [HTTPCookiePropertyKey: Any] = [
            .name: cookieName,
            .value: cookieValue,
            .domain: url.host ?? "localhost",
            .path: "/api/auth",
        ]

        for part in parts.dropFirst() {
            let lower = part.lowercased()
            if lower.hasPrefix("path=") {
                props[.path] = String(part.dropFirst(5))
            } else if lower.hasPrefix("max-age=") {
                if let seconds = Int(part.dropFirst(8)) {
                    props[.maximumAge] = String(seconds)
                }
            } else if lower.hasPrefix("domain=") {
                props[.domain] = String(part.dropFirst(7))
            } else if lower == "secure" {
                // Only set Secure when actually on HTTPS
                if baseURL.scheme == "https" {
                    props[.secure] = "TRUE"
                }
            }
        }

        guard let cookie = HTTPCookie(properties: props) else { return }

        let webCookieStore = WKWebsiteDataStore.default().httpCookieStore
        await webCookieStore.setCookie(cookie)
    }
}

private struct LoginError: Error {
    let message: String
}

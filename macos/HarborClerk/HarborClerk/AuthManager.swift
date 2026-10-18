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

/// Where AuthManager keeps the remembered login. Production is the Keychain;
/// a test passes a fake, so it never reads or deletes the developer's real
/// saved login (#612).
protocol CredentialStore {
    func load() -> KeychainCredentials?
    func save(email: String, password: String)
    func delete()
}

struct KeychainCredentialStore: CredentialStore {
    func load() -> KeychainCredentials? { KeychainManager.load() }
    func save(email: String, password: String) { KeychainManager.save(email: email, password: password) }
    func delete() { KeychainManager.delete() }
}

/// The two requests AuthManager makes, so a test can answer them without a
/// server.
struct AuthTransport {
    var setupStatus: (URL) async throws -> (Data, URLResponse)
    var login: (URLRequest) async throws -> (Data, URLResponse)

    static let live = AuthTransport(
        setupStatus: { url in try await URLSession.shared.data(from: url) },
        login: { request in
            // Use a session that does NOT handle cookies itself, so the raw
            // Set-Cookie header is preserved in the response for us to parse.
            let ephemeral = URLSessionConfiguration.ephemeral
            ephemeral.httpCookieAcceptPolicy = .never
            ephemeral.httpShouldSetCookies = false
            let session = URLSession(configuration: ephemeral)
            return try await session.data(for: request)
        }
    )
}

/// Owns the authentication state machine: Keychain auto-login, API calls, cookie injection.
@MainActor
final class AuthManager: ObservableObject {
    @Published var state: AuthState = .waitingForServer

    let baseURL: URL

    private static let loggedOutKey = "didLogOut"

    private let environment: [String: String]
    private let credentials: CredentialStore
    private let defaults: UserDefaults
    private let transport: AuthTransport

    /// Everything but `baseURL` defaults to the real thing. A test injects the
    /// environment (to keep or drop the start-nothing switch), a fake
    /// credential store, a defaults suite of its own and a canned transport.
    init(
        baseURL: URL,
        environment: [String: String] = ProcessInfo.processInfo.environment,
        credentials: CredentialStore = KeychainCredentialStore(),
        defaults: UserDefaults = .standard,
        transport: AuthTransport = .live
    ) {
        self.baseURL = baseURL
        self.environment = environment
        self.credentials = credentials
        self.defaults = defaults
        self.transport = transport
    }

    // MARK: - Server became available

    func onServerBecameAvailable() async {
        // Same start-nothing switch the server's AppDelegate honours. The
        // signing launch probe in notarize.sh execs this app for two seconds and
        // kills it; without this, that ran the real path below, and the catch-all
        // treated "killed mid-request" like stale credentials and deleted the
        // user's saved login from the Keychain.
        // `.waitingForServer`, not `.loginRequired`: LoginView's onAppear reads the
        // saved login from the Keychain, and a freshly Developer-ID-signed probe
        // binary has a different designated requirement from the build that stored
        // it — so that read raises the ACL prompt in the middle of `make sign`.
        // WaitingView touches nothing.
        if environment["XCTestConfigurationFilePath"] != nil {
            state = .waitingForServer
            return
        }

        state = .checkingAuth

        // 1. Check if first-time setup is needed
        if await needsSetup() {
            state = .needsSetup
            return
        }

        // 2. Try auto-login from Keychain (skip if user explicitly logged out)
        let wasLoggedOut = defaults.bool(forKey: Self.loggedOutKey)
        if !wasLoggedOut, let creds = credentials.load() {
            do {
                try await performLogin(email: creds.email, password: creds.password)
                return // state is now .authenticated
            } catch let error as LoginError where error.credentialsRejected {
                // The server said these credentials are wrong (the password
                // was changed, the user removed): they are stale, so forget them.
                credentials.delete()
            } catch {
                // Anything else says nothing about the credentials: the API
                // restarting mid-login, a 5xx, a body we could not read, a
                // disabled account. Deleting here wiped the remembered password
                // whenever the server blinked (#612). Keep it, say why the
                // sign-in did not happen, and let the user retry.
                state = .loginRequired(errorMessage: Self.message(for: error))
                return
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
                credentials.save(email: email, password: password)
            }
            // Clear logged-out flag on successful login
            defaults.removeObject(forKey: Self.loggedOutKey)
        } catch {
            state = .loginRequired(errorMessage: Self.message(for: error))
        }
    }

    /// What the login screen says about a failed sign-in.
    private static func message(for error: Error) -> String {
        (error as? LoginError)?.message ?? "Connection failed. Is the server running?"
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
        // Remember that user explicitly logged out so we don't auto-login on next launch
        defaults.set(true, forKey: Self.loggedOutKey)
        state = .loginRequired(errorMessage: nil)
    }

    // MARK: - After setup completes (web navigated to /login)

    func handleSetupComplete() {
        state = .loginRequired(errorMessage: nil)
    }

    // MARK: - Private

    private func needsSetup() async -> Bool {
        let url = baseURL.appendingPathComponent("api/system/setup-status")
        guard let (data, response) = try? await transport.setupStatus(url),
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

        let (data, response) = try await transport.login(request)
        guard let httpResponse = response as? HTTPURLResponse else {
            throw LoginError(message: "Unexpected response")
        }

        // 401 is the API's answer to a wrong email or password
        // (`POST /api/auth/login`), and the only answer that condemns the
        // credentials. Its 403 means the account is disabled, which a correct
        // password still gets.
        if httpResponse.statusCode == 401 {
            throw LoginError(message: "Invalid email or password", credentialsRejected: true)
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
    /// The server rejected the email or password themselves, as opposed to
    /// failing to answer, answering with an error, or answering oddly.
    var credentialsRejected = false
}

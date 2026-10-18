import XCTest
@testable import HarborClerk

/// Counts every call AuthManager makes on the saved login. Stands in for the
/// Keychain, so no test reads or deletes the developer's real saved login.
final class FakeCredentialStore: CredentialStore {
    var stored: KeychainCredentials?
    private(set) var loads = 0
    private(set) var saves = 0
    private(set) var deletes = 0

    init(stored: KeychainCredentials?) {
        self.stored = stored
    }

    var calls: Int { loads + saves + deletes }

    func load() -> KeychainCredentials? {
        loads += 1
        return stored
    }

    func save(email: String, password: String) {
        saves += 1
        stored = KeychainCredentials(email: email, password: password)
    }

    func delete() {
        deletes += 1
        stored = nil
    }
}

/// The saved login survives everything but the server rejecting it (#612),
/// and the signing probe's start-nothing switch keeps AuthManager away from
/// it altogether (#611).
///
/// No test here lets a login succeed: that path injects the refresh cookie
/// into `WKWebsiteDataStore.default()`, which for this test host is the same
/// store the developer's installed app uses.
@MainActor
final class AuthManagerTests: XCTestCase {

    private static let baseURL = URL(string: "http://127.0.0.1:1")!
    private static let saved = KeychainCredentials(email: "owner@example.test", password: "not-a-real-password")
    /// The switch the XCTest runner and `notarize.sh`'s launch probe set.
    private static let probeEnvironment = ["XCTestConfigurationFilePath": "/dev/null"]

    private var defaults: UserDefaults!
    private var suiteName: String!
    private var setupCalls = 0
    private var loginCalls = 0

    override func setUp() {
        super.setUp()
        // Not `.standard`: under this test host that is the installed app's
        // defaults domain, whose logged-out flag the tests must not read.
        suiteName = "AuthManagerTests-\(UUID().uuidString)"
        defaults = UserDefaults(suiteName: suiteName)
        setupCalls = 0
        loginCalls = 0
    }

    override func tearDown() {
        defaults.removePersistentDomain(forName: suiteName)
        super.tearDown()
    }

    /// Setup already done; the login request answered by `answer`.
    private func transport(
        _ answer: @escaping (URLRequest) throws -> (Data, URLResponse)
    ) -> AuthTransport {
        AuthTransport(
            setupStatus: { [unowned self] url in
                setupCalls += 1
                let body = try JSONSerialization.data(withJSONObject: ["needs_setup": false])
                return (body, HTTPURLResponse(url: url, statusCode: 200, httpVersion: nil, headerFields: nil)!)
            },
            login: { [unowned self] request in
                loginCalls += 1
                return try answer(request)
            }
        )
    }

    private static func status(_ code: Int, body: String = "{}") -> (URLRequest) throws -> (Data, URLResponse) {
        { request in
            (Data(body.utf8), HTTPURLResponse(url: request.url!, statusCode: code, httpVersion: nil, headerFields: nil)!)
        }
    }

    private func makeManager(
        environment: [String: String] = [:],
        store: FakeCredentialStore,
        login answer: @escaping (URLRequest) throws -> (Data, URLResponse)
    ) -> AuthManager {
        AuthManager(
            baseURL: Self.baseURL,
            environment: environment,
            credentials: store,
            defaults: defaults,
            transport: transport(answer)
        )
    }

    /// Run `body` and fail the test if it has not returned within `seconds`.
    private func bounded(_ what: String, seconds: TimeInterval = 10, _ body: @escaping @MainActor () async -> Void) async {
        let returned = expectation(description: "\(what) returned within \(seconds)s")
        let task = Task { @MainActor in
            await body()
            returned.fulfill()
        }
        await fulfillment(of: [returned], timeout: seconds)
        task.cancel()
    }

    // MARK: - The start-nothing switch (#611, #612)

    func testWithTheSwitchSetNothingTouchesTheSavedLogin() async {
        let store = FakeCredentialStore(stored: Self.saved)
        // A login that fails as the probe's kill did; the switch must keep it from ever being asked.
        let auth = makeManager(environment: Self.probeEnvironment, store: store) { _ in
            throw URLError(.networkConnectionLost)
        }

        await bounded("onServerBecameAvailable") { await auth.onServerBecameAvailable() }

        XCTAssertEqual(auth.state, .waitingForServer)
        XCTAssertEqual(store.calls, 0, "the signing probe must not read, save or delete the saved login")
        XCTAssertEqual(setupCalls + loginCalls, 0, "nor make a request")
        XCTAssertEqual(store.stored?.email, Self.saved.email)
    }

    /// The other half of the guard's test: without the switch the same
    /// manager does reach the saved login, so the test above fails if the
    /// switch is removed rather than passing for some other reason.
    func testWithoutTheSwitchARejectedSavedLoginIsDeleted() async {
        let store = FakeCredentialStore(stored: Self.saved)
        let auth = makeManager(store: store, login: Self.status(401, body: #"{"detail":"Invalid email or password"}"#))

        await bounded("onServerBecameAvailable") { await auth.onServerBecameAvailable() }

        XCTAssertEqual(store.loads, 1)
        XCTAssertEqual(loginCalls, 1)
        XCTAssertEqual(store.deletes, 1, "a 401 means the saved password is wrong; forget it")
        XCTAssertNil(store.stored)
        XCTAssertEqual(auth.state, .loginRequired(errorMessage: nil))
    }

    // MARK: - Only a rejection deletes the saved login (#612)

    func testAConnectionDroppedMidLoginKeepsTheSavedLogin() async {
        let store = FakeCredentialStore(stored: Self.saved)
        let auth = makeManager(store: store) { _ in throw URLError(.networkConnectionLost) }

        await bounded("onServerBecameAvailable") { await auth.onServerBecameAvailable() }

        XCTAssertEqual(loginCalls, 1)
        XCTAssertEqual(store.deletes, 0, "the API restarting mid-login says nothing about the password")
        XCTAssertEqual(store.stored?.email, Self.saved.email)
        XCTAssertEqual(auth.state, .loginRequired(errorMessage: "Connection failed. Is the server running?"))
    }

    func testAServerErrorKeepsTheSavedLogin() async {
        let store = FakeCredentialStore(stored: Self.saved)
        let auth = makeManager(store: store, login: Self.status(503))

        await bounded("onServerBecameAvailable") { await auth.onServerBecameAvailable() }

        XCTAssertEqual(store.deletes, 0)
        XCTAssertEqual(auth.state, .loginRequired(errorMessage: "Login failed (HTTP 503)"))
    }

    func testAnUnreadableSuccessKeepsTheSavedLogin() async {
        let store = FakeCredentialStore(stored: Self.saved)
        let auth = makeManager(store: store, login: Self.status(200, body: "<html>proxy error</html>"))

        await bounded("onServerBecameAvailable") { await auth.onServerBecameAvailable() }

        XCTAssertEqual(store.deletes, 0)
        XCTAssertEqual(auth.state, .loginRequired(errorMessage: "Unexpected response format"))
    }

    func testADisabledAccountKeepsTheSavedLogin() async {
        // The API answers 403 for a correct password on a disabled account.
        let store = FakeCredentialStore(stored: Self.saved)
        let auth = makeManager(store: store, login: Self.status(403, body: #"{"detail":"Account disabled"}"#))

        await bounded("onServerBecameAvailable") { await auth.onServerBecameAvailable() }

        XCTAssertEqual(store.deletes, 0)
        XCTAssertEqual(auth.state, .loginRequired(errorMessage: "Account disabled"))
    }

    func testAfterAnExplicitLogoutTheSavedLoginIsNotTried() async {
        let store = FakeCredentialStore(stored: Self.saved)
        defaults.set(true, forKey: "didLogOut")
        let auth = makeManager(store: store, login: Self.status(401))

        await bounded("onServerBecameAvailable") { await auth.onServerBecameAvailable() }

        XCTAssertEqual(store.calls, 0)
        XCTAssertEqual(loginCalls, 0)
        XCTAssertEqual(auth.state, .loginRequired(errorMessage: nil))
    }
}

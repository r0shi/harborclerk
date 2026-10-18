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
/// Every transport here records cookies instead of writing them: the live
/// sink is `WKWebsiteDataStore.default()`, which for this test host is the
/// same store the developer's installed app uses.
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
    private var cookiesSet: [HTTPCookie] = []
    /// Run inside the cookie write, after recording it.
    private var duringSetCookie: (@MainActor () -> Void)?
    /// What the setup-status request answers for `needs_setup`; may suspend.
    private var setupAnswer: (@MainActor () async -> Bool)?

    override func setUp() {
        super.setUp()
        // Not `.standard`: under this test host that is the installed app's
        // defaults domain, whose logged-out flag the tests must not read.
        suiteName = "AuthManagerTests-\(UUID().uuidString)"
        defaults = UserDefaults(suiteName: suiteName)
        setupCalls = 0
        loginCalls = 0
        cookiesSet = []
        duringSetCookie = nil
        setupAnswer = nil
    }

    override func tearDown() {
        defaults.removePersistentDomain(forName: suiteName)
        super.tearDown()
    }

    /// Setup already done; the login request answered by `answer`.
    private func transport(
        _ answer: @escaping (URLRequest) async throws -> (Data, URLResponse)
    ) -> AuthTransport {
        AuthTransport(
            setupStatus: { [unowned self] url in
                setupCalls += 1
                let needs = await setupAnswer?() ?? false
                let body = try JSONSerialization.data(withJSONObject: ["needs_setup": needs])
                return (body, HTTPURLResponse(url: url, statusCode: 200, httpVersion: nil, headerFields: nil)!)
            },
            login: { [unowned self] request in
                loginCalls += 1
                return try await answer(request)
            },
            setCookie: { [unowned self] cookie in
                cookiesSet.append(cookie)
                duringSetCookie?()
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
        login answer: @escaping (URLRequest) async throws -> (Data, URLResponse)
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

    /// The production wiring: `environment` left at its default, which under
    /// XCTest really carries the switch, as it does under the signing probe.
    func testTheDefaultEnvironmentCarriesTheSwitch() async {
        let store = FakeCredentialStore(stored: Self.saved)
        let auth = AuthManager(
            baseURL: Self.baseURL,
            credentials: store,
            defaults: defaults,
            transport: transport { _ in throw URLError(.networkConnectionLost) }
        )

        await bounded("onServerBecameAvailable") { await auth.onServerBecameAvailable() }

        XCTAssertEqual(auth.state, .waitingForServer)
        XCTAssertEqual(store.calls, 0)
        XCTAssertEqual(setupCalls + loginCalls, 0)
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

    // MARK: - Manual login failures

    func testAManualLoginThatCannotConnectSaysSoAndLeavesTheStoreAlone() async {
        let store = FakeCredentialStore(stored: nil)
        let auth = makeManager(store: store) { _ in throw URLError(.cannotConnectToHost) }

        await bounded("login") { await auth.login(email: "a@example.test", password: "pw", rememberMe: true) }

        XCTAssertEqual(auth.state, .loginRequired(errorMessage: "Connection failed. Is the server running?"))
        XCTAssertEqual(store.calls, 0, "a failed manual login neither saves nor deletes")
    }

    func testAManualLoginWithAWrongPasswordSaysSoAndLeavesTheStoreAlone() async {
        let store = FakeCredentialStore(stored: Self.saved)
        let auth = makeManager(store: store, login: Self.status(401, body: #"{"detail":"Invalid email or password"}"#))

        await bounded("login") { await auth.login(email: "a@example.test", password: "wrong", rememberMe: true) }

        XCTAssertEqual(auth.state, .loginRequired(errorMessage: "Invalid email or password"))
        XCTAssertEqual(store.calls, 0, "a rejected manual login neither saves the typed password nor deletes the saved one")
        XCTAssertEqual(store.stored?.email, Self.saved.email)
    }

    // MARK: - A superseded auto-login does not decide the state

    /// A first auto-login hangs (the server went down mid-request), the server
    /// comes back, a second one succeeds, and then the first fails. It must
    /// not overwrite `.authenticated`, nor act on the saved login.
    private func assertALateFailureIsIgnored(
        _ lateFailure: @escaping () throws -> (Data, URLResponse),
        file: StaticString = #filePath, line: UInt = #line
    ) async {
        let store = FakeCredentialStore(stored: Self.saved)
        let gate = Gate()
        let auth = makeManager(store: store) { [unowned self] request in
            if loginCalls == 1 {
                await gate.wait()
                return try lateFailure()
            }
            let body = Data(#"{"access_token":"t"}"#.utf8)
            return (body, HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!)
        }

        let first = Task { @MainActor in await auth.onServerBecameAvailable() }
        await bounded("the first login to reach the server") {
            while self.loginCalls < 1 && !Task.isCancelled { await Task.yield() }
        }
        await bounded("the second auto-login") { await auth.onServerBecameAvailable() }
        XCTAssertEqual(auth.state, .authenticated, file: file, line: line)

        await gate.open()
        await bounded("the first login's late failure") { await first.value }

        XCTAssertEqual(loginCalls, 2, file: file, line: line)
        XCTAssertEqual(auth.state, .authenticated, "a superseded attempt's failure must not sign the user out", file: file, line: line)
        XCTAssertEqual(store.deletes, 0, file: file, line: line)
        XCTAssertTrue(cookiesSet.isEmpty, "the success had no Set-Cookie header", file: file, line: line)
    }

    func testALateConnectionFailureAfterANewerSuccessIsIgnored() async {
        await assertALateFailureIsIgnored { throw URLError(.networkConnectionLost) }
    }

    func testALateRejectionAfterANewerSuccessIsIgnored() async {
        await assertALateFailureIsIgnored {
            (Data("{}".utf8), HTTPURLResponse(url: Self.baseURL, statusCode: 401, httpVersion: nil, headerFields: nil)!)
        }
    }

    /// The mirror case: the newer attempt failed, then the older one comes
    /// back with a session. The newer outcome stands, and the stale session's
    /// cookie is not installed.
    func testALateSuccessAfterANewerFailureIsIgnored() async {
        let store = FakeCredentialStore(stored: Self.saved)
        let gate = Gate()
        let auth = makeManager(store: store) { [unowned self] request in
            if loginCalls == 1 {
                await gate.wait()
                let headers = ["Set-Cookie": "refresh_token=stale; Path=/api/auth; HttpOnly"]
                let body = Data(#"{"access_token":"t"}"#.utf8)
                return (body, HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: headers)!)
            }
            throw URLError(.networkConnectionLost)
        }

        let first = Task { @MainActor in await auth.onServerBecameAvailable() }
        await bounded("the first login to reach the server") {
            while self.loginCalls < 1 && !Task.isCancelled { await Task.yield() }
        }
        await bounded("the second auto-login") { await auth.onServerBecameAvailable() }
        let newer = AuthState.loginRequired(errorMessage: "Connection failed. Is the server running?")
        XCTAssertEqual(auth.state, newer)

        await gate.open()
        await bounded("the first login's late success") { await first.value }

        XCTAssertEqual(auth.state, newer)
        XCTAssertTrue(cookiesSet.isEmpty, "a superseded session's cookie is not installed")
    }

    /// Superseded while the session cookie is being written: the request has
    /// answered, but the attempt is no longer the latest by the time it would
    /// say `.authenticated`.
    func testASignInSupersededDuringTheCookieWriteIsNotAuthenticated() async {
        let store = FakeCredentialStore(stored: Self.saved)
        let auth = makeManager(store: store) { request in
            let headers = ["Set-Cookie": "refresh_token=abc; Path=/api/auth; HttpOnly"]
            let body = Data(#"{"access_token":"t"}"#.utf8)
            return (body, HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: headers)!)
        }
        duringSetCookie = { auth.serverBecameUnavailable() }

        await bounded("onServerBecameAvailable") { await auth.onServerBecameAvailable() }

        XCTAssertEqual(cookiesSet.count, 1, "the cookie write happened, and the supersession inside it")
        XCTAssertEqual(auth.state, .waitingForServer)
    }

    /// A manual sign-in in flight when the server goes away, whose request
    /// then fails: the failure is not shown over the waiting view.
    func testAManualLoginSupersededMidRequestLeavesTheStateAlone() async {
        let store = FakeCredentialStore(stored: nil)
        let gate = Gate()
        let auth = makeManager(store: store) { _ in
            await gate.wait()
            throw URLError(.networkConnectionLost)
        }

        let login = Task { @MainActor in await auth.login(email: "a@example.test", password: "pw", rememberMe: true) }
        await bounded("the login to reach the server") {
            while self.loginCalls < 1 && !Task.isCancelled { await Task.yield() }
        }
        auth.serverBecameUnavailable()
        await gate.open()
        await bounded("the late failure") { await login.value }

        XCTAssertEqual(auth.state, .waitingForServer)
        XCTAssertEqual(store.calls, 0)
    }

    /// The setup-status answer arrives after the attempt was superseded. A
    /// late `needs_setup: true` must not switch to the setup page, and a late
    /// `false` must not go on to read the saved login or send it.
    private func assertALateSetupAnswerIsIgnored(needsSetup: Bool, file: StaticString = #filePath, line: UInt = #line) async {
        let store = FakeCredentialStore(stored: Self.saved)
        let gate = Gate()
        setupAnswer = {
            await gate.wait()
            return needsSetup
        }
        let auth = makeManager(store: store, login: Self.status(200, body: #"{"access_token":"t"}"#))

        let first = Task { @MainActor in await auth.onServerBecameAvailable() }
        await bounded("the setup check to reach the server") {
            while self.setupCalls < 1 && !Task.isCancelled { await Task.yield() }
        }
        auth.serverBecameUnavailable()
        await gate.open()
        await bounded("the late setup answer") { await first.value }

        XCTAssertEqual(auth.state, .waitingForServer, file: file, line: line)
        XCTAssertEqual(store.loads, 0, "a superseded attempt does not read the saved login", file: file, line: line)
        XCTAssertEqual(loginCalls, 0, "nor send it", file: file, line: line)
    }

    func testALateNeedsSetupAnswerIsIgnored() async {
        await assertALateSetupAnswerIsIgnored(needsSetup: true)
    }

    func testALateSetupDoneAnswerDoesNotGoOnToSignIn() async {
        await assertALateSetupAnswerIsIgnored(needsSetup: false)
    }

    /// Setup completing in the web view supersedes an auto-login in flight:
    /// its late failure does not replace the login form's state.
    func testSetupCompletionSupersedesASignInInFlight() async {
        let store = FakeCredentialStore(stored: Self.saved)
        let gate = Gate()
        let auth = makeManager(store: store) { _ in
            await gate.wait()
            throw URLError(.networkConnectionLost)
        }

        let first = Task { @MainActor in await auth.onServerBecameAvailable() }
        await bounded("the login to reach the server") {
            while self.loginCalls < 1 && !Task.isCancelled { await Task.yield() }
        }
        auth.handleSetupComplete()
        await gate.open()
        await bounded("the late failure") { await first.value }

        XCTAssertEqual(auth.state, .loginRequired(errorMessage: nil), "not the stale attempt's connection error")
    }

    /// The server going away supersedes a sign-in in flight as well.
    func testALoginThatFinishesAfterTheServerWentAwayIsIgnored() async {
        let store = FakeCredentialStore(stored: Self.saved)
        let gate = Gate()
        let auth = makeManager(store: store) { _ in
            await gate.wait()
            throw URLError(.networkConnectionLost)
        }

        let first = Task { @MainActor in await auth.onServerBecameAvailable() }
        await bounded("the login to reach the server") {
            while self.loginCalls < 1 && !Task.isCancelled { await Task.yield() }
        }
        auth.serverBecameUnavailable()
        await gate.open()
        await bounded("the late failure") { await first.value }

        XCTAssertEqual(auth.state, .waitingForServer)
    }
}

/// The client's first start-nothing guard: under the switch the window holds
/// `InertHostView`, so even a broken AuthManager guard cannot reach the
/// Keychain, the server or the WebKit store from the test host.
@MainActor
final class TestHostRootTests: XCTestCase {

    func testTheSwitchSelectsTheInertRoot() {
        XCTAssertFalse(HarborClerkApp.showsLiveRoot(environment: ["XCTestConfigurationFilePath": "/dev/null"]))
        XCTAssertTrue(HarborClerkApp.showsLiveRoot(environment: [:]))
    }

    /// This very process: its window shows the inert root, and ContentView
    /// never appeared.
    func testThisTestHostShowsTheInertRoot() async {
        let appeared = expectation(description: "InertHostView appeared within 10s")
        let wait = Task { @MainActor in
            while !RootViewLog.inertAppeared && !Task.isCancelled { try? await Task.sleep(for: .milliseconds(50)) }
            appeared.fulfill()
        }
        await fulfillment(of: [appeared], timeout: 10)
        wait.cancel()

        XCTAssertTrue(RootViewLog.inertAppeared)
        XCTAssertFalse(RootViewLog.liveAppeared, "ContentView must never appear in the test host")
    }
}

/// Holds a request until the test opens it.
actor Gate {
    private var isOpen = false
    private var waiters: [CheckedContinuation<Void, Never>] = []

    func wait() async {
        if isOpen { return }
        await withCheckedContinuation { waiters.append($0) }
    }

    func open() {
        isOpen = true
        waiters.forEach { $0.resume() }
        waiters = []
    }
}

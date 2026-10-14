import XCTest
@testable import HarborClerkServer

/// A service whose live-child report is independent of its recorded state,
/// and which counts the calls that reach it. Stands in for LlamaService,
/// whose real `start()` needs a model file and the bundled binary, in the
/// two places #689 lived: the relaunch paths deciding by state instead of
/// process, and `startService` advancing a state a concurrent stop had
/// already moved.
final class MockProcessService: ManagedService {
    let name: String
    var state: ServiceState
    var holdsLiveProcess: Bool
    var isLaunchdManaged = false
    var stopCalls = 0
    var startCalls = 0
    var healthCalls = 0
    /// What `start()` leaves the state at, or nil to leave it alone. Models a
    /// stop that landed while the real `start()` was suspended in an await,
    /// or a start that refused and said why.
    var stateAfterStart: ServiceState?
    /// What `healthCheck()` moves the state to before answering, or nil.
    /// Models a stop that landed during the probe.
    var stateDuringHealthCheck: ServiceState?
    var healthy = false
    /// A standing refusal (#709), as LlamaService reports one for a model
    /// whose weights do not fit this Mac.
    var startRefusal: LaunchRefusal?

    init(name: String = "mock", state: ServiceState, holdsLiveProcess: Bool = false) {
        self.name = name
        self.state = state
        self.holdsLiveProcess = holdsLiveProcess
    }

    func start() async throws {
        startCalls += 1
        if let next = stateAfterStart { state = next }
    }

    func stop() async {
        stopCalls += 1
        holdsLiveProcess = false
        state = .stopped
    }

    func healthCheck() async -> Bool {
        healthCalls += 1
        if let next = stateDuringHealthCheck { state = next }
        return healthy
    }
}

@MainActor
final class ServiceManagerRelaunchTests: XCTestCase {

    private var tempDir: URL!

    override func setUp() {
        super.setUp()
        tempDir = FileManager.default.temporaryDirectory
            .appendingPathComponent("ServiceManagerRelaunchTests-\(UUID().uuidString)")
        try? FileManager.default.createDirectory(at: tempDir, withIntermediateDirectories: true)
    }

    override func tearDown() {
        try? FileManager.default.removeItem(at: tempDir)
        super.tearDown()
    }

    /// A manager whose pid file lands in this test's scratch directory. Reaching
    /// `running` writes it, and the default path is the live app's data folder.
    private func makeManager() -> ServiceManager {
        let sm = ServiceManager()
        sm.pidFileURL = tempDir.appendingPathComponent("child-pids.txt")
        return sm
    }

    // MARK: - stopForRelaunch decides by the process, not the state

    func testErroredServiceWithLiveProcessIsStopped() async {
        let sm = makeManager()
        let svc = MockProcessService(state: .errored, holdsLiveProcess: true)

        let stopped = await sm.stopForRelaunch(svc)

        XCTAssertTrue(stopped)
        XCTAssertEqual(svc.stopCalls, 1, "an errored service with a live child must be stopped, not reset")
        XCTAssertEqual(svc.state, .stopped)
    }

    func testStoppedServiceWithLiveProcessIsStopped() async {
        let sm = makeManager()
        let svc = MockProcessService(state: .stopped, holdsLiveProcess: true)

        let stopped = await sm.stopForRelaunch(svc)

        XCTAssertTrue(stopped)
        XCTAssertEqual(svc.stopCalls, 1, "a child spawned after a stop ran is still a child to stop")
    }

    func testErroredServiceWithoutProcessIsOnlyReset() async {
        let sm = makeManager()
        let svc = MockProcessService(state: .errored, holdsLiveProcess: false)

        let stopped = await sm.stopForRelaunch(svc)

        XCTAssertFalse(stopped)
        XCTAssertEqual(svc.stopCalls, 0)
        XCTAssertEqual(svc.state, .stopped, "errored with nothing alive becomes stopped so the relaunch starts clean")
    }

    func testStoppedServiceWithoutProcessIsLeftAlone() async {
        let sm = makeManager()
        let svc = MockProcessService(state: .stopped, holdsLiveProcess: false)

        let stopped = await sm.stopForRelaunch(svc)

        XCTAssertFalse(stopped)
        XCTAssertEqual(svc.stopCalls, 0)
        XCTAssertEqual(svc.state, .stopped)
    }

    func testRunningServiceIsStopped() async {
        let sm = makeManager()
        let svc = MockProcessService(state: .running, holdsLiveProcess: true)

        let stopped = await sm.stopForRelaunch(svc)

        XCTAssertTrue(stopped)
        XCTAssertEqual(svc.stopCalls, 1)
    }

    /// A start in flight has no child yet; stopping it anyway is what makes
    /// `start()` find the state moved on when it resumes, and not spawn.
    func testStartingServiceIsStoppedEvenBeforeItsSpawn() async {
        let sm = makeManager()
        let svc = MockProcessService(state: .starting, holdsLiveProcess: false)

        let stopped = await sm.stopForRelaunch(svc)

        XCTAssertTrue(stopped)
        XCTAssertEqual(svc.stopCalls, 1)
    }

    /// launchd holds the process for Postgres and Tika, so there is no live
    /// child to consult: a `stopped` agent may still be loaded and holding its
    /// port, and is booted out before the restart as before.
    func testStoppedLaunchdServiceIsStillBootedOut() async {
        let sm = makeManager()
        let svc = MockProcessService(state: .stopped, holdsLiveProcess: false)
        svc.isLaunchdManaged = true

        let stopped = await sm.stopForRelaunch(svc)

        XCTAssertTrue(stopped)
        XCTAssertEqual(svc.stopCalls, 1)
    }

    func testErroredLaunchdServiceIsOnlyReset() async {
        let sm = makeManager()
        let svc = MockProcessService(state: .errored, holdsLiveProcess: false)
        svc.isLaunchdManaged = true

        let stopped = await sm.stopForRelaunch(svc)

        XCTAssertFalse(stopped)
        XCTAssertEqual(svc.stopCalls, 0)
        XCTAssertEqual(svc.state, .stopped)
    }

    // MARK: - a settings restart cancels the config-watcher relaunch only when it relaunches llama

    func testSettingsRestartRelaunchesLlamaOnlyForLlamaKeys() {
        XCTAssertTrue(ServiceManager.settingsRestartRelaunchesLlama(["llm_model_id"]))
        XCTAssertTrue(ServiceManager.settingsRestartRelaunchesLlama(["llm_yarn_enabled", "log_level"]))
        XCTAssertTrue(ServiceManager.settingsRestartRelaunchesLlama(["llama_port"]))
        XCTAssertFalse(ServiceManager.settingsRestartRelaunchesLlama(["worker_preset"]))
        XCTAssertFalse(ServiceManager.settingsRestartRelaunchesLlama(["log_level", "api_port", "reranker_enabled"]))
        XCTAssertFalse(ServiceManager.settingsRestartRelaunchesLlama([]))
    }

    // MARK: - startService only advances a service that is still starting

    func testStartServiceLeavesAServiceStoppedDuringStartAlone() async {
        let sm = makeManager()
        let svc = MockProcessService(state: .stopped)
        svc.stateAfterStart = .stopping
        svc.healthy = true

        await sm.startService(svc)

        XCTAssertEqual(svc.startCalls, 1)
        XCTAssertEqual(svc.state, .stopping, "a stop that landed inside start() owns the state; the health wait must not run")
        XCTAssertEqual(svc.healthCalls, 0)
    }

    func testStartServiceLeavesAServiceStoppedDuringHealthCheckAlone() async {
        let sm = makeManager()
        let svc = MockProcessService(state: .stopped)
        svc.stateDuringHealthCheck = .stopped
        svc.healthy = true

        await sm.startService(svc)

        XCTAssertEqual(svc.healthCalls, 1, "the wait ends at the first probe that finds the state moved on")
        XCTAssertEqual(svc.state, .stopped, "a healthy answer does not make a stopped service running")
    }

    func testStartServiceLeavesAServiceStoppedBetweenProbesAlone() async {
        let sm = makeManager()
        let svc = MockProcessService(state: .stopped)
        // The stop lands during the one-second pause between probes.
        let stopper = Task { @MainActor in
            try? await Task.sleep(for: .milliseconds(300))
            svc.state = .stopped
        }

        await sm.startService(svc)
        await stopper.value

        XCTAssertEqual(svc.healthCalls, 1, "no second probe once the state has moved on")
        XCTAssertEqual(svc.state, .stopped)
    }

    func testStartServiceRunsAHealthyService() async {
        let sm = makeManager()
        let svc = MockProcessService(state: .stopped, holdsLiveProcess: true)
        svc.healthy = true

        await sm.startService(svc)

        XCTAssertEqual(svc.state, .running)
        XCTAssertTrue(FileManager.default.fileExists(atPath: sm.pidFileURL.path), "reaching running records the pid file, in this test's scratch directory")
    }

    /// Start or Restart from the status window while a stop is still in
    /// flight: the spawn would race the stop's tail for `process`.
    func testStartServiceRefusesAServiceStillStopping() async {
        let sm = makeManager()
        let svc = MockProcessService(state: .stopping, holdsLiveProcess: true)

        await sm.startService(svc)

        XCTAssertEqual(svc.startCalls, 0, "no spawn while a stop is in flight")
        XCTAssertEqual(svc.state, .stopping)
    }

    /// A second driver on a service another caller is already starting would
    /// spawn a second child a few seconds after the first.
    func testStartServiceIsANoOpOnAServiceAlreadyStarting() async {
        let sm = makeManager()
        let svc = MockProcessService(state: .starting)
        svc.healthy = true

        await sm.startService(svc)

        XCTAssertEqual(svc.startCalls, 0, "another caller owns this start")
        XCTAssertEqual(svc.healthCalls, 0)
        XCTAssertEqual(svc.state, .starting)
    }

    func testStartServiceHonoursARefusal() async {
        let sm = makeManager()
        let svc = MockProcessService(state: .stopped)
        svc.stateAfterStart = .errored

        await sm.startService(svc)

        XCTAssertEqual(svc.state, .errored)
        XCTAssertEqual(svc.healthCalls, 0, "a start that said why it refused is not waited on")
    }

    // MARK: - a start refusal is said once, and not restarted into (#709)

    private static let doesNotFit = LaunchRefusal(
        modelId: "qwen36-35b-a3b",
        message: "Does not fit this Mac (18 GB): needs about 48 GB. Open the Models page and pick a model that fits."
    )

    /// A manager whose notices land in `notices` instead of Notification Center.
    private func makeNoticingManager() -> (ServiceManager, () -> [(title: String, body: String)]) {
        let sm = makeManager()
        var notices: [(title: String, body: String)] = []
        sm.presentNotice = { title, body in notices.append((title, body)) }
        return (sm, { notices })
    }

    func testStartServiceNoticesARefusalOnceWithTheModelsPagesWords() async {
        let (sm, notices) = makeNoticingManager()
        let svc = MockProcessService(name: "LLM", state: .stopped)
        svc.stateAfterStart = .errored
        svc.startRefusal = Self.doesNotFit

        await sm.startService(svc)

        XCTAssertEqual(svc.state, .errored)
        XCTAssertEqual(notices().count, 1)
        XCTAssertEqual(notices().first?.title, "LLM not started")
        XCTAssertEqual(notices().first?.body, Self.doesNotFit.message)
        XCTAssertEqual(sm.noticedRefusals, ["qwen36-35b-a3b"])

        // Start All, Restart, a config-change relaunch, a health tick: every
        // path reaches startService again, and the same model refuses again.
        await sm.startService(svc)
        await sm.startService(svc)

        XCTAssertEqual(svc.startCalls, 3, "each start still asks; the answer is the same")
        XCTAssertEqual(notices().count, 1, "said once per launch per model, not once per attempt")
    }

    func testADifferentRefusedModelIsNoticedInItsTurn() async {
        let (sm, notices) = makeNoticingManager()
        let svc = MockProcessService(name: "LLM", state: .stopped)
        svc.stateAfterStart = .errored
        svc.startRefusal = Self.doesNotFit
        await sm.startService(svc)

        svc.startRefusal = LaunchRefusal(modelId: "gemma4-26b-a4b", message: "Does not fit this Mac (18 GB): needs about 36 GB. Open the Models page and pick a model that fits.")
        await sm.startService(svc)
        svc.startRefusal = Self.doesNotFit
        await sm.startService(svc)

        XCTAssertEqual(notices().map(\.body).filter { $0.contains("36 GB") }.count, 1)
        XCTAssertEqual(notices().count, 2, "one notice per model this launch; switching back repeats nothing")
    }

    func testAnErrorThatIsNotARefusalIsNotNoticed() async {
        let (sm, notices) = makeNoticingManager()
        let svc = MockProcessService(name: "LLM", state: .stopped)
        svc.stateAfterStart = .errored  // the model file is missing, say

        await sm.startService(svc)

        XCTAssertEqual(svc.state, .errored)
        XCTAssertTrue(notices().isEmpty, "a crash or a missing file keeps the old channel: the log and the red dot")
        XCTAssertTrue(sm.noticedRefusals.isEmpty)
    }

    func testAutoRestartLeavesARefusedModelAlone() async {
        let sm = makeManager()
        let svc = MockProcessService(name: "LLM", state: .errored)
        svc.startRefusal = Self.doesNotFit
        svc.stateAfterStart = .errored

        await sm.attemptAutoRestart(svc)

        XCTAssertEqual(svc.stopCalls, 0, "nothing to stop, and nothing to restart into")
        XCTAssertEqual(svc.startCalls, 0, "a model that cannot fit is not retried; the setting has to change first")
        XCTAssertEqual(svc.state, .errored)
    }

    func testAutoRestartStillRestartsAServiceWithoutARefusal() async {
        let sm = makeManager()
        let svc = MockProcessService(name: "mock", state: .errored)
        svc.stateAfterStart = .errored  // refuses nothing, so the restart runs its course, and the start is reached

        let done = expectation(description: "attemptAutoRestart returned")
        let task = Task { @MainActor in
            await sm.attemptAutoRestart(svc)
            done.fulfill()
        }
        // The restart path sleeps 5 s for the old socket; well inside this bound.
        await fulfillment(of: [done], timeout: 30)
        await task.value

        XCTAssertEqual(svc.startCalls, 1, "the guard is for refusals only")
    }

    /// LlamaService reports its refusal for as long as the settings name the
    /// model it was about; a new model id is a new question.
    func testLlamaRefusalStandsUntilTheSettingNamesAnotherModel() {
        let settings = AppSettings(configURL: tempDir.appendingPathComponent("config.json"))
        settings.llmModelId = "qwen36-35b-a3b"
        let llama = LlamaService()
        llama.settingsOverride = settings
        XCTAssertNil(llama.startRefusal)

        llama.refusal = Self.doesNotFit
        XCTAssertEqual(llama.startRefusal, Self.doesNotFit)

        settings.llmModelId = "qwen35-4b"
        XCTAssertNil(llama.startRefusal, "the user picked another model; the next start decides for it")

        settings.llmModelId = "qwen36-35b-a3b"
        XCTAssertEqual(llama.startRefusal, Self.doesNotFit, "back to the refused one: the answer has not changed")
    }

    /// A refusal is about the model the settings named at that start. A later start that errors for another
    /// reason (the file is gone) or finds no model must not leave "does not fit" standing over it.
    func testAStartThatDoesNotReachTheFitCheckClearsAnOlderRefusal() async throws {
        let settings = AppSettings(configURL: tempDir.appendingPathComponent("config.json"))
        settings.modelsDirOverride = tempDir  // holds no GGUF, so the file guard returns before any port probe
        settings.llamaPort = 1
        settings.llmModelId = "qwen35-4b"
        let llama = LlamaService()
        llama.settingsOverride = settings
        llama.portOverride = 1
        llama.refusal = LaunchRefusal(modelId: "qwen35-4b", message: Self.doesNotFit.message)
        XCTAssertNotNil(llama.startRefusal)

        llama.state = .starting
        try await llama.start()

        XCTAssertEqual(llama.state, .errored, "the file is missing")
        XCTAssertNil(llama.refusal, "that is not a fit refusal, and the old one is not this start's")
        XCTAssertNil(llama.startRefusal)

        llama.refusal = Self.doesNotFit
        settings.llmModelId = ""
        llama.state = .starting
        try await llama.start()

        XCTAssertEqual(llama.state, .stopped, "no model selected")
        XCTAssertNil(llama.refusal)
    }

    // MARK: - the health wait sleeps even on a cancelled Task

    func testSleepEvenIfCancelledStillWaits() async {
        let started = Date()
        let task = Task { await sleepEvenIfCancelled(seconds: 0.3) }
        task.cancel()
        await task.value

        XCTAssertGreaterThanOrEqual(Date().timeIntervalSince(started), 0.25, "a cancelled Task.sleep returns at once and the wait loop spins; this one must not")
    }
}

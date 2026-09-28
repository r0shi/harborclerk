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

    // MARK: - the health wait sleeps even on a cancelled Task

    func testSleepEvenIfCancelledStillWaits() async {
        let started = Date()
        let task = Task { await sleepEvenIfCancelled(seconds: 0.3) }
        task.cancel()
        await task.value

        XCTAssertGreaterThanOrEqual(Date().timeIntervalSince(started), 0.25, "a cancelled Task.sleep returns at once and the wait loop spins; this one must not")
    }
}

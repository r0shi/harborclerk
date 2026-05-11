# PR-1: Shutdown-aware HealthChecker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Track in-flight `attemptAutoRestart` Tasks in `HealthChecker` so `ServiceManager.stopAll()` can cancel them before they race the shutdown. Closes the audit-identified race where a restart Task that passed the `.shutdownPending` gate ends up calling `start()` on a service the orchestrator already moved on from.

**Architecture:** Add a `[UUID: Task<Void, Never>]` dictionary to `HealthChecker`; `checkAll()` inserts on enqueue, the Task itself removes on completion via `defer`. New `cancelInFlightRestarts()` cancels and awaits every entry. `ServiceManager.stopAll()` calls it (plus `configChangeTask?.cancel()`) at the very top — before any state mutation. Same for `restartForChangedSettings()` since that path also stops services.

**Tech Stack:** Swift 5+, Foundation, XCTest. macOS-only target. Tested via `xcodebuild test`.

---

## Context the engineer needs to read first

Read these files in this order before starting:

1. **`/Users/alex/mcp-gateway/docs/superpowers/specs/2026-05-11-menubar-process-mgmt-tier-bc-design.md`** — the approved design. Section "Shutdown-aware HealthChecker" + Section "Detailed design / HealthChecker.cancelInFlightRestarts" are the relevant ones for this PR.

2. **`/Users/alex/mcp-gateway/macos/HarborClerkServer/HarborClerkServer/HealthChecker.swift`** — the file being modified (76 lines). The `checkAll()` method (lines 38-75) is where the auto-restart Task is dispatched today (line 68: `await serviceManager.attemptAutoRestart(service)`). Note that today's code does `await` directly inside `checkAll()` — that's actually NOT what we want once we have cancellation, because we want the in-flight Task to be a separately-trackable handle. Part of this PR is dispatching the auto-restart as a tracked `Task` instead of awaiting inline.

3. **`/Users/alex/mcp-gateway/macos/HarborClerkServer/HarborClerkServer/ServiceManager.swift`** — `stopAll()` is at lines 335-411 (after the PR #341 changes), `restartForChangedSettings()` is at lines ~695-786, `configChangeTask` declaration is at line 97.

4. **`/Users/alex/mcp-gateway/macos/HarborClerkServer/HarborClerkServerTests/HealthCheckerTests.swift`** — existing tests, follow the same style.

5. **`/Users/alex/.claude/projects/-Users-alex-mcp-gateway/memory/project_menubar_process_management_audit.md`** — the audit memo explaining WHY this matters (specifically item 5 under "Lesser issues" describes the race).

## How to run tests

```bash
cd /Users/alex/mcp-gateway/macos/HarborClerkServer
xcodebuild -project HarborClerkServer.xcodeproj -scheme HarborClerkServer -destination 'platform=macOS,arch=arm64' test 2>&1 | tail -20
```

To run only one test class:

```bash
xcodebuild -project HarborClerkServer.xcodeproj -scheme HarborClerkServer -destination 'platform=macOS,arch=arm64' \
    -only-testing:HarborClerkServerTests/HealthCheckerTests test 2>&1 | tail -20
```

Tests are slow (~30 s for the full suite, ~5 s for a single class) due to xcodebuild overhead. The test runner occasionally "hangs before establishing connection" — re-run if that happens.

## File structure

**Modify:**
- `macos/HarborClerkServer/HarborClerkServer/HealthChecker.swift` — add task tracking + `cancelInFlightRestarts`
- `macos/HarborClerkServer/HarborClerkServer/ServiceManager.swift` — `stopAll()` and `restartForChangedSettings()` prepend cancellation calls

**Test:**
- `macos/HarborClerkServer/HarborClerkServerTests/HealthCheckerTests.swift` — extend with new tests

No new files needed. No xcodeproj edits needed.

---

### Task 1: Add task tracking to HealthChecker

**Files:**
- Modify: `macos/HarborClerkServer/HarborClerkServer/HealthChecker.swift:13-20` (add property) and lines 56-69 (modify checkAll's auto-restart dispatch)
- Test: `macos/HarborClerkServer/HarborClerkServerTests/HealthCheckerTests.swift`

- [ ] **Step 1: Write a failing test that verifies a Task handle is stored when checkAll triggers an auto-restart**

Add to `HealthCheckerTests.swift`:

```swift
func testTrackingStoresHandleWhenCheckAllTriggersRestart() async throws {
    let mockServices = MockServiceManager()
    let svc = MockServiceWithFailingHealth(name: "test-svc")
    mockServices.services = [svc]
    let hc = HealthChecker(serviceManager: mockServices)

    // Drive 6 consecutive failed health checks (the threshold) so the
    // auto-restart Task is dispatched. checkAll() is private; we exercise
    // it indirectly by setting startPolling() with a very fast interval
    // OR by exposing a test-only entry point. Test-only entry point is
    // simpler — see Step 3.
    svc.state = .running
    for _ in 0..<6 {
        await hc.tickForTesting()
    }

    XCTAssertEqual(hc.inFlightTaskCount, 1)
}
```

The test references two not-yet-existing things: `hc.tickForTesting()` (internal API for tests) and `hc.inFlightTaskCount` (read-only view of the dict size). These get added in Step 3.

Also assumes `MockServiceManager` and `MockServiceWithFailingHealth` test doubles exist or will exist. Check `HealthCheckerTests.swift` for similar existing mocks — there's almost certainly a `MockServiceManager`. If not, add minimal versions:

```swift
final class MockServiceManager: ServiceManager {
    // Use the production ServiceManager so the protocol matches; only
    // override what we need.
    var attemptAutoRestartCalled: [String] = []
    override func attemptAutoRestart(_ service: any ManagedService) async {
        attemptAutoRestartCalled.append(service.name)
        // Don't actually restart — just record + sleep so the Task is
        // observably in-flight.
        try? await Task.sleep(for: .milliseconds(500))
    }
}

final class MockServiceWithFailingHealth: ManagedService {
    let name: String
    var state: ServiceState = .stopped
    init(name: String) { self.name = name }
    func start() async throws {}
    func stop() async {}
    func healthCheck() async -> Bool { false }
}
```

- [ ] **Step 2: Run the test to confirm it fails**

```bash
xcodebuild -project HarborClerkServer.xcodeproj -scheme HarborClerkServer -destination 'platform=macOS,arch=arm64' \
    -only-testing:HarborClerkServerTests/HealthCheckerTests/testTrackingStoresHandleWhenCheckAllTriggersRestart test 2>&1 | tail -10
```

Expected output: `Cannot find 'tickForTesting' in scope` or similar compile error.

- [ ] **Step 3: Add the property, the test entry point, and modify `checkAll` to dispatch as a tracked Task**

Edit `HealthChecker.swift`:

```swift
@MainActor
final class HealthChecker {
    private let serviceManager: ServiceManager
    private var timer: Timer?
    private let interval: TimeInterval = 10
    var paused = false
    var pausedServices: Set<String> = []

    private var failureCounts: [String: Int] = [:]
    private let consecutiveFailuresBeforeError = 6

    /// In-flight auto-restart Tasks, keyed by UUID. Inserted by checkAll
    /// when it dispatches an auto-restart; removed by the Task itself
    /// via defer when its body returns (whether the restart succeeded,
    /// failed, or was cancelled). cancelInFlightRestarts() walks this
    /// dict to abort everything before stopAll mutates service state.
    private var taskHandles: [UUID: Task<Void, Never>] = [:]

    /// Read-only view of the in-flight Task count. Test-only.
    var inFlightTaskCount: Int { taskHandles.count }

    init(serviceManager: ServiceManager) {
        self.serviceManager = serviceManager
    }

    func startPolling() {
        timer = Timer.scheduledTimer(withTimeInterval: interval, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in
                await self?.checkAll()
            }
        }
    }

    func stopPolling() {
        timer?.invalidate()
        timer = nil
    }

    /// Test-only entry point that runs one health check pass synchronously.
    /// Production code calls checkAll via the Timer above.
    func tickForTesting() async {
        await checkAll()
    }

    private func checkAll() async {
        guard !paused else { return }
        var changed = false
        for service in serviceManager.services {
            guard service.state == .running else {
                failureCounts[service.name] = nil
                continue
            }
            guard !pausedServices.contains(service.name) else { continue }

            let healthy = await service.healthCheck()
            guard service.state == .running else {
                failureCounts[service.name] = nil
                continue
            }
            if healthy {
                failureCounts[service.name] = nil
            } else {
                let count = (failureCounts[service.name] ?? 0) + 1
                failureCounts[service.name] = count
                Log.logger("health").warning(
                    "[\(service.name, privacy: .public)] Health check failed (\(count, privacy: .public)/\(self.consecutiveFailuresBeforeError, privacy: .public))"
                )
                if count >= self.consecutiveFailuresBeforeError {
                    service.state = .errored
                    failureCounts[service.name] = nil
                    changed = true
                    Log.logger("health").error(
                        "[\(service.name, privacy: .public)] \(self.consecutiveFailuresBeforeError, privacy: .public) consecutive failures — marked errored"
                    )
                    // Dispatch the auto-restart as a tracked Task so
                    // cancelInFlightRestarts() can abort it during shutdown.
                    // Previously this was `await serviceManager.attemptAutoRestart(service)`
                    // inline — which was simpler but unstoppable.
                    let id = UUID()
                    let svc = service
                    let sm = serviceManager
                    taskHandles[id] = Task { @MainActor in
                        defer { Task { @MainActor in self.taskHandles[id] = nil } }
                        await sm.attemptAutoRestart(svc)
                    }
                }
            }
        }
        if changed {
            serviceManager.notifyStateChanged()
        }
    }
}
```

- [ ] **Step 4: Run the test to confirm it passes**

```bash
xcodebuild -project HarborClerkServer.xcodeproj -scheme HarborClerkServer -destination 'platform=macOS,arch=arm64' \
    -only-testing:HarborClerkServerTests/HealthCheckerTests/testTrackingStoresHandleWhenCheckAllTriggersRestart test 2>&1 | tail -10
```

Expected: `Test Suite ... passed`.

If you get `inFlightTaskCount == 0`, the Task ran its `defer` and removed itself before you read the count. Adjust the mock's `attemptAutoRestart` to sleep longer (e.g. 2 seconds), or have the test await `Task.yield()` once.

- [ ] **Step 5: Commit**

```bash
git add macos/HarborClerkServer/HarborClerkServer/HealthChecker.swift \
        macos/HarborClerkServer/HarborClerkServerTests/HealthCheckerTests.swift
git commit -m "feat(menubar): track in-flight HealthChecker restart Tasks

Phase 1 of the shutdown-aware HealthChecker refactor: convert the
inline auto-restart await into a tracked Task so stopAll() can cancel
it. No behavior change yet — Task.cancel() isn't called anywhere.
PR-1 step 2 wires the cancellation in.

Spec: docs/superpowers/specs/2026-05-11-menubar-process-mgmt-tier-bc-design.md"
```

---

### Task 2: Add cancelInFlightRestarts and verify it cancels tracked tasks

**Files:**
- Modify: `macos/HarborClerkServer/HarborClerkServer/HealthChecker.swift`
- Test: `macos/HarborClerkServer/HarborClerkServerTests/HealthCheckerTests.swift`

- [ ] **Step 1: Write a failing test that verifies cancelInFlightRestarts cancels every tracked Task and waits for completion**

Add to `HealthCheckerTests.swift`:

```swift
func testCancelInFlightRestartsCancelsAndAwaits() async throws {
    let mockServices = MockServiceManager()
    // Make attemptAutoRestart observably long so we can race cancellation
    mockServices.attemptAutoRestartDelay = .seconds(5)
    let svc = MockServiceWithFailingHealth(name: "test-svc")
    mockServices.services = [svc]
    let hc = HealthChecker(serviceManager: mockServices)

    svc.state = .running
    for _ in 0..<6 {
        await hc.tickForTesting()
    }
    XCTAssertEqual(hc.inFlightTaskCount, 1)

    let start = Date()
    await hc.cancelInFlightRestarts()
    let elapsed = Date().timeIntervalSince(start)

    XCTAssertEqual(hc.inFlightTaskCount, 0, "all in-flight tasks should be removed")
    XCTAssertLessThan(elapsed, 1.0, "cancel should not wait for the 5s sleep — Task.cancel breaks the sleep")
}
```

The test relies on `Task.sleep` being cancellation-aware (it is — `try? await Task.sleep` returns immediately when the Task is cancelled). It also requires the mock's `attemptAutoRestart` to honor cancellation, which `Task.sleep` does automatically.

- [ ] **Step 2: Run the test to confirm it fails**

```bash
xcodebuild -project HarborClerkServer.xcodeproj -scheme HarborClerkServer -destination 'platform=macOS,arch=arm64' \
    -only-testing:HarborClerkServerTests/HealthCheckerTests/testCancelInFlightRestartsCancelsAndAwaits test 2>&1 | tail -10
```

Expected: compile error `Value of type 'HealthChecker' has no member 'cancelInFlightRestarts'`.

- [ ] **Step 3: Add `cancelInFlightRestarts` to HealthChecker**

Append to HealthChecker.swift just before the closing `}`:

```swift
    /// Cancel every in-flight auto-restart Task and wait for them to
    /// finish (cancellation propagates via Task.sleep + the awaits inside
    /// serviceManager.attemptAutoRestart). Called by stopAll() and
    /// restartForChangedSettings() before they begin mutating service
    /// state, so a Task that already passed its .shutdownPending gate
    /// can't end up calling start() on a service the orchestrator
    /// already moved on from.
    func cancelInFlightRestarts() async {
        let snapshot = taskHandles
        for (_, task) in snapshot { task.cancel() }
        for (_, task) in snapshot { _ = await task.value }
        taskHandles.removeAll()
    }
```

Note: we take a snapshot BEFORE iterating because the deferred removal inside each Task will mutate `taskHandles` from another @MainActor hop. The snapshot lets us safely iterate while removals happen concurrently. The explicit `taskHandles.removeAll()` at the end is defensive — any handles whose `defer` didn't fire (shouldn't happen, but) get cleared.

- [ ] **Step 4: Run the test to confirm it passes**

```bash
xcodebuild -project HarborClerkServer.xcodeproj -scheme HarborClerkServer -destination 'platform=macOS,arch=arm64' \
    -only-testing:HarborClerkServerTests/HealthCheckerTests/testCancelInFlightRestartsCancelsAndAwaits test 2>&1 | tail -10
```

Expected: `Test Suite ... passed`.

- [ ] **Step 5: Commit**

```bash
git add macos/HarborClerkServer/HarborClerkServer/HealthChecker.swift \
        macos/HarborClerkServer/HarborClerkServerTests/HealthCheckerTests.swift
git commit -m "feat(menubar): HealthChecker.cancelInFlightRestarts()

Phase 2 of the shutdown-aware HealthChecker refactor: the method that
stopAll() will call. No callers yet — wired in by the next task.

Spec: docs/superpowers/specs/2026-05-11-menubar-process-mgmt-tier-bc-design.md"
```

---

### Task 3: Wire ServiceManager.stopAll() and restartForChangedSettings() to cancel before mutating

**Files:**
- Modify: `macos/HarborClerkServer/HarborClerkServer/ServiceManager.swift` — `stopAll()` and `restartForChangedSettings()`

- [ ] **Step 1: Inspect the current stopAll and restartForChangedSettings to find the right insertion points**

In `ServiceManager.swift`:
- `stopAll()` starts at line ~335. The first executable line is `stopConfigWatcher()`. We want to prepend cancellation BEFORE that.
- `restartForChangedSettings()` starts at line ~698. Find the first place it stops services (search for `worker.process?.terminate()` or `await svc.stop()`).

- [ ] **Step 2: Write a smoke test that exercises the integration**

Add to `HealthCheckerTests.swift` (NOT a ServiceManager test file — this verifies the contract from HealthChecker's side):

```swift
func testStopAllCancelsInFlightRestarts() async throws {
    // We can't easily run the full ServiceManager.stopAll() in a unit
    // test (it manages real subprocesses). Instead, we verify the
    // contract: HealthChecker has a public method cancelInFlightRestarts
    // that ServiceManager.stopAll calls. The grep guard catches
    // regressions where the call gets removed.
    let url = URL(fileURLWithPath: "/Users/alex/mcp-gateway/macos/HarborClerkServer/HarborClerkServer/ServiceManager.swift")
    let source = try String(contentsOf: url, encoding: .utf8)
    XCTAssertTrue(
        source.contains("await healthChecker?.cancelInFlightRestarts()"),
        "ServiceManager.swift must call healthChecker.cancelInFlightRestarts() (likely in stopAll or restartForChangedSettings)"
    )
}
```

Yes, this is a source-text assertion. The real test is the manual smoke test the user will run after merge. Source-text guards are a reasonable proxy for "this wiring exists" when integration testing is too heavy.

- [ ] **Step 3: Run the test to confirm it fails**

```bash
xcodebuild -project HarborClerkServer.xcodeproj -scheme HarborClerkServer -destination 'platform=macOS,arch=arm64' \
    -only-testing:HarborClerkServerTests/HealthCheckerTests/testStopAllCancelsInFlightRestarts test 2>&1 | tail -10
```

Expected: assertion failure `ServiceManager.swift must call healthChecker.cancelInFlightRestarts()`.

- [ ] **Step 4: Modify ServiceManager.stopAll() to prepend the cancellation**

In `ServiceManager.swift`, locate:

```swift
    func stopAll() async {
        stopConfigWatcher()
```

Change to:

```swift
    func stopAll() async {
        // Cancel in-flight auto-restart Tasks AND the config-watcher
        // restart task BEFORE mutating service state. Without this,
        // a HealthChecker-dispatched restart that passed its
        // .shutdownPending gate could call start() on a service we're
        // about to tear down, leaving an orphaned child the menubar's
        // Process ref doesn't track. See
        // project_menubar_process_management_audit.md item 5.
        configChangeTask?.cancel()
        configChangeTask = nil
        await healthChecker?.cancelInFlightRestarts()

        stopConfigWatcher()
```

- [ ] **Step 5: Modify ServiceManager.restartForChangedSettings() with the same prefix**

Locate the start of `restartForChangedSettings()` (around line 698). Prepend:

```swift
    func restartForChangedSettings(_ changedKeys: Set<String>) async {
        // Same rationale as stopAll: cancel in-flight restart Tasks
        // first so they don't race the targeted teardown.
        configChangeTask?.cancel()
        configChangeTask = nil
        await healthChecker?.cancelInFlightRestarts()

        // ... existing body unchanged
```

- [ ] **Step 6: Run the test to confirm it passes**

```bash
xcodebuild -project HarborClerkServer.xcodeproj -scheme HarborClerkServer -destination 'platform=macOS,arch=arm64' \
    -only-testing:HarborClerkServerTests/HealthCheckerTests/testStopAllCancelsInFlightRestarts test 2>&1 | tail -10
```

Expected: `Test Suite ... passed`.

- [ ] **Step 7: Run the full test suite to confirm no regression**

```bash
xcodebuild -project HarborClerkServer.xcodeproj -scheme HarborClerkServer -destination 'platform=macOS,arch=arm64' test 2>&1 | tail -15
```

Expected: All 65+ tests pass (63 previously + 3 new from this PR). If anything else fails, investigate before continuing.

- [ ] **Step 8: Commit**

```bash
git add macos/HarborClerkServer/HarborClerkServer/ServiceManager.swift \
        macos/HarborClerkServer/HarborClerkServerTests/HealthCheckerTests.swift
git commit -m "feat(menubar): stopAll and restartForChangedSettings cancel in-flight restarts

Wires HealthChecker.cancelInFlightRestarts() and configChangeTask.cancel()
into both teardown paths. Audit-identified race (item 5) now closed: a
restart Task that passed its .shutdownPending gate cannot reach
start() on a service the orchestrator already moved on from.

Spec: docs/superpowers/specs/2026-05-11-menubar-process-mgmt-tier-bc-design.md"
```

---

### Task 4: Fresh-eyes review and PR

- [ ] **Step 1: Verify the build is clean and all tests pass**

```bash
cd /Users/alex/mcp-gateway/macos/HarborClerkServer
xcodebuild -project HarborClerkServer.xcodeproj -scheme HarborClerkServer -destination 'platform=macOS,arch=arm64' build 2>&1 | tail -3
xcodebuild -project HarborClerkServer.xcodeproj -scheme HarborClerkServer -destination 'platform=macOS,arch=arm64' test 2>&1 | tail -5
```

Both should end with `BUILD SUCCEEDED` and `TEST SUCCEEDED`.

- [ ] **Step 2: Push and dispatch fresh-eyes review**

```bash
cd /Users/alex/mcp-gateway
git push -u origin fix/menubar-shutdown-aware-healthchecker
```

Then dispatch a minimal-prompt fresh-eyes review per the standing directive in `MEMORY.md`:

```
feature-dev:code-reviewer agent prompt:
"Review the changes on branch fix/menubar-shutdown-aware-healthchecker
(tip <SHA>) against origin/main. Repo at /Users/alex/mcp-gateway.
Report findings with confidence scores. Address ≥80 confidence before merge."
```

Address any ≥80 findings, push fixes, re-review until clean.

- [ ] **Step 3: Open PR**

```bash
gh pr create --title "feat(menubar): shutdown-aware HealthChecker (PR-1 of 4)" \
  --body-file <tmp>
```

PR body should reference:
- The spec doc: `docs/superpowers/specs/2026-05-11-menubar-process-mgmt-tier-bc-design.md`
- The audit memo: `project_menubar_process_management_audit.md`
- That this is the first of four sub-PRs for the Tier B+C refactor.

- [ ] **Step 4: Watch CI and merge**

Container-scan typically takes ~10 minutes. Once all 6 checks green:

```bash
gh pr merge <N> --squash --delete-branch
```

---

## Self-review checklist (done by plan author)

- **Spec coverage:** Spec section "Shutdown-aware HealthChecker" → covered by Tasks 1-3. ✓
- **Type consistency:** `taskHandles: [UUID: Task<Void, Never>]` used consistently. `inFlightTaskCount` is read-only computed. ✓
- **No placeholders:** All code blocks are complete; commands have expected output stated. ✓

## Out of scope (deferred to later PRs in the four-PR series)

- Process groups (PR-2)
- Force Stop All menu (PR-3)
- launchd migration for Postgres + Tika (PR-4)

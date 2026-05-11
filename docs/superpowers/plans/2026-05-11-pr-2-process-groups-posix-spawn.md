# PR-2: Process Groups via setpgid Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every menubar-spawned child becomes the leader of its own process group, so `killpg()` reaches grandchildren on force-kill. Catches Tika's child JVMs, worker-spawned `tesseract`/`pdftoppm`/`magick`, and any other indirect descendants that today survive `kill(pid, SIGKILL)`.

**Architecture:** Add a thin `Process.runAsProcessGroupLeader()` extension that calls `try self.run()` then immediately `setpgid(self.processIdentifier, 0)` from the parent. Migrate every `proc.run()` call site in the menubar's service code to use the new method. Update `forceKillEverything()` and `Process.waitForExitWithDeadline()`'s SIGKILL escalation to use `killpg(pid, SIGKILL)` instead of `kill(pid, SIGKILL)`. Accept a microsecond-scale race window (between fork and setpgid) — none of our services fork immediately on startup, so the race is purely theoretical.

**Tech Stack:** Swift 5+, Foundation, Darwin (for `setpgid`/`killpg`/`errno`), XCTest. macOS-only.

---

## Context the engineer needs to read first

1. **`/Users/alex/mcp-gateway/docs/superpowers/specs/2026-05-11-menubar-process-mgmt-tier-bc-design.md`** — Sections "Process groups for the remaining children" + "Detailed design / Process.runAsProcessGroupLeader". Note: the spec considered a separate `SpawnedProcess` class as a fallback if the extension proves fragile. This plan commits to the extension approach (call `setpgid` from the parent after `run()`); the fallback is documented but not implemented unless something blows up.

2. **`/Users/alex/mcp-gateway/macos/HarborClerkServer/HarborClerkServer/ProcessExtensions.swift`** — existing file from PR #341 with `detachPipesForShutdown()` and `waitForExitWithDeadline()`. We extend it here, not creating a new file.

3. **`/Users/alex/mcp-gateway/macos/HarborClerkServer/HarborClerkServer/ServiceManager.swift`** — `forceKillEverything()` is the function we update to use `killpg`.

4. **All service files that call `proc.run()` (or `try? proc.run()`) for a long-running subprocess:**
   - `Services/PythonService.swift` — line ~68 (`try proc.run()`)
   - `Services/LlamaService.swift` — line ~79 (`try proc.run()`)
   - `Services/TikaService.swift` — line ~52 (`try proc.run()`)
   - `Services/PostgresService.swift` — calls `pg_ctl` as a short-lived helper, NOT a long-running service; SKIP migration here (the postmaster spawned by pg_ctl isn't tracked by our `Process` ref anyway). The launchd migration in PR-4 will reshape this entirely.

5. **`/Users/alex/mcp-gateway/macos/HarborClerkServer/HarborClerkServer/ServiceManager.swift:235-264`** — `killStaleProcess(onPort:)` runs `lsof` then `kill(pid, SIGTERM)`. We could update this to `killpg` too, but lsof returns the leaf PID and we don't know which pgid it belongs to. Leave alone for this PR.

6. **macOS `man 2 setpgid`, `man 2 killpg`** — set up the mental model.

## How to run tests

Same as PR-1:

```bash
cd /Users/alex/mcp-gateway/macos/HarborClerkServer
xcodebuild -project HarborClerkServer.xcodeproj -scheme HarborClerkServer -destination 'platform=macOS,arch=arm64' test 2>&1 | tail -15

# Single test class:
xcodebuild ... -only-testing:HarborClerkServerTests/ProcessExtensionsTests test
```

## File structure

**Modify:**
- `macos/HarborClerkServer/HarborClerkServer/ProcessExtensions.swift` — add `runAsProcessGroupLeader`; update SIGKILL escalation inside `waitForExitWithDeadline` to use `killpg`
- `macos/HarborClerkServer/HarborClerkServer/ServiceManager.swift` — update `forceKillEverything` to use `killpg`
- `macos/HarborClerkServer/HarborClerkServer/Services/PythonService.swift` — `start()` switches to `runAsProcessGroupLeader`
- `macos/HarborClerkServer/HarborClerkServer/Services/LlamaService.swift` — same
- `macos/HarborClerkServer/HarborClerkServer/Services/TikaService.swift` — same

**Test:**
- `macos/HarborClerkServer/HarborClerkServerTests/ProcessExtensionsTests.swift` — extend

No new files. No xcodeproj edits.

---

### Task 1: Add Process.runAsProcessGroupLeader() extension

**Files:**
- Modify: `macos/HarborClerkServer/HarborClerkServer/ProcessExtensions.swift`
- Test: `macos/HarborClerkServer/HarborClerkServerTests/ProcessExtensionsTests.swift`

- [ ] **Step 1: Write a failing test that asserts after `runAsProcessGroupLeader()`, the child's process group ID equals its PID**

Add to `ProcessExtensionsTests.swift`:

```swift
/// After runAsProcessGroupLeader, getpgid(pid) must equal pid — that
/// confirms the child is the leader of a new process group rather than
/// inheriting the parent's pgid. Without this, killpg(pid) only hits
/// the leaf process and grandchildren orphan to launchd.
func testRunAsProcessGroupLeaderMakesChildItsOwnPgidLeader() throws {
    let proc = Process()
    proc.executableURL = URL(fileURLWithPath: "/bin/sleep")
    proc.arguments = ["3"]

    try proc.runAsProcessGroupLeader()
    defer {
        if proc.isRunning {
            kill(proc.processIdentifier, SIGKILL)
            proc.waitUntilExit()
        }
    }

    let pid = proc.processIdentifier
    let pgid = getpgid(pid)
    XCTAssertEqual(pgid, pid, "expected child to be its own pgid leader (pgid == pid)")
}
```

- [ ] **Step 2: Run the test to confirm it fails**

```bash
xcodebuild ... -only-testing:HarborClerkServerTests/ProcessExtensionsTests/testRunAsProcessGroupLeaderMakesChildItsOwnPgidLeader test 2>&1 | tail -10
```

Expected: compile error `Value of type 'Process' has no member 'runAsProcessGroupLeader'`.

- [ ] **Step 3: Add the extension method**

Append to `ProcessExtensions.swift`:

```swift
extension Process {
    /// Launch this Process and immediately make the child its own
    /// process-group leader, so `killpg(child.pid, signal)` reaches
    /// any grandchildren the child later spawns.
    ///
    /// Required for services whose children spawn further children:
    /// Tika's JVM forks child JVMs for heavy extractions, Python
    /// workers spawn `pdftoppm`, `tesseract`, `magick`, etc. Without
    /// this, `kill(pid, SIGKILL)` only hits the leaf process; the
    /// grandchildren get reparented to launchd and leak across menubar
    /// restarts. See `project_menubar_process_management_audit.md`
    /// item 4.
    ///
    /// Implementation: `Process.run()` uses `posix_spawn` internally
    /// but doesn't expose `POSIX_SPAWN_SETPGROUP`. We instead call
    /// `setpgid()` on the child from the parent immediately after
    /// `run()` returns. There's a theoretical race — if the child
    /// fork-exec's another binary before our setpgid call lands, that
    /// grandchild inherits the OLD pgid. In practice the window is
    /// microseconds and none of our managed services (Python, llama,
    /// Java/Tika) fork in their startup path before initialising; the
    /// race has never been observed.
    ///
    /// On macOS, `setpgid(pid, 0)` says "make this pid a new pgid
    /// leader, with its pid as the pgid value". The race-with-already-
    /// exec'd-child error EACCES is logged but not raised — by the
    /// time we see it, the child is already running with its inherited
    /// pgid, which is no worse than what we had before this method
    /// existed.
    func runAsProcessGroupLeader() throws {
        try self.run()
        let pid = self.processIdentifier
        if setpgid(pid, 0) != 0 {
            let err = errno
            Log.logger("lifecycle").warning(
                "setpgid(\(pid, privacy: .public), 0) failed: errno=\(err, privacy: .public) — grandchildren may leak on force-kill of this pid"
            )
        }
    }
}
```

- [ ] **Step 4: Run the test to confirm it passes**

```bash
xcodebuild ... -only-testing:HarborClerkServerTests/ProcessExtensionsTests/testRunAsProcessGroupLeaderMakesChildItsOwnPgidLeader test 2>&1 | tail -10
```

Expected: `Test Suite ... passed`. The full file test count goes from 6 (after PR #341) to 7.

- [ ] **Step 5: Commit**

```bash
git add macos/HarborClerkServer/HarborClerkServer/ProcessExtensions.swift \
        macos/HarborClerkServer/HarborClerkServerTests/ProcessExtensionsTests.swift
git commit -m "feat(menubar): add Process.runAsProcessGroupLeader

setpgid(child, 0) from the parent immediately after run(). Closes the
process-group gap audit item 4: kill(pid) only hits the leaf process,
killpg(pid) reaches grandchildren once pid == pgid.

Spec: docs/superpowers/specs/2026-05-11-menubar-process-mgmt-tier-bc-design.md"
```

---

### Task 2: Verify killpg() reaches grandchildren (the actual user-visible behaviour)

**Files:**
- Test: `macos/HarborClerkServer/HarborClerkServerTests/ProcessExtensionsTests.swift`

- [ ] **Step 1: Write a failing test that spawns a `sh -c 'sleep 30 & wait'` (parent + grandchild), runs as pgid leader, killpg's it, and verifies BOTH parent AND grandchild die**

Add to `ProcessExtensionsTests.swift`:

```swift
/// The user-visible reason for runAsProcessGroupLeader: killpg reaches
/// grandchildren that plain kill(pid) would miss. Verified end-to-end
/// by spawning `sh -c 'sleep 30 & wait'`. The sh forks a sleep child;
/// without pgid we'd only kill sh and orphan sleep. With pgid we kill
/// both.
func testKillpgReachesGrandchildSpawnedViaShell() async throws {
    let proc = Process()
    proc.executableURL = URL(fileURLWithPath: "/bin/sh")
    // Background-spawn a sleep so it's a separate process from sh, then
    // `wait` so sh stays alive until the sleep dies.
    proc.arguments = ["-c", "sleep 30 & echo $! > /tmp/pg-test-grandchild.pid; wait"]
    try proc.runAsProcessGroupLeader()
    defer {
        if proc.isRunning {
            kill(proc.processIdentifier, SIGKILL)
            proc.waitUntilExit()
        }
        try? FileManager.default.removeItem(atPath: "/tmp/pg-test-grandchild.pid")
    }

    // Give sh a moment to write the grandchild PID
    try await Task.sleep(for: .milliseconds(200))

    guard let pidStr = try? String(contentsOfFile: "/tmp/pg-test-grandchild.pid"),
          let grandchildPid = Int32(pidStr.trimmingCharacters(in: .whitespacesAndNewlines)) else {
        XCTFail("grandchild pid file missing or unparseable")
        return
    }
    XCTAssertEqual(kill(grandchildPid, 0), 0, "grandchild must be alive before killpg")

    // killpg the leader. Should hit sh AND the sleep.
    XCTAssertEqual(killpg(proc.processIdentifier, SIGKILL), 0)

    // Give the kernel a moment to clean up
    try await Task.sleep(for: .milliseconds(500))

    XCTAssertEqual(kill(grandchildPid, 0), -1, "grandchild must be dead after killpg")
    XCTAssertEqual(errno, ESRCH, "kill(pid, 0) should have failed with ESRCH (no such process)")
}
```

- [ ] **Step 2: Run the test to confirm it passes (yes, this test depends ONLY on existing extension method)**

```bash
xcodebuild ... -only-testing:HarborClerkServerTests/ProcessExtensionsTests/testKillpgReachesGrandchildSpawnedViaShell test 2>&1 | tail -10
```

If it passes immediately, that confirms the Task 1 implementation works for the actual desired behaviour. If it fails, the most likely cause is the `setpgid` race losing — investigate whether `/bin/sh` is forking before our setpgid lands. Mitigation: use `posix_spawn` with `POSIX_SPAWN_SETPGROUP` directly (the SpawnedProcess fallback documented in the spec). This is the canary that tells us whether the simple extension approach holds up.

- [ ] **Step 3: Commit (test addition only — no impl change)**

```bash
git add macos/HarborClerkServer/HarborClerkServerTests/ProcessExtensionsTests.swift
git commit -m "test(menubar): killpg reaches grandchildren via runAsProcessGroupLeader

End-to-end test of the user-visible behaviour the audit was after.
sh -c 'sleep 30 & wait' creates parent-and-grandchild; killpg kills
both. If this ever regresses, the simple parent-side setpgid approach
has lost its race and we should consider the SpawnedProcess fallback
from the design spec."
```

---

### Task 3: Update Process.waitForExitWithDeadline SIGKILL escalation to use killpg

**Files:**
- Modify: `macos/HarborClerkServer/HarborClerkServer/ProcessExtensions.swift` — `waitForExitWithDeadline` already exists from PR #341; we change one line.

- [ ] **Step 1: Write a failing test**

The existing `testWaitForExitWithDeadlineEscalatesToSigkill` (from PR #341) verifies SIGKILL fires. We add a new test that uses a parent+grandchild and verifies the grandchild dies too:

```swift
/// waitForExitWithDeadline's SIGKILL escalation must reach grandchildren
/// when the leader was launched as a pgid leader. Otherwise a hung
/// service that spawned subprocesses (Tika child JVMs, worker-spawned
/// pdftoppm) leaks them through the SIGKILL fallback.
func testWaitForExitWithDeadlineSigkillReachesGrandchildren() async throws {
    let proc = Process()
    proc.executableURL = URL(fileURLWithPath: "/bin/sh")
    // Trap SIGTERM so it doesn't exit on the polite signal. Spawn
    // a grandchild and wait. With trap, only SIGKILL kills sh.
    proc.arguments = ["-c", "trap '' TERM; sleep 30 & echo $! > /tmp/pg-test-grandchild2.pid; wait"]
    try proc.runAsProcessGroupLeader()

    defer { try? FileManager.default.removeItem(atPath: "/tmp/pg-test-grandchild2.pid") }

    try await Task.sleep(for: .milliseconds(200))
    let grandchildPid = Int32((try String(contentsOfFile: "/tmp/pg-test-grandchild2.pid"))
        .trimmingCharacters(in: .whitespacesAndNewlines))!

    proc.terminate()  // SIGTERM, which sh has trapped to no-op
    await proc.waitForExitWithDeadline(graceSeconds: 1.0, serviceName: "pg-test-grandchild2")
    XCTAssertFalse(proc.isRunning)

    try await Task.sleep(for: .milliseconds(500))
    XCTAssertEqual(kill(grandchildPid, 0), -1, "grandchild must be dead after deadline-driven SIGKILL")
}
```

- [ ] **Step 2: Run to confirm it fails (because waitForExitWithDeadline still uses kill, not killpg)**

```bash
xcodebuild ... -only-testing:HarborClerkServerTests/ProcessExtensionsTests/testWaitForExitWithDeadlineSigkillReachesGrandchildren test 2>&1 | tail -10
```

Expected: assertion failure on the grandchild — its PID is still alive because we SIGKILLed the leaf, not the pgid.

- [ ] **Step 3: Modify `waitForExitWithDeadline` to use `killpg`**

In `ProcessExtensions.swift`, locate the SIGKILL escalation inside `waitForExitWithDeadline`:

```swift
DispatchQueue.global().asyncAfter(deadline: .now() + grace) {
    guard proc.isRunning else { return }
    Log.logger("lifecycle").warning(
        "[\(name, privacy: .public)] Still running after \(Int(grace), privacy: .public)s — sending SIGKILL"
    )
    kill(proc.processIdentifier, SIGKILL)
}
```

Change the last line to:

```swift
    // killpg targets the entire process group (which the leader's pid
    // identifies after runAsProcessGroupLeader). Catches grandchildren
    // — Tika's child JVMs, worker-spawned tesseract/pdftoppm/magick.
    // Falls back gracefully if the process wasn't launched as a pgid
    // leader: killpg with a non-leader pid returns -1 ESRCH and we
    // log it. For belt-and-suspenders we also send a plain kill().
    if killpg(proc.processIdentifier, SIGKILL) != 0 {
        // Not a pgid leader (or already dead) — fall back to kill().
        kill(proc.processIdentifier, SIGKILL)
    }
```

- [ ] **Step 4: Run the test to confirm it passes**

```bash
xcodebuild ... -only-testing:HarborClerkServerTests/ProcessExtensionsTests/testWaitForExitWithDeadlineSigkillReachesGrandchildren test 2>&1 | tail -10
```

Expected: pass.

- [ ] **Step 5: Run the existing waitForExit tests to confirm no regression**

```bash
xcodebuild ... -only-testing:HarborClerkServerTests/ProcessExtensionsTests test 2>&1 | tail -10
```

All 8 (or so) tests in ProcessExtensionsTests should pass.

- [ ] **Step 6: Commit**

```bash
git add macos/HarborClerkServer/HarborClerkServer/ProcessExtensions.swift \
        macos/HarborClerkServer/HarborClerkServerTests/ProcessExtensionsTests.swift
git commit -m "feat(menubar): waitForExitWithDeadline SIGKILL escalates via killpg

Process group form of SIGKILL catches grandchildren that the plain
kill() form would orphan. Belt-and-suspenders fallback to kill() if
killpg fails (process wasn't launched as a pgid leader).

Spec: docs/superpowers/specs/2026-05-11-menubar-process-mgmt-tier-bc-design.md"
```

---

### Task 4: Update ServiceManager.forceKillEverything to use killpg

**Files:**
- Modify: `macos/HarborClerkServer/HarborClerkServer/ServiceManager.swift` — `forceKillEverything()`

- [ ] **Step 1: Inspect the current forceKillEverything**

The function from PR #341 walks three PID sources and calls `kill(pid, SIGKILL)` on each. We change every direct kill call to the killpg-with-fallback pattern.

- [ ] **Step 2: Add a comment-based test (the integration is hard to unit-test; source-guard is acceptable here)**

Add to a relevant test file (e.g. `ServiceConfigTests.swift` — pick the most thematically appropriate):

```swift
/// forceKillEverything must use killpg() so grandchildren of tracked
/// PIDs (Tika child JVMs, worker-spawned utilities) are reached.
/// Source-text guard against accidental regression.
func testForceKillEverythingUsesKillpg() throws {
    let url = URL(fileURLWithPath: "/Users/alex/mcp-gateway/macos/HarborClerkServer/HarborClerkServer/ServiceManager.swift")
    let source = try String(contentsOf: url, encoding: .utf8)
    let forceKillRange = source.range(of: "func forceKillEverything")!
    let nextFuncRange = source.range(of: "func ", range: forceKillRange.upperBound..<source.endIndex)
    let body = String(source[forceKillRange.upperBound..<(nextFuncRange?.lowerBound ?? source.endIndex)])
    XCTAssertTrue(body.contains("killpg("), "forceKillEverything must call killpg to reach grandchildren")
}
```

- [ ] **Step 3: Run to confirm it fails**

```bash
xcodebuild ... -only-testing:HarborClerkServerTests/ServiceConfigTests/testForceKillEverythingUsesKillpg test 2>&1 | tail -10
```

Expected: assertion failure (current code uses plain `kill`).

- [ ] **Step 4: Modify forceKillEverything in ServiceManager.swift**

Find every `kill(pid, SIGKILL)` inside `forceKillEverything` and replace with:

```swift
// killpg reaches grandchildren when the tracked PID is a pgid leader
// (which it is, post-PR-2). Fall back to plain kill() for belt-and-
// suspenders.
if killpg(pid, SIGKILL) != 0 { kill(pid, SIGKILL) }
```

There are three such sites:
1. Inside the first loop iterating `services` — both branches (PythonService + TikaService/LlamaService).
2. Inside the postmaster.pid read branch.
3. Inside the child-pids.txt read branch.

All three become the same pattern. Note: source 2 (postmaster) is a launchd-managed process after PR-4 — it will probably not be in the menubar's pgid at all. The killpg-then-fall-back-to-kill pattern handles both cases.

- [ ] **Step 5: Run the test to confirm it passes**

```bash
xcodebuild ... -only-testing:HarborClerkServerTests/ServiceConfigTests/testForceKillEverythingUsesKillpg test 2>&1 | tail -10
```

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add macos/HarborClerkServer/HarborClerkServer/ServiceManager.swift \
        macos/HarborClerkServer/HarborClerkServerTests/ServiceConfigTests.swift
git commit -m "feat(menubar): forceKillEverything uses killpg for grandchild reach

Three call sites (live service refs, postmaster.pid, child-pids.txt)
all switch to killpg-with-fallback-to-kill. Together with PR-2's
runAsProcessGroupLeader, this means a force-kill of any service tree
catches its descendants instead of orphaning them to launchd.

Spec: docs/superpowers/specs/2026-05-11-menubar-process-mgmt-tier-bc-design.md"
```

---

### Task 5: Migrate every service's `proc.run()` to `runAsProcessGroupLeader()`

**Files:**
- Modify: `macos/HarborClerkServer/HarborClerkServer/Services/PythonService.swift`
- Modify: `macos/HarborClerkServer/HarborClerkServer/Services/LlamaService.swift`
- Modify: `macos/HarborClerkServer/HarborClerkServer/Services/TikaService.swift`

Note: `PostgresService` calls `pg_ctl` as a short-lived helper that exits after launching `postgres` — the postmaster isn't tracked by our Process ref. We do NOT change PostgresService here; PR-4 reshapes Postgres into a launchd agent anyway.

- [ ] **Step 1: Modify PythonService.swift**

In `start()`, find `try proc.run()` (near line 68) and change to:

```swift
try proc.runAsProcessGroupLeader()
```

That's the only change in this file.

- [ ] **Step 2: Modify LlamaService.swift**

Same change at line ~79.

- [ ] **Step 3: Modify TikaService.swift**

Same change at line ~52. **Tika gets this even though it'll move to launchd in PR-4** — protects the brief window between merges (and gives us a tested fallback if PR-4 ever needs to be reverted).

- [ ] **Step 4: Build and run the full test suite**

```bash
cd /Users/alex/mcp-gateway/macos/HarborClerkServer
xcodebuild -project HarborClerkServer.xcodeproj -scheme HarborClerkServer -destination 'platform=macOS,arch=arm64' build 2>&1 | tail -3
xcodebuild -project HarborClerkServer.xcodeproj -scheme HarborClerkServer -destination 'platform=macOS,arch=arm64' test 2>&1 | tail -5
```

Expected: `BUILD SUCCEEDED` and `TEST SUCCEEDED`. Test count: 67 (was 63 after PR #341 + 1 per Task 1/2/3/4 above = 67).

- [ ] **Step 5: Commit**

```bash
git add macos/HarborClerkServer/HarborClerkServer/Services/PythonService.swift \
        macos/HarborClerkServer/HarborClerkServer/Services/LlamaService.swift \
        macos/HarborClerkServer/HarborClerkServer/Services/TikaService.swift
git commit -m "feat(menubar): every long-running child is a pgid leader

PythonService (API + Embedder + Workers + Watcher), LlamaService,
TikaService now spawn via runAsProcessGroupLeader. PostgresService
skipped (pg_ctl is short-lived; PR-4 reshapes that path entirely).

Combined with the killpg updates in waitForExitWithDeadline and
forceKillEverything, this closes the grandchild-leak hole the audit
identified.

Spec: docs/superpowers/specs/2026-05-11-menubar-process-mgmt-tier-bc-design.md"
```

---

### Task 6: Fresh-eyes review and PR

- [ ] **Step 1: Verify build + tests**

```bash
cd /Users/alex/mcp-gateway/macos/HarborClerkServer
xcodebuild -project HarborClerkServer.xcodeproj -scheme HarborClerkServer -destination 'platform=macOS,arch=arm64' build 2>&1 | tail -3
xcodebuild -project HarborClerkServer.xcodeproj -scheme HarborClerkServer -destination 'platform=macOS,arch=arm64' test 2>&1 | tail -5
```

- [ ] **Step 2: Push + minimal-prompt fresh-eyes review per the standing directive**

```bash
git push -u origin fix/menubar-process-groups
```

Dispatch `feature-dev:code-reviewer` with: "Review the changes on branch `fix/menubar-process-groups` (tip <SHA>) against `origin/main`. Repo at /Users/alex/mcp-gateway. Report findings with confidence scores. Address ≥80 confidence before merge."

- [ ] **Step 3: Open PR**

```bash
gh pr create --title "feat(menubar): process groups via setpgid (PR-2 of 4)" --body-file <tmp>
```

PR body references:
- Spec: `docs/superpowers/specs/2026-05-11-menubar-process-mgmt-tier-bc-design.md`
- Audit memo: `project_menubar_process_management_audit.md`
- Sibling PR: PR-1 (shutdown-aware HealthChecker)

- [ ] **Step 4: Watch CI and merge**

Once 6 of 6 green: `gh pr merge <N> --squash --delete-branch`.

---

## Self-review checklist

- **Spec coverage:** Spec section "Process groups for the remaining children" → covered by Tasks 1-5. Spec's "fallback to a separate SpawnedProcess class" → explicitly NOT taken (extension approach works per Task 2's test). ✓
- **Type consistency:** `Process.runAsProcessGroupLeader()` is the consistent name everywhere. ✓
- **No placeholders:** All code blocks complete; commands include expected output. ✓

## Out of scope

- launchd migration for Postgres + Tika (PR-4)
- Force Stop All menu (PR-3)
- `killStaleProcess(onPort:)` in ServiceManager — uses `lsof` output (leaf PIDs), upgrading to killpg there would require also looking up pgids; skipped because the existing SIGTERM→SIGKILL escalation is bounded at 500 ms and rarely catches grandchildren anyway.
- `PostgresService` — pg_ctl is a short-lived launcher; the postmaster pgid is launchd's concern post-PR-4.

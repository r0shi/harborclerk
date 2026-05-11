# PR-3: Force Stop All Menu Item Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a "Force Stop All" menu item under the existing "Stop All" entry so users have a manual escape hatch that doesn't require waiting the 30-second deadline. Reuses `ServiceManager.forceKillEverything()` (already in place from PR #341); keeps the menubar open afterward so the user can re-Start All when ready.

**Architecture:** Single NSMenuItem addition in `AppDelegate.setupMenu()`. Click handler shows an NSAlert confirmation, then calls `forceKillEverything()` (and once PR-4 ships, `await launchdAgent.stop()` for each launchd agent — but this PR ships first and forceKillEverything alone is sufficient at the time of merge).

**Tech Stack:** Swift 5+, AppKit (NSMenuItem, NSAlert), XCTest.

---

## Context the engineer needs to read first

1. **`/Users/alex/mcp-gateway/docs/superpowers/specs/2026-05-11-menubar-process-mgmt-tier-bc-design.md`** — Section "Force Stop All menu" and "Detailed design / Force Stop All menu wiring" are the relevant ones.

2. **`/Users/alex/mcp-gateway/macos/HarborClerkServer/HarborClerkServer/AppDelegate.swift`** — `setupMenu()` is at lines ~70-118. The existing "Stop All" item is at line 93-95. We insert our new item directly after it.

3. **`/Users/alex/mcp-gateway/macos/HarborClerkServer/HarborClerkServer/ServiceManager.swift`** — `forceKillEverything()` is the existing function we're calling. From PR #341.

4. **Note on ordering with PR-4 (launchd migration):** This plan (PR-3) ships BEFORE PR-4. At PR-3 merge time, Postgres + Tika are still menubar children, so `forceKillEverything()` alone handles them. When PR-4 lands, the launchd-agent stop call will need to be added to this menu's handler — see PR-4 plan, Task on "wire Force Stop All into launchd-agent shutdown."

## How to run tests

Same as PR-1:

```bash
cd /Users/alex/mcp-gateway/macos/HarborClerkServer
xcodebuild -project HarborClerkServer.xcodeproj -scheme HarborClerkServer -destination 'platform=macOS,arch=arm64' test 2>&1 | tail -15
```

UI behaviour (the alert, the menu item) is hard to test from XCTest without UI testing infrastructure. We rely on source-text guards for the wiring, and manual smoke testing for the dialog itself.

## File structure

**Modify:**
- `macos/HarborClerkServer/HarborClerkServer/AppDelegate.swift` — add menu item + action handler

**Test:**
- `macos/HarborClerkServer/HarborClerkServerTests/ServiceConfigTests.swift` — add source-text guards

No new files. No xcodeproj edits.

---

### Task 1: Add the Force Stop All menu item

**Files:**
- Modify: `macos/HarborClerkServer/HarborClerkServer/AppDelegate.swift`
- Test: `macos/HarborClerkServer/HarborClerkServerTests/ServiceConfigTests.swift`

- [ ] **Step 1: Write a failing test that asserts the menu has a "Force Stop All" item**

Add to `ServiceConfigTests.swift`:

```swift
/// AppDelegate.setupMenu must register a "Force Stop All" item.
/// Source-text guard — runtime menu inspection is brittle from a
/// non-UI XCTest. Manual smoke test covers the UI side.
func testAppDelegateRegistersForceStopAllMenuItem() throws {
    let url = URL(fileURLWithPath: "/Users/alex/mcp-gateway/macos/HarborClerkServer/HarborClerkServer/AppDelegate.swift")
    let source = try String(contentsOf: url, encoding: .utf8)
    XCTAssertTrue(
        source.contains("\"Force Stop All\""),
        "AppDelegate.swift must declare a 'Force Stop All' menu item"
    )
    XCTAssertTrue(
        source.contains("forceKillEverything()"),
        "AppDelegate.swift must call serviceManager.forceKillEverything() (likely in the Force Stop All action handler)"
    )
}
```

- [ ] **Step 2: Run to confirm it fails**

```bash
xcodebuild ... -only-testing:HarborClerkServerTests/ServiceConfigTests/testAppDelegateRegistersForceStopAllMenuItem test 2>&1 | tail -10
```

Expected: assertion failure on both lines.

- [ ] **Step 3: Add the menu item and action handler to AppDelegate.swift**

Locate the existing "Stop All" item in `setupMenu()`:

```swift
let stopItem = NSMenuItem(title: "Stop All", action: #selector(stopAllServices), keyEquivalent: "")
stopItem.target = self
menu.addItem(stopItem)

menu.addItem(NSMenuItem.separator())
```

Replace with:

```swift
let stopItem = NSMenuItem(title: "Stop All", action: #selector(stopAllServices), keyEquivalent: "")
stopItem.target = self
menu.addItem(stopItem)

let forceStopItem = NSMenuItem(title: "Force Stop All", action: #selector(forceStopAllServices), keyEquivalent: "")
forceStopItem.target = self
menu.addItem(forceStopItem)

menu.addItem(NSMenuItem.separator())
```

Then add the action handler. Locate the existing `stopAllServices` action method:

```swift
@objc private func stopAllServices() {
    Task { await serviceManager.stopAll() }
}
```

Add immediately after:

```swift
@objc private func forceStopAllServices() {
    // Confirmation dialog — destructive operation. Default button is
    // "Force Stop" so the keyboard-Return path matches the user's
    // intent (they clicked the scary menu item; default-action commits).
    let alert = NSAlert()
    alert.messageText = "Force Stop All Services?"
    alert.informativeText = """
        This will SIGKILL every Harbor Clerk subprocess.

        In-flight database transactions and document processing will be lost. \
        The menubar will stay open so you can Start All when ready.

        Use Quit instead if you want a clean shutdown.
        """
    alert.alertStyle = .warning
    alert.addButton(withTitle: "Force Stop")
    alert.addButton(withTitle: "Cancel")
    guard alert.runModal() == .alertFirstButtonReturn else { return }

    Task {
        // forceKillEverything is synchronous — it sends SIGKILL to
        // every tracked PID and returns immediately. It does NOT
        // wait for processes to actually die. That's fine: the
        // menubar stays open and the user's next Start All click
        // will land on a freshly-empty state because savePidFile +
        // removePidFile happen during the kill pass.
        self.serviceManager.forceKillEverything()

        // NOTE for PR-4 follow-up: once Postgres + Tika are launchd
        // agents, this is where the agent-stop calls go:
        //   await self.serviceManager.postgresService.stop()
        //   await self.serviceManager.tikaService.stop()
        // For now (pre-PR-4), forceKillEverything's "kill the
        // postmaster.pid PID" branch handles Postgres adequately.

        self.serviceManager.notifyStateChanged()
    }
}
```

- [ ] **Step 4: Run the test to confirm it passes**

```bash
xcodebuild ... -only-testing:HarborClerkServerTests/ServiceConfigTests/testAppDelegateRegistersForceStopAllMenuItem test 2>&1 | tail -10
```

Expected: pass.

- [ ] **Step 5: Run the full test suite to confirm no regression**

```bash
xcodebuild ... test 2>&1 | tail -5
```

Expected: `TEST SUCCEEDED` with one more test than the post-PR-2 baseline.

- [ ] **Step 6: Manually smoke-test the dialog**

This step doesn't have an automated test. Build and run the app:

```bash
xcodebuild -project HarborClerkServer.xcodeproj -scheme HarborClerkServer -destination 'platform=macOS,arch=arm64' build 2>&1 | tail -3
open ~/Library/Developer/Xcode/DerivedData/HarborClerkServer-*/Build/Products/Debug/HarborClerkServer.app
```

In the menubar:
1. Click the Harbor Clerk Server icon.
2. Verify "Force Stop All" appears under "Stop All" and above the next separator.
3. Click "Force Stop All".
4. Verify the alert text matches what was added in Step 3.
5. Click "Cancel" — nothing should happen.
6. Click "Force Stop All" again, click "Force Stop" — services should die and the menubar should stay open.
7. Click "Start All" — services should come back.

- [ ] **Step 7: Commit**

```bash
git add macos/HarborClerkServer/HarborClerkServer/AppDelegate.swift \
        macos/HarborClerkServer/HarborClerkServerTests/ServiceConfigTests.swift
git commit -m "feat(menubar): add Force Stop All menu item

Manual escape hatch for users who'd rather not wait the 30s deadline.
NSAlert confirmation gates the destructive action; on confirm,
forceKillEverything() SIGKILLs every tracked subprocess and the
menubar stays open so Start All can bring them back.

PR-4 follow-up: once Postgres+Tika are launchd agents, this handler
also needs to await each agent.stop() — left as a TODO comment in
the source for the next PR.

Spec: docs/superpowers/specs/2026-05-11-menubar-process-mgmt-tier-bc-design.md"
```

---

### Task 2: Fresh-eyes review and PR

- [ ] **Step 1: Verify build + tests**

```bash
cd /Users/alex/mcp-gateway/macos/HarborClerkServer
xcodebuild -project HarborClerkServer.xcodeproj -scheme HarborClerkServer -destination 'platform=macOS,arch=arm64' build 2>&1 | tail -3
xcodebuild -project HarborClerkServer.xcodeproj -scheme HarborClerkServer -destination 'platform=macOS,arch=arm64' test 2>&1 | tail -5
```

- [ ] **Step 2: Push + fresh-eyes review (only if the change is substantive enough)**

A single menu item + alert is a small change. The standing directive's fresh-eyes review applies to "multi-component, multi-subsystem, or non-trivial failure paths" PRs. This one isn't — it touches one file, reuses an already-tested function. Skip the formal fresh-eyes review; rely on the manual smoke test in Task 1 Step 6.

```bash
git push -u origin fix/menubar-force-stop-all
```

- [ ] **Step 3: Open PR**

```bash
gh pr create --title "feat(menubar): Force Stop All menu item (PR-3 of 4)" --body-file <tmp>
```

PR body references the spec + audit memo + sibling PRs (PR-1, PR-2).

- [ ] **Step 4: Watch CI, merge**

```bash
gh pr merge <N> --squash --delete-branch
```

---

## Self-review checklist

- **Spec coverage:** Spec section "Force Stop All menu" → fully covered by Task 1. The PR-4 follow-up (await launchd agent stop) is documented inline as a TODO comment and in PR-4's plan. ✓
- **Type consistency:** `forceKillEverything` is the consistent name from PR #341. ✓
- **No placeholders:** Alert text is the exact final copy. Code blocks are complete. ✓

## Out of scope

- launchd-agent stop integration (PR-4)
- "Uninstall Services" menu item (separately deferred per `pr_followups.md`)
- Localization of the alert text (we don't currently localize anything in this app)

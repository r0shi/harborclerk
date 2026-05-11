# PR-4: launchd Migration for Postgres + Tika Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move Postgres and Tika out of the menubar's direct subprocess management into launchd-managed LaunchAgents under `gui/$UID`. Postgres survives menubar crashes (closing the mid-query data-loss exposure from `project_menubar_crashes_during_model_switch.md`); Tika sheds its child-JVM leak risk by inheriting launchd's correct process-group cleanup; both services skip the Pipe+waitUntilExit dance entirely because launchd routes stdio to files.

**Architecture:** Three new files (`LaunchctlClient.swift`, `LaunchdAgent.swift`, `LaunchdPlist.swift`). `PostgresService` and `TikaService` keep the `ManagedService` protocol but delegate `start()`/`stop()`/`healthCheck()` to a `LaunchdAgent` instance. Plists are generated from current `Bundle.main.resourceURL` + `AppSettings` on every menubar startup and only bootout-rewrite-bootstrap'd if their on-disk content differs. Control model Y per the spec: `RunAtLoad=false`, `KeepAlive={SuccessfulExit:false}` — menubar still owns the start/stop decision; launchd contributes crash-survival only.

**Tech Stack:** Swift 5+, Foundation, AppKit, XCTest. macOS-only. Shells out to `/bin/launchctl` for lifecycle control.

---

## Context the engineer needs to read first

This is the biggest of the four sub-PRs. Read all of these before writing code:

1. **`/Users/alex/mcp-gateway/docs/superpowers/specs/2026-05-11-menubar-process-mgmt-tier-bc-design.md`** — Sections "Postgres + Tika move to launchd agents", "Detailed design / LaunchdAgent class", "Detailed design / Plist contents", "Detailed design / PostgresService / TikaService refactor", "Risks", "Open implementation questions". Especially the last one — it lists details to settle during implementation.

2. **`/Users/alex/.claude/projects/-Users-alex-mcp-gateway/memory/project_menubar_process_management_audit.md`** — the original audit. Tier C section is the architectural argument.

3. **Current service implementations (heavily reshaped here):**
   - `macos/HarborClerkServer/HarborClerkServer/Services/PostgresService.swift` (~397 lines) — pay particular attention to `start()` (the initdb + pg_ctl flow), `stop()` (the fast→immediate→SIGKILL escalation), `pgEnvironment()`, and `ensureLoggingConfig()`. The new flow keeps `initdb`/`createDatabaseAndExtensions`/`ensureLoggingConfig` unchanged (they're invoked once per data dir lifetime) but replaces direct `pg_ctl start`/`stop` with launchd lifecycle.
   - `macos/HarborClerkServer/HarborClerkServer/Services/TikaService.swift` (~84 lines) — simpler than Postgres. Just exec `java -jar tika-server.jar`.

4. **`launchctl` man page** (`man launchctl` from a terminal). The relevant subcommands are: `bootstrap`, `bootout`, `kickstart`, `print`. The legacy `load`/`unload` are deprecated; use `bootstrap`/`bootout`.

5. **macOS LaunchAgent plist documentation:** Apple's `launchd.plist(5)` man page covers the keys we use (`Label`, `Program`, `ProgramArguments`, `EnvironmentVariables`, `RunAtLoad`, `KeepAlive`, `StandardOutPath`, `StandardErrorPath`).

6. **`macos/HarborClerkServer/HarborClerkServer/HealthChecker.swift`** — we add an `isLaunchdManaged` skip path here so the menubar's auto-restart doesn't fight launchd's `KeepAlive`.

## How to run tests

Same as the other PRs. Unit-testable parts: `LaunchctlClient` protocol + fake impl, `LaunchdPlist` generation. Integration-testable: nothing — real `launchctl` invocations are macOS-state-modifying; the test plan covers them via manual smoke testing.

```bash
cd /Users/alex/mcp-gateway/macos/HarborClerkServer
xcodebuild -project HarborClerkServer.xcodeproj -scheme HarborClerkServer -destination 'platform=macOS,arch=arm64' test 2>&1 | tail -15
```

## How to add Swift files to the xcodeproj

This PR creates three new Swift sources. Each must be registered with the project file:

1. Add `PBXBuildFile` entry near the top (assign new IDs `A100000061`, `A100000062`, `A100000063`).
2. Add `PBXFileReference` entry in the fileRefs section (`B100000061`, etc).
3. Add to the HarborClerkServer group's `children` list.
4. Add to the `Sources` build phase.

PR-1's `ProcessExtensions.swift` did this — search `project.pbxproj` for `B100000060` for the pattern.

## File structure

**Create:**
- `macos/HarborClerkServer/HarborClerkServer/LaunchctlClient.swift` (~80 lines) — protocol + real impl + fake impl for tests
- `macos/HarborClerkServer/HarborClerkServer/LaunchdAgent.swift` (~150 lines) — the agent abstraction (install/start/stop/status/uninstall)
- `macos/HarborClerkServer/HarborClerkServer/LaunchdPlist.swift` (~120 lines) — plist generation for postgres + tika
- `macos/HarborClerkServer/HarborClerkServerTests/LaunchctlClientTests.swift` — fake impl behaviour
- `macos/HarborClerkServer/HarborClerkServerTests/LaunchdAgentTests.swift` — lifecycle logic against fake
- `macos/HarborClerkServer/HarborClerkServerTests/LaunchdPlistTests.swift` — generation correctness

**Modify:**
- `macos/HarborClerkServer/HarborClerkServer/Services/PostgresService.swift` — `start()`/`stop()`/`healthCheck()` delegate to LaunchdAgent
- `macos/HarborClerkServer/HarborClerkServer/Services/TikaService.swift` — same
- `macos/HarborClerkServer/HarborClerkServer/HealthChecker.swift` — skip auto-restart for launchd-managed services
- `macos/HarborClerkServer/HarborClerkServer/AppDelegate.swift` — Force Stop All also stops launchd agents (per PR-3's TODO)
- `macos/HarborClerkServer/HarborClerkServer.xcodeproj/project.pbxproj` — register the three new sources + three new test files

---

### Task 1: Create LaunchctlClient protocol + real impl + fake impl

**Files:**
- Create: `macos/HarborClerkServer/HarborClerkServer/LaunchctlClient.swift`
- Create: `macos/HarborClerkServer/HarborClerkServerTests/LaunchctlClientTests.swift`
- Modify: `project.pbxproj`

- [ ] **Step 1: Write the file**

```swift
// macos/HarborClerkServer/HarborClerkServer/LaunchctlClient.swift
import Foundation

/// Result of a `launchctl` invocation.
struct LaunchctlResult: Equatable {
    let exitCode: Int32
    let stdout: String
    let stderr: String

    var success: Bool { exitCode == 0 }
}

/// Abstraction over `/bin/launchctl` invocations so the LaunchdAgent
/// lifecycle logic is unit-testable. Production uses `RealLaunchctlClient`
/// which shells out via `Process`; tests use `FakeLaunchctlClient` which
/// records invocations and returns scripted results.
protocol LaunchctlClient: AnyObject {
    /// `launchctl bootstrap <domain-target> <plist-path>`
    func bootstrap(domain: String, plistPath: URL) async -> LaunchctlResult

    /// `launchctl bootout <service-target>` where service-target is
    /// `<domain>/<label>` (or pass the plist path — both work; the
    /// service-target form is preferred).
    func bootout(serviceTarget: String) async -> LaunchctlResult

    /// `launchctl kickstart -k <service-target>` — start the service, or
    /// restart if already running (-k for "kill and restart").
    func kickstart(serviceTarget: String) async -> LaunchctlResult

    /// `launchctl print <service-target>` — returns the parsed PID + state.
    /// Result.stdout is the raw output; the caller (LaunchdAgent) parses.
    func print_(serviceTarget: String) async -> LaunchctlResult
}

/// Production implementation that shells out via `Process`.
final class RealLaunchctlClient: LaunchctlClient {
    private static let launchctlPath = "/bin/launchctl"

    func bootstrap(domain: String, plistPath: URL) async -> LaunchctlResult {
        await run(["bootstrap", domain, plistPath.path])
    }

    func bootout(serviceTarget: String) async -> LaunchctlResult {
        await run(["bootout", serviceTarget])
    }

    func kickstart(serviceTarget: String) async -> LaunchctlResult {
        await run(["kickstart", "-k", serviceTarget])
    }

    func print_(serviceTarget: String) async -> LaunchctlResult {
        await run(["print", serviceTarget])
    }

    private func run(_ args: [String]) async -> LaunchctlResult {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: Self.launchctlPath)
        proc.arguments = args
        let stdout = Pipe()
        let stderr = Pipe()
        proc.standardOutput = stdout
        proc.standardError = stderr

        do {
            try proc.run()
        } catch {
            return LaunchctlResult(
                exitCode: -1,
                stdout: "",
                stderr: "failed to launch launchctl: \(error)",
            )
        }

        return await withCheckedContinuation { c in
            DispatchQueue.global().async {
                proc.waitUntilExit()
                let outData = stdout.fileHandleForReading.readDataToEndOfFile()
                let errData = stderr.fileHandleForReading.readDataToEndOfFile()
                c.resume(returning: LaunchctlResult(
                    exitCode: proc.terminationStatus,
                    stdout: String(data: outData, encoding: .utf8) ?? "",
                    stderr: String(data: errData, encoding: .utf8) ?? "",
                ))
            }
        }
    }
}

/// Test implementation that records every invocation and returns
/// caller-scripted results. Default behaviour: every call returns
/// success with empty output.
final class FakeLaunchctlClient: LaunchctlClient {
    /// Recorded invocations, in call order. Format: "verb arg1 arg2 ..."
    /// e.g. "bootstrap gui/501 /Users/alex/Library/LaunchAgents/com.example.plist"
    private(set) var invocations: [String] = []

    /// Override the result for the next matching invocation. Set on a
    /// per-verb basis. If not set, defaults to a success result.
    var nextBootstrapResult: LaunchctlResult?
    var nextBootoutResult: LaunchctlResult?
    var nextKickstartResult: LaunchctlResult?
    var nextPrintResult: LaunchctlResult?

    func bootstrap(domain: String, plistPath: URL) async -> LaunchctlResult {
        invocations.append("bootstrap \(domain) \(plistPath.path)")
        let r = nextBootstrapResult ?? .ok
        nextBootstrapResult = nil
        return r
    }
    func bootout(serviceTarget: String) async -> LaunchctlResult {
        invocations.append("bootout \(serviceTarget)")
        let r = nextBootoutResult ?? .ok
        nextBootoutResult = nil
        return r
    }
    func kickstart(serviceTarget: String) async -> LaunchctlResult {
        invocations.append("kickstart \(serviceTarget)")
        let r = nextKickstartResult ?? .ok
        nextKickstartResult = nil
        return r
    }
    func print_(serviceTarget: String) async -> LaunchctlResult {
        invocations.append("print \(serviceTarget)")
        let r = nextPrintResult ?? .ok
        nextPrintResult = nil
        return r
    }
}

extension LaunchctlResult {
    static let ok = LaunchctlResult(exitCode: 0, stdout: "", stderr: "")
}
```

- [ ] **Step 2: Write tests against the fake**

```swift
// macos/HarborClerkServer/HarborClerkServerTests/LaunchctlClientTests.swift
import XCTest
@testable import HarborClerkServer

final class LaunchctlClientTests: XCTestCase {
    func testFakeRecordsInvocations() async throws {
        let fake = FakeLaunchctlClient()
        _ = await fake.bootstrap(domain: "gui/501", plistPath: URL(fileURLWithPath: "/tmp/test.plist"))
        _ = await fake.bootout(serviceTarget: "gui/501/com.example")
        _ = await fake.kickstart(serviceTarget: "gui/501/com.example")
        _ = await fake.print_(serviceTarget: "gui/501/com.example")

        XCTAssertEqual(fake.invocations, [
            "bootstrap gui/501 /tmp/test.plist",
            "bootout gui/501/com.example",
            "kickstart gui/501/com.example",
            "print gui/501/com.example",
        ])
    }

    func testFakeReturnsScriptedResult() async throws {
        let fake = FakeLaunchctlClient()
        fake.nextBootstrapResult = LaunchctlResult(exitCode: 5, stdout: "", stderr: "bootstrap failed: domain not loaded")
        let r = await fake.bootstrap(domain: "gui/501", plistPath: URL(fileURLWithPath: "/x"))
        XCTAssertEqual(r.exitCode, 5)
        XCTAssertFalse(r.success)
        XCTAssertEqual(r.stderr, "bootstrap failed: domain not loaded")
    }

    func testFakeDefaultsToSuccess() async throws {
        let fake = FakeLaunchctlClient()
        let r = await fake.bootout(serviceTarget: "anything")
        XCTAssertTrue(r.success)
    }
}
```

- [ ] **Step 3: Register both files in `project.pbxproj`**

Follow the pattern used for `ProcessExtensions.swift` in PR #341. New IDs:
- `A100000061 /* LaunchctlClient.swift in Sources */` → `B100000061`
- `AT10000012 /* LaunchctlClientTests.swift in Sources */` → `BT10000012`

Add to the main target's Sources phase + HarborClerkServer group's children. Add the test file to the test target's Sources phase + HarborClerkServerTests group.

- [ ] **Step 4: Build and run tests**

```bash
cd /Users/alex/mcp-gateway/macos/HarborClerkServer
xcodebuild -project HarborClerkServer.xcodeproj -scheme HarborClerkServer -destination 'platform=macOS,arch=arm64' build 2>&1 | tail -3
xcodebuild ... -only-testing:HarborClerkServerTests/LaunchctlClientTests test 2>&1 | tail -10
```

Expected: `BUILD SUCCEEDED` and all 3 new tests pass.

- [ ] **Step 5: Commit**

```bash
git add macos/HarborClerkServer/HarborClerkServer/LaunchctlClient.swift \
        macos/HarborClerkServer/HarborClerkServerTests/LaunchctlClientTests.swift \
        macos/HarborClerkServer/HarborClerkServer.xcodeproj/project.pbxproj
git commit -m "feat(menubar): LaunchctlClient protocol + real + fake impls

Foundation for the launchd migration. RealLaunchctlClient shells out
via Process; FakeLaunchctlClient records invocations + returns scripted
results so LaunchdAgent lifecycle is unit-testable without modifying
real macOS state.

Spec: docs/superpowers/specs/2026-05-11-menubar-process-mgmt-tier-bc-design.md"
```

---

### Task 2: Create LaunchdPlist with postgres + tika generators

**Files:**
- Create: `macos/HarborClerkServer/HarborClerkServer/LaunchdPlist.swift`
- Create: `macos/HarborClerkServer/HarborClerkServerTests/LaunchdPlistTests.swift`
- Modify: `project.pbxproj`

- [ ] **Step 1: Write the file**

```swift
// macos/HarborClerkServer/HarborClerkServer/LaunchdPlist.swift
import Foundation

/// Generates LaunchAgent plist XML for our two launchd-managed services.
///
/// The plists hold absolute paths to the bundled binaries plus the user's
/// data + logs directories. Because the bundle can live in any location
/// (the user might keep it in /Applications, ~/Applications, or run it
/// from a build directory), plist contents are regenerated on every
/// menubar startup from the current `Bundle.main.resourceURL` and
/// `AppSettings`. LaunchdAgent.ensureInstalled compares the generated
/// content to what's on disk and only bootout-rewrite-bootstraps when
/// something changed.
enum LaunchdPlist {

    /// Postgres LaunchAgent plist contents. The `Program` is `postgres`
    /// directly (NOT `pg_ctl`) so launchd tracks the postmaster's PID.
    /// pg_ctl is a wrapper that starts postgres and exits; if launchd
    /// tracked pg_ctl it would either restart-loop it or consider the
    /// service gone.
    static func postgres(bundle: URL, dataDir: URL, logsDir: URL, port: Int) -> String {
        let postgresBin = bundle.appendingPathComponent("postgres/bin/postgres").path
        let pgBinDir = bundle.appendingPathComponent("postgres/bin").path
        let pgLibDir = bundle.appendingPathComponent("postgres/lib").path
        let pgShareDir = bundle.appendingPathComponent("postgres/share").path
        let logFile = logsDir.appendingPathComponent("postgres-launchd.log").path

        return """
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0">
        <dict>
            <key>Label</key>
            <string>com.harborclerk.postgres</string>
            <key>Program</key>
            <string>\(postgresBin)</string>
            <key>ProgramArguments</key>
            <array>
                <string>\(postgresBin)</string>
                <string>-D</string>
                <string>\(dataDir.path)</string>
                <string>-p</string>
                <string>\(port)</string>
                <string>-k</string>
                <string>/tmp</string>
            </array>
            <key>EnvironmentVariables</key>
            <dict>
                <key>PGDATA</key>
                <string>\(dataDir.path)</string>
                <key>PATH</key>
                <string>\(pgBinDir):/usr/bin:/bin</string>
                <key>LD_LIBRARY_PATH</key>
                <string>\(pgLibDir)</string>
                <key>DYLD_LIBRARY_PATH</key>
                <string>\(pgLibDir)</string>
                <key>PGSHARE</key>
                <string>\(pgShareDir)</string>
            </dict>
            <key>RunAtLoad</key>
            <false/>
            <key>KeepAlive</key>
            <dict>
                <key>SuccessfulExit</key>
                <false/>
            </dict>
            <key>StandardOutPath</key>
            <string>\(logFile)</string>
            <key>StandardErrorPath</key>
            <string>\(logFile)</string>
        </dict>
        </plist>
        """
    }

    /// Tika LaunchAgent plist contents. Same shape as postgres.
    static func tika(bundle: URL, logsDir: URL, port: Int) -> String {
        let javaBin = bundle.appendingPathComponent("java/Contents/Home/bin/java").path
        let javaHome = bundle.appendingPathComponent("java/Contents/Home").path
        let tikaJar = bundle.appendingPathComponent("tika/tika-server.jar").path
        let logFile = logsDir.appendingPathComponent("tika-launchd.log").path

        return """
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0">
        <dict>
            <key>Label</key>
            <string>com.harborclerk.tika</string>
            <key>Program</key>
            <string>\(javaBin)</string>
            <key>ProgramArguments</key>
            <array>
                <string>\(javaBin)</string>
                <string>-jar</string>
                <string>\(tikaJar)</string>
                <string>--host</string>
                <string>127.0.0.1</string>
                <string>--port</string>
                <string>\(port)</string>
            </array>
            <key>EnvironmentVariables</key>
            <dict>
                <key>JAVA_HOME</key>
                <string>\(javaHome)</string>
                <key>PATH</key>
                <string>\(javaHome)/bin:/usr/bin:/bin</string>
            </dict>
            <key>RunAtLoad</key>
            <false/>
            <key>KeepAlive</key>
            <dict>
                <key>SuccessfulExit</key>
                <false/>
            </dict>
            <key>StandardOutPath</key>
            <string>\(logFile)</string>
            <key>StandardErrorPath</key>
            <string>\(logFile)</string>
        </dict>
        </plist>
        """
    }
}
```

- [ ] **Step 2: Write tests**

```swift
// macos/HarborClerkServer/HarborClerkServerTests/LaunchdPlistTests.swift
import XCTest
@testable import HarborClerkServer

final class LaunchdPlistTests: XCTestCase {
    private let testBundle = URL(fileURLWithPath: "/Applications/HarborClerkServer.app/Contents/Resources")
    private let testDataDir = URL(fileURLWithPath: "/Users/test/Library/Application Support/Harbor Clerk/postgres-data")
    private let testLogsDir = URL(fileURLWithPath: "/Users/test/Library/Application Support/Harbor Clerk/logs")

    func testPostgresPlistIsValidXMLPropertyList() throws {
        let xml = LaunchdPlist.postgres(bundle: testBundle, dataDir: testDataDir, logsDir: testLogsDir, port: 5433)
        let data = xml.data(using: .utf8)!
        // PropertyListSerialization will throw if the XML doesn't parse
        // as a property list. That's our basic correctness check.
        let parsed = try PropertyListSerialization.propertyList(from: data, options: [], format: nil) as? [String: Any]
        XCTAssertNotNil(parsed)
        XCTAssertEqual(parsed?["Label"] as? String, "com.harborclerk.postgres")
    }

    func testPostgresPlistHasCorrectKeys() throws {
        let xml = LaunchdPlist.postgres(bundle: testBundle, dataDir: testDataDir, logsDir: testLogsDir, port: 5433)
        let parsed = try PropertyListSerialization.propertyList(from: xml.data(using: .utf8)!, options: [], format: nil) as! [String: Any]

        XCTAssertEqual(parsed["Label"] as? String, "com.harborclerk.postgres")
        XCTAssertEqual(parsed["Program"] as? String, testBundle.appendingPathComponent("postgres/bin/postgres").path)
        XCTAssertEqual(parsed["RunAtLoad"] as? Bool, false)
        let keepAlive = parsed["KeepAlive"] as? [String: Any]
        XCTAssertEqual(keepAlive?["SuccessfulExit"] as? Bool, false)
        let env = parsed["EnvironmentVariables"] as? [String: String]
        XCTAssertEqual(env?["PGDATA"], testDataDir.path)
    }

    func testPostgresPlistArgsIncludePortAndDataDir() throws {
        let xml = LaunchdPlist.postgres(bundle: testBundle, dataDir: testDataDir, logsDir: testLogsDir, port: 5433)
        let parsed = try PropertyListSerialization.propertyList(from: xml.data(using: .utf8)!, options: [], format: nil) as! [String: Any]
        let args = parsed["ProgramArguments"] as! [String]
        XCTAssertTrue(args.contains("5433"))
        XCTAssertTrue(args.contains(testDataDir.path))
        XCTAssertTrue(args.contains("-D"))
        XCTAssertTrue(args.contains("-p"))
    }

    func testTikaPlistIsValidPropertyList() throws {
        let xml = LaunchdPlist.tika(bundle: testBundle, logsDir: testLogsDir, port: 9998)
        let parsed = try PropertyListSerialization.propertyList(from: xml.data(using: .utf8)!, options: [], format: nil) as! [String: Any]
        XCTAssertEqual(parsed["Label"] as? String, "com.harborclerk.tika")
        let env = parsed["EnvironmentVariables"] as? [String: String]
        XCTAssertEqual(env?["JAVA_HOME"], testBundle.appendingPathComponent("java/Contents/Home").path)
    }

    func testTikaPlistArgsIncludePort() throws {
        let xml = LaunchdPlist.tika(bundle: testBundle, logsDir: testLogsDir, port: 9998)
        let parsed = try PropertyListSerialization.propertyList(from: xml.data(using: .utf8)!, options: [], format: nil) as! [String: Any]
        let args = parsed["ProgramArguments"] as! [String]
        XCTAssertTrue(args.contains("9998"))
        XCTAssertTrue(args.contains("--host"))
        XCTAssertTrue(args.contains("127.0.0.1"))
    }
}
```

- [ ] **Step 3: Register in project.pbxproj**

IDs: `A100000062 / B100000062` for the source, `AT10000013 / BT10000013` for the test.

- [ ] **Step 4: Build + run tests**

```bash
xcodebuild ... build 2>&1 | tail -3
xcodebuild ... -only-testing:HarborClerkServerTests/LaunchdPlistTests test 2>&1 | tail -10
```

Expected: all 5 plist tests pass.

- [ ] **Step 5: Commit**

```bash
git add macos/HarborClerkServer/HarborClerkServer/LaunchdPlist.swift \
        macos/HarborClerkServer/HarborClerkServerTests/LaunchdPlistTests.swift \
        macos/HarborClerkServer/HarborClerkServer.xcodeproj/project.pbxproj
git commit -m "feat(menubar): LaunchdPlist generators for postgres + tika

Plist XML built from current Bundle.main.resourceURL + AppSettings so
the agents track wherever the bundle currently lives. Property-list
serialisation tests confirm the XML parses + has expected key shape.

Spec: docs/superpowers/specs/2026-05-11-menubar-process-mgmt-tier-bc-design.md"
```

---

### Task 3: Create LaunchdAgent with install/start/stop/status/uninstall

**Files:**
- Create: `macos/HarborClerkServer/HarborClerkServer/LaunchdAgent.swift`
- Create: `macos/HarborClerkServer/HarborClerkServerTests/LaunchdAgentTests.swift`
- Modify: `project.pbxproj`

- [ ] **Step 1: Write LaunchdAgent.swift**

```swift
// macos/HarborClerkServer/HarborClerkServer/LaunchdAgent.swift
import Foundation
import os

enum LaunchdAgentError: LocalizedError {
    case plistWriteFailed(URL, Error)
    case bootstrapFailed(LaunchctlResult)
    case bootoutFailed(LaunchctlResult)
    case kickstartFailed(LaunchctlResult)

    var errorDescription: String? {
        switch self {
        case .plistWriteFailed(let url, let err): return "failed to write plist at \(url.path): \(err)"
        case .bootstrapFailed(let r):              return "launchctl bootstrap failed (exit \(r.exitCode)): \(r.stderr)"
        case .bootoutFailed(let r):                return "launchctl bootout failed (exit \(r.exitCode)): \(r.stderr)"
        case .kickstartFailed(let r):              return "launchctl kickstart failed (exit \(r.exitCode)): \(r.stderr)"
        }
    }
}

enum LaunchdServiceState: Equatable {
    case loaded(pid: Int32?)   // pid nil if loaded-but-not-running
    case notLoaded
}

/// Wraps the lifecycle of one LaunchAgent: idempotent plist install,
/// start (kickstart), stop (bootout), status (print + parse), uninstall
/// (bootout + delete plist).
final class LaunchdAgent {
    let label: String
    let plistURL: URL
    private let launchctl: LaunchctlClient
    private let log: Logger

    var domainTarget: String { "gui/\(getuid())" }
    var serviceTarget: String { "\(domainTarget)/\(label)" }

    init(label: String, plistURL: URL, launchctl: LaunchctlClient = RealLaunchctlClient()) {
        self.label = label
        self.plistURL = plistURL
        self.launchctl = launchctl
        self.log = Log.logger("launchd-\(label.replacingOccurrences(of: ".", with: "-"))")
    }

    /// Write the plist to disk if its contents differ from the new
    /// `content`. If the plist had to be rewritten (or wasn't on disk),
    /// bootout the existing service (idempotent — bootout of a not-
    /// loaded service is allowed) and bootstrap the new one.
    ///
    /// Called by services during their start() — every menubar launch
    /// runs this, so plist drift across upgrades / bundle moves is
    /// self-healing.
    func ensureInstalled(plistContent: String) async throws {
        let onDisk = try? String(contentsOf: plistURL, encoding: .utf8)
        if onDisk == plistContent {
            log.debug("plist on disk matches; no rewrite needed")
            return
        }

        log.info("plist content drift — bootout, rewrite, bootstrap")

        // Bootout if currently loaded. Don't error if it wasn't loaded;
        // we just want a known-clean state before bootstrap.
        _ = await launchctl.bootout(serviceTarget: serviceTarget)

        // Make sure the parent directory exists.
        try FileManager.default.createDirectory(
            at: plistURL.deletingLastPathComponent(),
            withIntermediateDirectories: true,
        )

        do {
            try plistContent.write(to: plistURL, atomically: true, encoding: .utf8)
        } catch {
            throw LaunchdAgentError.plistWriteFailed(plistURL, error)
        }

        let bootstrap = await launchctl.bootstrap(domain: domainTarget, plistPath: plistURL)
        guard bootstrap.success else { throw LaunchdAgentError.bootstrapFailed(bootstrap) }
    }

    /// `launchctl kickstart -k` — starts the service, or restarts it
    /// if already running. After ensureInstalled, this is the verb to
    /// actually launch the process.
    func start() async throws {
        let r = await launchctl.kickstart(serviceTarget: serviceTarget)
        guard r.success else { throw LaunchdAgentError.kickstartFailed(r) }
    }

    /// `launchctl bootout` — stops the service. Does NOT uninstall the
    /// plist or unregister from launchd; that's `uninstall()`.
    func stop() async throws {
        let r = await launchctl.bootout(serviceTarget: serviceTarget)
        // bootout of a not-loaded service may exit non-zero. We treat
        // any "service not found" outcome as success — the goal is
        // "service is not running" and that goal is met.
        if !r.success && !r.stderr.lowercased().contains("could not find service") {
            throw LaunchdAgentError.bootoutFailed(r)
        }
    }

    /// Parse `launchctl print <service-target>` for PID + load state.
    /// Returns .notLoaded if launchctl reports no such service.
    func status() async -> LaunchdServiceState {
        let r = await launchctl.print_(serviceTarget: serviceTarget)
        if !r.success {
            // Common failure: "Could not find service ... in domain ..."
            return .notLoaded
        }
        // launchctl print output is human-readable; we look for the
        // "pid = N" line. Absent → loaded but not running. Present → loaded
        // and running.
        if let pidLine = r.stdout.split(separator: "\n").first(where: { $0.contains("pid =") }) {
            let pidStr = pidLine.split(separator: "=").last?.trimmingCharacters(in: .whitespaces) ?? ""
            return .loaded(pid: Int32(pidStr))
        }
        return .loaded(pid: nil)
    }

    /// Stop + delete plist + remove launchd registration. Used by
    /// "Uninstall Services" (future) and at-uninstall scripts.
    func uninstall() async {
        try? await stop()
        try? FileManager.default.removeItem(at: plistURL)
    }
}
```

- [ ] **Step 2: Write tests against FakeLaunchctlClient**

```swift
// macos/HarborClerkServer/HarborClerkServerTests/LaunchdAgentTests.swift
import XCTest
@testable import HarborClerkServer

final class LaunchdAgentTests: XCTestCase {
    private var tempDir: URL!
    override func setUp() async throws {
        tempDir = FileManager.default.temporaryDirectory.appendingPathComponent("launchd-agent-tests-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: tempDir, withIntermediateDirectories: true)
    }
    override func tearDown() async throws {
        try? FileManager.default.removeItem(at: tempDir)
    }

    func testEnsureInstalledNoOpsWhenPlistMatches() async throws {
        let plistURL = tempDir.appendingPathComponent("a.plist")
        try "<plist>match</plist>".write(to: plistURL, atomically: true, encoding: .utf8)
        let fake = FakeLaunchctlClient()
        let agent = LaunchdAgent(label: "com.test.a", plistURL: plistURL, launchctl: fake)

        try await agent.ensureInstalled(plistContent: "<plist>match</plist>")

        // No launchctl invocations because content matched.
        XCTAssertEqual(fake.invocations, [])
    }

    func testEnsureInstalledBootoutRewritesBootstrapWhenDrifted() async throws {
        let plistURL = tempDir.appendingPathComponent("a.plist")
        try "<plist>old</plist>".write(to: plistURL, atomically: true, encoding: .utf8)
        let fake = FakeLaunchctlClient()
        let agent = LaunchdAgent(label: "com.test.a", plistURL: plistURL, launchctl: fake)

        try await agent.ensureInstalled(plistContent: "<plist>new</plist>")

        XCTAssertEqual(fake.invocations.count, 2)
        XCTAssertTrue(fake.invocations[0].hasPrefix("bootout "))
        XCTAssertTrue(fake.invocations[1].hasPrefix("bootstrap "))
        // Verify the plist on disk got rewritten:
        XCTAssertEqual(try String(contentsOf: plistURL, encoding: .utf8), "<plist>new</plist>")
    }

    func testEnsureInstalledHandlesMissingPlistFile() async throws {
        let plistURL = tempDir.appendingPathComponent("never-existed.plist")
        let fake = FakeLaunchctlClient()
        let agent = LaunchdAgent(label: "com.test.a", plistURL: plistURL, launchctl: fake)

        try await agent.ensureInstalled(plistContent: "<plist>first</plist>")

        XCTAssertTrue(FileManager.default.fileExists(atPath: plistURL.path))
        XCTAssertEqual(fake.invocations.count, 2)
    }

    func testEnsureInstalledThrowsOnBootstrapFailure() async throws {
        let plistURL = tempDir.appendingPathComponent("a.plist")
        let fake = FakeLaunchctlClient()
        fake.nextBootstrapResult = LaunchctlResult(exitCode: 5, stdout: "", stderr: "bootstrap failed")
        let agent = LaunchdAgent(label: "com.test.a", plistURL: plistURL, launchctl: fake)

        do {
            try await agent.ensureInstalled(plistContent: "<plist>first</plist>")
            XCTFail("expected bootstrap failure")
        } catch let LaunchdAgentError.bootstrapFailed(r) {
            XCTAssertEqual(r.exitCode, 5)
        }
    }

    func testStartCallsKickstart() async throws {
        let fake = FakeLaunchctlClient()
        let agent = LaunchdAgent(label: "com.test.a", plistURL: tempDir.appendingPathComponent("a.plist"), launchctl: fake)
        try await agent.start()
        XCTAssertEqual(fake.invocations, ["kickstart gui/\(getuid())/com.test.a"])
    }

    func testStopCallsBootout() async throws {
        let fake = FakeLaunchctlClient()
        let agent = LaunchdAgent(label: "com.test.a", plistURL: tempDir.appendingPathComponent("a.plist"), launchctl: fake)
        try await agent.stop()
        XCTAssertEqual(fake.invocations, ["bootout gui/\(getuid())/com.test.a"])
    }

    func testStopTreatsNotFoundAsSuccess() async throws {
        let fake = FakeLaunchctlClient()
        fake.nextBootoutResult = LaunchctlResult(exitCode: 113, stdout: "", stderr: "Could not find service \"com.test.a\" in domain")
        let agent = LaunchdAgent(label: "com.test.a", plistURL: tempDir.appendingPathComponent("a.plist"), launchctl: fake)
        // Should NOT throw — the goal "service is not running" is met.
        try await agent.stop()
    }

    func testStopThrowsOnOtherBootoutErrors() async throws {
        let fake = FakeLaunchctlClient()
        fake.nextBootoutResult = LaunchctlResult(exitCode: 22, stdout: "", stderr: "EINVAL")
        let agent = LaunchdAgent(label: "com.test.a", plistURL: tempDir.appendingPathComponent("a.plist"), launchctl: fake)
        do {
            try await agent.stop()
            XCTFail("expected throw")
        } catch is LaunchdAgentError {
            // ok
        }
    }

    func testStatusReturnsLoadedWithPidWhenPrintShowsPid() async throws {
        let fake = FakeLaunchctlClient()
        fake.nextPrintResult = LaunchctlResult(
            exitCode: 0,
            stdout: "com.test.a = {\n\tpid = 12345\n\tstate = running\n}\n",
            stderr: "",
        )
        let agent = LaunchdAgent(label: "com.test.a", plistURL: tempDir.appendingPathComponent("a.plist"), launchctl: fake)
        let state = await agent.status()
        XCTAssertEqual(state, .loaded(pid: 12345))
    }

    func testStatusReturnsLoadedNilWhenPrintShowsNoPid() async throws {
        let fake = FakeLaunchctlClient()
        fake.nextPrintResult = LaunchctlResult(
            exitCode: 0,
            stdout: "com.test.a = {\n\tstate = waiting\n}\n",
            stderr: "",
        )
        let agent = LaunchdAgent(label: "com.test.a", plistURL: tempDir.appendingPathComponent("a.plist"), launchctl: fake)
        let state = await agent.status()
        XCTAssertEqual(state, .loaded(pid: nil))
    }

    func testStatusReturnsNotLoadedOnPrintFailure() async throws {
        let fake = FakeLaunchctlClient()
        fake.nextPrintResult = LaunchctlResult(exitCode: 113, stdout: "", stderr: "Could not find service")
        let agent = LaunchdAgent(label: "com.test.a", plistURL: tempDir.appendingPathComponent("a.plist"), launchctl: fake)
        let state = await agent.status()
        XCTAssertEqual(state, .notLoaded)
    }

    func testUninstallStopsAndDeletesPlist() async throws {
        let plistURL = tempDir.appendingPathComponent("a.plist")
        try "<plist>x</plist>".write(to: plistURL, atomically: true, encoding: .utf8)
        let fake = FakeLaunchctlClient()
        let agent = LaunchdAgent(label: "com.test.a", plistURL: plistURL, launchctl: fake)
        await agent.uninstall()
        XCTAssertFalse(FileManager.default.fileExists(atPath: plistURL.path))
        XCTAssertTrue(fake.invocations.contains(where: { $0.hasPrefix("bootout ") }))
    }
}
```

- [ ] **Step 3: Register in project.pbxproj**

IDs: `A100000063 / B100000063` source, `AT10000014 / BT10000014` test.

- [ ] **Step 4: Build + tests**

```bash
xcodebuild ... build 2>&1 | tail -3
xcodebuild ... -only-testing:HarborClerkServerTests/LaunchdAgentTests test 2>&1 | tail -10
```

Expected: all 11 LaunchdAgent tests pass.

- [ ] **Step 5: Commit**

```bash
git add macos/HarborClerkServer/HarborClerkServer/LaunchdAgent.swift \
        macos/HarborClerkServer/HarborClerkServerTests/LaunchdAgentTests.swift \
        macos/HarborClerkServer/HarborClerkServer.xcodeproj/project.pbxproj
git commit -m "feat(menubar): LaunchdAgent — install/start/stop/status/uninstall

Wraps the lifecycle of one LaunchAgent. ensureInstalled is idempotent
across menubar restarts; stop treats 'service not found' as success;
status parses launchctl print output for PID. Tested against
FakeLaunchctlClient.

Spec: docs/superpowers/specs/2026-05-11-menubar-process-mgmt-tier-bc-design.md"
```

---

### Task 4: Refactor PostgresService to delegate to LaunchdAgent

**Files:**
- Modify: `macos/HarborClerkServer/HarborClerkServer/Services/PostgresService.swift`

This is the biggest refactor in the PR. Strategy: keep the `ManagedService` protocol surface unchanged; keep the initdb / createDatabaseAndExtensions / ensureLoggingConfig flow unchanged (they run at most once per data dir lifetime); replace the `pg_ctl start`/`pg_ctl stop` calls with `agent.ensureInstalled() + agent.start()` / `agent.stop()`.

- [ ] **Step 1: Add a launchdAgent property + initializer change + isLaunchdManaged flag**

At the top of PostgresService:

```swift
final class PostgresService: ManagedService {
    let name = "PostgreSQL"
    var state: ServiceState = .stopped
    /// True for the launchd-managed services. The HealthChecker uses
    /// this to skip its own auto-restart path — launchd's KeepAlive
    /// already handles crash recovery, and double-restarting would be
    /// a foot-gun.
    let isLaunchdManaged = true

    private let agent: LaunchdAgent

    init(launchctl: LaunchctlClient = RealLaunchctlClient()) {
        let plistURL = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/LaunchAgents/com.harborclerk.postgres.plist")
        self.agent = LaunchdAgent(
            label: "com.harborclerk.postgres",
            plistURL: plistURL,
            launchctl: launchctl,
        )
    }
```

Delete the old `private var process: Process?`. We don't track a Process anymore — launchd does.

- [ ] **Step 2: Refactor `start()`**

Existing `start()` is ~80 lines: needsInit check → initializeDatabase → createDatabaseAndExtensions → ensureLoggingConfig → stale-pid cleanup → pg_ctl start → wait for ready. Keep the first half, replace the pg_ctl start half with LaunchdAgent calls.

```swift
    func start() async throws {
        let fm = FileManager.default
        let pgVersionFile = dataDir.appendingPathComponent("PG_VERSION")
        let expectedMajor = bundledPgMajorVersion()
        var needsInit = false
        if fm.fileExists(atPath: pgVersionFile.path) {
            let stored = (try? String(contentsOf: pgVersionFile, encoding: .utf8))?
                .trimmingCharacters(in: .whitespacesAndNewlines)
            if stored != expectedMajor {
                Log.logger("postgresql").warning(
                    "Data directory version mismatch: found \(stored ?? "?", privacy: .public), expected \(expectedMajor, privacy: .public). Reinitializing."
                )
                try? fm.removeItem(at: dataDir)
                needsInit = true
            }
        } else {
            needsInit = true
        }

        if needsInit {
            try await initializeDatabase()
            try await createDatabaseAndExtensions()
        }

        try ensureLoggingConfig()

        // Stale postmaster.pid removal: same logic as before, but now we
        // don't manually invoke pg_ctl stop — launchctl bootout (inside
        // agent.ensureInstalled) handles the lingering instance.
        let pidFile = dataDir.appendingPathComponent("postmaster.pid")
        if fm.fileExists(atPath: pidFile.path) {
            let action = Self.stalePidAction(pidFileContents: try? String(contentsOf: pidFile))
            switch action {
            case .remove(let pid):
                try? fm.removeItem(at: pidFile)
                Log.logger("postgresql").info("Removed stale postmaster.pid (pid \(pid, privacy: .public))")
            case .removeUnparseable:
                try? fm.removeItem(at: pidFile)
            case .keep:
                Log.logger("postgresql").warning("Existing PostgreSQL process found; bootout will handle it")
                // Don't kill manually — let agent.ensureInstalled's bootout
                // do it cleanly via launchctl.
            }
        }

        // Install/refresh the plist with current bundle paths.
        let plist = LaunchdPlist.postgres(
            bundle: Bundle.main.resourceURL!,
            dataDir: dataDir,
            logsDir: AppSettings.shared.logsDir,
            port: port,
        )
        try await agent.ensureInstalled(plistContent: plist)

        // launchctl kickstart -k starts (or restarts) the service.
        try await agent.start()
    }
```

- [ ] **Step 3: Refactor `stop()`**

The current 38-line three-phase fast→immediate→SIGKILL escalation becomes:

```swift
    func stop() async {
        state = .stopping
        do {
            try await agent.stop()
        } catch {
            Log.logger("postgresql").error(
                "launchctl bootout failed: \(error.localizedDescription, privacy: .public)"
            )
        }
        state = .stopped
    }
```

The escalation logic moves to launchd — `bootout` SIGTERMs the postmaster; if it doesn't exit, launchd escalates via its own logic. The aggressive `pg_ctl stop -m immediate` + `SIGKILL` paths from before are no longer accessible via launchd's bootout. If we need a faster stop for testing or emergency, that's `forceKillEverything` (which now reads `postmaster.pid` and SIGKILLs directly — the existing fallback path).

- [ ] **Step 4: `healthCheck()` stays unchanged**

`pg_isready` invocation against `port` works regardless of who's managing the postmaster.

- [ ] **Step 5: Delete the dead `awaitExitedCleanly` helper**

The static helper used by old `start()`/`stop()`/`healthCheck()` is no longer needed in the new code paths (healthCheck still uses it for the pg_isready exit code — keep that part). Re-check after refactor.

- [ ] **Step 6: Build + run existing tests to confirm no regression**

```bash
xcodebuild ... build 2>&1 | tail -3
xcodebuild ... test 2>&1 | tail -5
```

`OverallStateTests`, `WorkerCountsTests`, etc. don't exercise PostgresService directly, so they should still pass. There's no PostgresService unit test today — the integration test is "run the menubar and watch Postgres come up".

- [ ] **Step 7: Manual smoke test (REQUIRED for this task)**

Build + run the menubar app. Open Console.app and filter to subsystem `com.harborclerk.server`. Verify:

1. On first launch with no existing data dir: initdb runs → createDatabaseAndExtensions runs → plist gets installed in `~/Library/LaunchAgents/com.harborclerk.postgres.plist` → `launchctl bootstrap` runs → `kickstart` runs → `pg_isready` succeeds → state → .running.

2. After menubar quit, postgres process should be killed via launchd bootout. Verify with `ps aux | grep postgres`.

3. Stop menubar app then immediately re-launch. Verify postgres comes back up via launchctl kickstart, NOT via initdb (since data dir exists). Verify plist on disk is unchanged (no bootout-rewrite-bootstrap cycle).

4. Simulate menubar crash: launch menubar app, then `kill -9 <menubar-pid>`. Verify postgres KEEPS RUNNING (launchd survives — this is the win!).

5. Relaunch menubar after crash. Verify it doesn't try to initdb (data dir exists) and doesn't double-bootstrap (plist unchanged).

- [ ] **Step 8: Commit**

```bash
git add macos/HarborClerkServer/HarborClerkServer/Services/PostgresService.swift
git commit -m "feat(menubar): PostgresService delegates lifecycle to LaunchdAgent

start() retains initdb/createDatabaseAndExtensions/ensureLoggingConfig
for first-run setup, then ensures the plist is installed + kickstarts.
stop() is launchctl bootout. healthCheck() unchanged (pg_isready).

User-visible: menubar crash no longer takes Postgres down. Closes the
mid-query data-loss exposure from the model-switch-crash memo.

Spec: docs/superpowers/specs/2026-05-11-menubar-process-mgmt-tier-bc-design.md"
```

---

### Task 5: Refactor TikaService to delegate to LaunchdAgent

**Files:**
- Modify: `macos/HarborClerkServer/HarborClerkServer/Services/TikaService.swift`

Simpler than Postgres — no init dance.

- [ ] **Step 1: Replace the file's body with the LaunchdAgent-delegating version**

```swift
import Foundation

final class TikaService: ManagedService {
    let name = "Tika"
    var state: ServiceState = .stopped
    let isLaunchdManaged = true

    /// Kept for forceKillEverything compatibility — when the launchd
    /// agent is loaded, returns the launchd-tracked pid via
    /// `agent.status()`. The expensive (async) path is fine because
    /// forceKillEverything is only called during emergency shutdown.
    var processIdentifier: Int32? {
        get async {
            if case .loaded(let pid) = await agent.status() { return pid }
            return nil
        }
    }

    private let agent: LaunchdAgent

    init(launchctl: LaunchctlClient = RealLaunchctlClient()) {
        let plistURL = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/LaunchAgents/com.harborclerk.tika.plist")
        self.agent = LaunchdAgent(label: "com.harborclerk.tika", plistURL: plistURL, launchctl: launchctl)
    }

    func start() async throws {
        let plist = LaunchdPlist.tika(
            bundle: Bundle.main.resourceURL!,
            logsDir: AppSettings.shared.logsDir,
            port: AppSettings.shared.tikaPort,
        )
        try await agent.ensureInstalled(plistContent: plist)
        try await agent.start()
    }

    func stop() async {
        state = .stopping
        do {
            try await agent.stop()
        } catch {
            Log.logger("tika").error(
                "launchctl bootout failed: \(error.localizedDescription, privacy: .public)"
            )
        }
        state = .stopped
    }

    func healthCheck() async -> Bool {
        // Unchanged from before: HTTP probe.
        guard let url = URL(string: "http://127.0.0.1:\(AppSettings.shared.tikaPort)/tika") else { return false }
        return await httpProbeOK(url)
    }
}
```

Note the change to `processIdentifier`: it's now `async` because it has to query launchctl. `ServiceManager.forceKillEverything` already iterates services via type-cast — we need to update it to handle the async getter. See Task 7.

- [ ] **Step 2: Build to find call sites that break**

```bash
xcodebuild ... build 2>&1 | tail -10
```

Expected build errors at sites that access `tikaService.processIdentifier` synchronously — most prominently `ServiceManager.savePidFile()` and `ServiceManager.forceKillEverything()`.

- [ ] **Step 3: Update savePidFile and forceKillEverything**

For `savePidFile()`: keep the existing direct property access — but since processIdentifier is now async, we need a synchronous fallback OR we accept that savePidFile is no longer fully accurate for the launchd-managed services. Pragmatic call: skip launchd-managed services in savePidFile (their PIDs are recoverable from launchctl print at need, and orphan-cleanup-from-PID-file is precisely the failure mode launchd avoids).

```swift
    private func savePidFile() {
        var pids: [String] = []
        for service in services {
            // Skip launchd-managed services — launchd tracks their PIDs
            // and they're not orphans-from-our-perspective (a menubar
            // crash leaves them running, and the next launch will
            // re-attach via launchctl).
            if let lm = service as? PostgresService, lm.isLaunchdManaged { continue }
            if let lm = service as? TikaService, lm.isLaunchdManaged { continue }

            if let pySvc = service as? PythonService, let proc = pySvc.process, proc.isRunning {
                pids.append(String(proc.processIdentifier))
            } else if let llama = service as? LlamaService, let pid = llama.processIdentifier {
                pids.append(String(pid))
            }
        }
        let content = pids.joined(separator: "\n")
        try? content.write(to: Self.pidFileURL, atomically: true, encoding: .utf8)
    }
```

For `forceKillEverything()`: the existing "read postmaster.pid and SIGKILL" branch still works for Postgres (and is now belt-and-suspenders alongside launchd's own cleanup). Tika needs a similar pgid-lookup path — since launchd-managed processes have whatever pgid launchd assigned, the launchctl-tracked PID is the only handle we have. Tika's case becomes async:

```swift
    func forceKillEverything() async {  // <-- now async
        let logger = Log.logger("lifecycle")
        var killed: Set<Int32> = []

        for service in services {
            // launchd-managed services: query launchctl for pid, then SIGKILL.
            if let tika = service as? TikaService, tika.isLaunchdManaged {
                if let pid = await tika.processIdentifier, pid > 0 {
                    if killpg(pid, SIGKILL) != 0 { kill(pid, SIGKILL) }
                    killed.insert(pid)
                }
                continue
            }
            if let postgres = service as? PostgresService, postgres.isLaunchdManaged {
                // Use postmaster.pid (same as pre-launchd path).
                let pgPidFile = AppSettings.shared.postgresDataDir.appendingPathComponent("postmaster.pid")
                if let contents = try? String(contentsOf: pgPidFile, encoding: .utf8),
                   let pidLine = contents.components(separatedBy: "\n").first,
                   let pid = Int32(pidLine), pid > 0 {
                    if killpg(pid, SIGKILL) != 0 { kill(pid, SIGKILL) }
                    killed.insert(pid)
                }
                continue
            }
            // Non-launchd menubar children — same as before (post-PR-2).
            if let pySvc = service as? PythonService, let proc = pySvc.process {
                let pid = proc.processIdentifier
                if pid > 0 {
                    if killpg(pid, SIGKILL) != 0 { kill(pid, SIGKILL) }
                    killed.insert(pid)
                }
            } else if let llama = service as? LlamaService, let pid = llama.processIdentifier {
                if killpg(pid, SIGKILL) != 0 { kill(pid, SIGKILL) }
                killed.insert(pid)
            }
        }

        // child-pids.txt fallback unchanged
        if let contents = try? String(contentsOf: Self.pidFileURL, encoding: .utf8) {
            for line in contents.components(separatedBy: "\n") {
                let trimmed = line.trimmingCharacters(in: .whitespaces)
                guard let pid = Int32(trimmed), pid > 0, !killed.contains(pid) else { continue }
                guard kill(pid, 0) == 0 else { continue }
                if killpg(pid, SIGKILL) != 0 { kill(pid, SIGKILL) }
                killed.insert(pid)
            }
        }

        logger.warning("forceKillEverything sent SIGKILL to \(killed.count, privacy: .public) tracked PIDs")
        removePidFile()
    }
```

Note `forceKillEverything()` is now `async`. Every caller becomes `await self.serviceManager.forceKillEverything()` — search the codebase.

- [ ] **Step 4: Build + test**

```bash
xcodebuild ... build 2>&1 | tail -3
xcodebuild ... test 2>&1 | tail -5
```

Expected: clean build, all tests pass.

- [ ] **Step 5: Manual smoke test**

Same as Postgres in Task 4 but for Tika. Verify the Tika launchd-agent gets installed, kickstarted, killed properly. Confirm child JVMs die when bootout runs (launchd kills the whole tree).

- [ ] **Step 6: Commit**

```bash
git add macos/HarborClerkServer/HarborClerkServer/Services/TikaService.swift \
        macos/HarborClerkServer/HarborClerkServer/ServiceManager.swift
git commit -m "feat(menubar): TikaService delegates to LaunchdAgent

Same shape as Postgres: ensureInstalled + kickstart on start, bootout
on stop. forceKillEverything is now async because launchd-managed
processIdentifier requires a launchctl print query.

Bonus: launchd's process-tree management cleans up Tika's child JVMs
correctly on bootout — closes the JVM-leak hole the audit identified
without needing the process-group code from PR-2 for Tika
specifically.

Spec: docs/superpowers/specs/2026-05-11-menubar-process-mgmt-tier-bc-design.md"
```

---

### Task 6: HealthChecker skips auto-restart for launchd-managed services

**Files:**
- Modify: `macos/HarborClerkServer/HarborClerkServer/HealthChecker.swift`
- Test: `macos/HarborClerkServer/HarborClerkServerTests/HealthCheckerTests.swift`

Auto-restart from the menubar AND launchd's KeepAlive will double-trigger after a crash. Launchd's restart is enough; the menubar should skip.

- [ ] **Step 1: Write a failing test**

```swift
func testCheckAllSkipsAutoRestartForLaunchdManagedService() async throws {
    let mockServices = MockServiceManager()
    let svc = MockServiceWithFailingHealth(name: "test-launchd-svc")
    svc.isLaunchdManaged = true   // <-- NEW: add this flag to ManagedService
    mockServices.services = [svc]
    let hc = HealthChecker(serviceManager: mockServices)

    svc.state = .running
    for _ in 0..<6 { await hc.tickForTesting() }

    XCTAssertEqual(hc.inFlightTaskCount, 0, "must not dispatch auto-restart Task for launchd-managed service")
    XCTAssertTrue(mockServices.attemptAutoRestartCalled.isEmpty, "must not call attemptAutoRestart")
}
```

- [ ] **Step 2: Add `isLaunchdManaged` to `ManagedService` protocol**

```swift
protocol ManagedService: AnyObject {
    var name: String { get }
    var state: ServiceState { get set }
    var isLaunchdManaged: Bool { get }  // <-- NEW
    func start() async throws
    func stop() async
    func healthCheck() async -> Bool
}

// Default implementation for services that aren't launchd-managed:
extension ManagedService {
    var isLaunchdManaged: Bool { false }
}
```

PostgresService and TikaService have already declared `let isLaunchdManaged = true` (from Tasks 4-5); the protocol now requires it formally with a default of `false` for everyone else.

- [ ] **Step 3: Modify `HealthChecker.checkAll` to skip launchd-managed services from auto-restart**

In `checkAll`, locate the `if count >= self.consecutiveFailuresBeforeError` block (added in PR-1). Add a guard:

```swift
if count >= self.consecutiveFailuresBeforeError {
    service.state = .errored
    failureCounts[service.name] = nil
    changed = true
    Log.logger("health").error(...)

    // Skip auto-restart for launchd-managed services — launchd's
    // KeepAlive handles their crash recovery and we'd double-restart
    // otherwise. The menubar's role for these is just status display.
    guard !service.isLaunchdManaged else { continue }

    let id = UUID()
    let svc = service
    let sm = serviceManager
    taskHandles[id] = Task { @MainActor in
        defer { Task { @MainActor in self.taskHandles[id] = nil } }
        await sm.attemptAutoRestart(svc)
    }
}
```

- [ ] **Step 4: Run the test to confirm it passes**

```bash
xcodebuild ... -only-testing:HarborClerkServerTests/HealthCheckerTests/testCheckAllSkipsAutoRestartForLaunchdManagedService test 2>&1 | tail -10
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add macos/HarborClerkServer/HarborClerkServer/HealthChecker.swift \
        macos/HarborClerkServer/HarborClerkServer/ServiceManager.swift \
        macos/HarborClerkServer/HarborClerkServerTests/HealthCheckerTests.swift
git commit -m "feat(menubar): HealthChecker skips auto-restart for launchd-managed

Launchd's KeepAlive={SuccessfulExit:false} restarts Postgres/Tika on
crash; the menubar's HealthChecker should not also dispatch
attemptAutoRestart for them. The new ManagedService.isLaunchdManaged
flag gates the skip.

Spec: docs/superpowers/specs/2026-05-11-menubar-process-mgmt-tier-bc-design.md"
```

---

### Task 7: AppDelegate's Force Stop All also stops launchd agents

**Files:**
- Modify: `macos/HarborClerkServer/HarborClerkServer/AppDelegate.swift`

Picks up PR-3's TODO comment.

- [ ] **Step 1: Update `forceStopAllServices` to await postgres + tika stops**

Locate the `forceStopAllServices` action handler (added in PR-3). Inside the Task:

```swift
Task {
    self.serviceManager.forceKillEverything()  // becomes async after Task 5

    // launchd-managed agents — let launchctl bootout do the right thing
    // for them rather than just SIGKILLing the postmaster (which would
    // trigger launchd's KeepAlive auto-restart).
    await self.serviceManager.postgresService.stop()
    await self.serviceManager.tikaService.stop()

    self.serviceManager.notifyStateChanged()
}
```

Update `applicationShouldTerminate` similarly — its deadline path calls `forceKillEverything()` synchronously today (from PR #341). After Task 5 it's async, so:

```swift
let deadlineTask: Task<Void, Never> = Task {
    try? await Task.sleep(for: .seconds(deadlineSeconds))
    Log.logger("lifecycle").error(...)
    await self.serviceManager.forceKillEverything()  // <-- await now needed
    // launchd agents too
    await self.serviceManager.postgresService.stop()
    await self.serviceManager.tikaService.stop()
    exit(0)
}
```

- [ ] **Step 2: Build + test + smoke**

```bash
xcodebuild ... build 2>&1 | tail -3
xcodebuild ... test 2>&1 | tail -5
```

Manual smoke test: trigger Force Stop All with menubar running, verify both menubar children AND launchd agents die. Verify launchd doesn't auto-restart them after (it shouldn't — bootout is "intentional stop").

- [ ] **Step 3: Commit**

```bash
git add macos/HarborClerkServer/HarborClerkServer/AppDelegate.swift
git commit -m "feat(menubar): Force Stop All + 30s deadline stop launchd agents

Both paths now bootout postgres + tika in addition to SIGKILLing the
menubar children. Without this, Force Stop All would SIGKILL the
postmaster — launchd would auto-restart it via KeepAlive — and the
user's 'stop everything' click would be meaningless.

Picks up the TODO from PR-3's forceStopAllServices.

Spec: docs/superpowers/specs/2026-05-11-menubar-process-mgmt-tier-bc-design.md"
```

---

### Task 8: Full smoke test matrix + fresh-eyes review + PR

- [ ] **Step 1: Run the full automated test suite**

```bash
xcodebuild ... test 2>&1 | tail -5
```

Test count: ~95 (63 baseline + 3 PR-1 + 4 PR-2 + 1 PR-3 + 24 new in PR-4 across LaunchctlClientTests/LaunchdPlistTests/LaunchdAgentTests/HealthCheckerTests).

- [ ] **Step 2: Manual smoke test matrix**

Build a release-style build (`xcodebuild ... build`), then run through every row:

| Scenario | Expected |
|---|---|
| First-run on a Mac with no `~/Library/LaunchAgents/com.harborclerk.*.plist` | Plists get written, bootstrapped, kickstarted; postgres + tika come up |
| Second launch (plist already exists, content matches) | No bootout-rewrite-bootstrap; kickstart only; postgres + tika come up |
| Bundle moved (e.g. /Applications → ~/Applications between launches) | Plist content differs from disk; bootout-rewrite-bootstrap fires; postgres + tika come up |
| Menubar killed via `kill -9 <pid>` while postgres + tika running | Both keep running. Re-launch menubar; it sees them already running (via `agent.status()`) and adopts them |
| Postgres crash (simulate via `kill -9 $(cat postmaster.pid)`) | launchd's KeepAlive restarts it within ~1s. HealthChecker does NOT also try (isLaunchdManaged skip) |
| Tika crash (simulate similarly) | Same |
| User clicks Quit menu | stopAll runs; postgres + tika stop via bootout; menubar quits cleanly within 30s |
| User clicks Force Stop All | Confirmation dialog; on confirm, both menubar children and launchd agents die; menubar stays open |
| Plist corrupted on disk (manually edit to garbage) | bootout-rewrite-bootstrap on next start — self-heals |
| launchctl bootstrap fails (simulate via permission change on plist) | Clear error logged; PostgresService.state = .errored |

- [ ] **Step 3: Push + fresh-eyes review per the standing directive**

This PR is multi-component (3 new files, 4 modified, async refactor) and has non-trivial failure paths (launchctl semantics, plist drift, double-restart). Per the standing directive in `MEMORY.md`, dispatch a **minimal-prompt** fresh-eyes review:

```bash
git push -u origin fix/menubar-launchd-postgres-tika
```

Dispatch `feature-dev:code-reviewer` with: "Review the changes on branch `fix/menubar-launchd-postgres-tika` (tip <SHA>) against `origin/main`. Repo at /Users/alex/mcp-gateway. Report findings with confidence scores. Address ≥80 confidence before merge."

Address all ≥80 findings, push fixes, re-review until clean.

- [ ] **Step 4: Open PR**

```bash
gh pr create --title "feat(menubar): launchd migration for Postgres + Tika (PR-4 of 4)" --body-file <tmp>
```

PR body references:
- Spec: `docs/superpowers/specs/2026-05-11-menubar-process-mgmt-tier-bc-design.md`
- Audit memo: `project_menubar_process_management_audit.md`
- Sibling PRs (PR-1, PR-2, PR-3)
- The smoke test matrix executed manually
- Deferred items per `pr_followups.md` ("Tier C follow-on: uninstall.sh")

- [ ] **Step 5: Watch CI, merge**

```bash
gh pr merge <N> --squash --delete-branch
```

After merge, update `pr_followups.md` with anything that came up during smoke testing that should be tracked.

---

## Self-review checklist

- **Spec coverage:** Every section of the spec maps to a task here:
  - "Postgres + Tika move to launchd agents" → Tasks 4-5
  - "Detailed design / LaunchdAgent class" → Task 3
  - "Detailed design / Plist contents" → Task 2
  - "Detailed design / PostgresService / TikaService refactor" → Tasks 4-5
  - "Open implementation questions / Auto-restart-on-crash interaction" → Task 6
  ✓
- **Type consistency:** `LaunchctlClient` / `LaunchdAgent` / `LaunchdPlist` consistent. `isLaunchdManaged` on `ManagedService` consistent. `forceKillEverything` becomes async — Task 5 updates callers + Task 7 catches the AppDelegate sites. ✓
- **No placeholders:** All file contents shown in full. All commands have expected output. ✓

## Out of scope

- "Uninstall Services" menu item / uninstall.sh — captured in `pr_followups.md`
- Embedder / llama-server / API / workers to launchd — captured in `pr_followups.md`
- `SMAppService.agent` migration — captured in spec's "Out of scope"

## Notes for the executor

This PR-4 is the largest of the four sub-PRs and the only one where manual smoke testing is **required** (not just recommended). The unit tests cover the protocol-level lifecycle logic but cannot validate that real `launchctl` does what we expect. Reserve time for the matrix in Task 8 Step 2; expect to discover at least one launchctl-semantics surprise during smoke testing that requires a small fix.

If `Process.runAsProcessGroupLeader` (from PR-2) proves unreliable for Tika specifically and the launchd migration here turns out to be fragile too, the fallback path is the `SpawnedProcess` class documented in the spec's "Detailed design / Process.runAsProcessGroupLeader" section. That fallback would replace Task 3's LaunchdAgent for Tika only, keeping Postgres on launchd. Don't preemptively switch — only if smoke testing reveals problems we can't otherwise fix.

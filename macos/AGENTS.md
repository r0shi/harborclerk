# AGENTS.md — `macos`

Two Swift apps: **Harbor Clerk Server** (menubar agent managing every backend
service) and **Harbor Clerk** (WKWebView shell around the SPA). Build scripts in
`scripts/`, orchestrated by `Makefile`.

## Process management — the rules that cost real outages

- **Never call `proc.waitUntilExit()` on the MainActor.** It blocks, and every
  other service stalls in `startupPending` behind it. Use
  `await withCheckedContinuation` instead — the pattern `MigrationRunner`
  already uses.
- **Nil the `readabilityHandler` on EOF.** `Log.createPipe()` installs a
  readability handler; if it is never cleared, `waitUntilExit()` deadlocks
  because the pipe's read end never closes. Set
  `handle.readabilityHandler = nil` inside the `guard !data.isEmpty` else
  branch. This presented as Alembic finishing while the runner hung forever;
  `sample <pid>` showed the thread parked in `mach_msg2_trap`.
- **`@MainActor` async functions still interleave.** They can suspend at any
  `await`, so state read before an await may be stale after it. Guard every
  state transition against concurrent mutation — `startService()` must re-check
  `.shutdownPending` rather than trusting an earlier check.
- **Kill process groups, not PIDs.** Tika forks JVMs and workers spawn
  `pdftoppm`, `tesseract`, `magick`. `kill(pid, SIGKILL)` orphans the
  grandchildren to launchd, where they survive relaunch. Use `killpg`.

`ServiceState` has seven states: `stopped`, `startupPending`, `starting`,
`running`, `shutdownPending`, `stopping`, `errored`. Pending states render with
blue dots.

Postgres and Tika may be **launchd-managed** rather than direct subprocesses, so
they survive a menubar crash.

## Environment for subprocesses

`ServiceManager` builds one shared environment dict handed to every subprocess,
which is why macOS gets variables like `LLAMA_SERVER_URL` for free. Docker
Compose has no equivalent — each service opts in individually, and forgetting
one there caused summarize to silently produce extractive summaries. When adding
a variable a worker needs, check whether the Compose side needs it too.

## Networking

The WKWebView app loads the SPA over **plain HTTP on loopback** at `api_port`
(default **8100**). The HTTPS gateway is a *separate* Caddy surface on **8443**
for local MCP clients requiring TLS, with `internal` or operator-supplied
certificates and a loopback-versus-LAN exposure check. These are different
surfaces; do not conflate them.

Port 80 is frequently occupied on macOS (AirPlay Receiver and friends) and is
not needed for local development.

The client `Info.plist` declares **no ATS keys**, so the loopback cleartext load
depends on WebKit's *implicit* localhost allowance rather than a declared
exception. That is a known gap, not a decision — see #561.

## Do not bump the SDK without re-testing

Many of macOS's stricter privacy and security defaults are gated
**"linked-on-or-after"** the SDK a binary was built against. Rebuilding against a
newer SDK is what *flips the stricter rules on* — ATS, TCC/file access,
hardened-runtime and JIT policy can all change behavior with no source change at
all. So an SDK bump is a behavioral change, not housekeeping.

**The bump has already happened.** This section used to say the build "links the
macOS 15.x SDK" and is "grandfathered into the older, looser behavior". That is
no longer true, and nothing recorded the change: the deployment target is still
15.0, but the SDK follows whatever Xcode is on the build machine, so installing
Xcode 26 silently moved it. Measured on the shipped bundles:

```
$ vtool -show-build /Applications/HarborClerkServer.app/Contents/MacOS/HarborClerkServer
 platform MACOS
    minos 15.0
      sdk 26.5
```

So the stricter rules are **already live in every build since that Xcode
upgrade**, and have been through a full field test — an 11,436-document corpus,
IMAP sync, search and Research — with no observed breakage. The `#561` ATS gap
(no ATS keys declared, loopback cleartext relying on WebKit's implicit localhost
allowance) is therefore running *under* the stricter regime today, not waiting
for it.

Two consequences worth keeping in mind:

- **The risk is inverted from how it reads.** Building on a machine with an older
  Xcode now *loosens* the shipped binary rather than being the safe default. The
  CI `macos` job runs on `macos-15`, whose SDK is older than the release
  machine's, so CI validates a **more permissive** configuration than ships — it
  cannot catch a regression that only the stricter SDK triggers.
- **"Do not bump without re-testing" needs a way to notice a bump.** It was
  bypassed here not by anyone deciding to bump, but by a routine Xcode upgrade.
  `vtool -show-build` on the built binary is the check; pinning Xcode in the job
  and at release time is the fix, and is not yet done.

When bumping, re-test on the target OS: the WKWebView loopback load, watched-
folder access from the background watcher, and every spawned helper (Postgres,
the Tika JVM, llama-server, bundled Python). See the macOS 27 readiness
assessment in `docs/superpowers/plans/` and epic #563.

## No native dialogs in the web view

`window.confirm` and `window.alert` silently return false in WKWebView. Swift
retains only two JS bridges — `pickFolder` (NSOpenPanel) and `revealInFinder`
(NSWorkspace). Folder management UI lives at `/folders` in the web app; resist
adding bridges for things the web app can do.

## Naming

Bundle IDs, keychain service IDs, access-group suffixes, and Logger subsystems
all use **`com.harborclerk.*`** — never `com.bitblot.*`.

## Worker presets (C = logical cores)

| Preset | io | cpu | llm |
|---|---|---|---|
| Quiet | 1 | 1 | 1 |
| Balanced | max(2, C//4) | 2 | 1 |
| Fast | max(2, C//2) | 3 | 1 |

Hard caps: io ≤ 8, cpu ≤ 4. The `llm` queue runs a single worker in every
preset. These are defined in `ServiceManager.workerCounts`.

## Builds

`build-venv.sh` uses `pip install --upgrade` for the local packages
(`harbor-clerk`, `embedder`). An older import-guard shortcut skipped reinstalls
even when the Python source had changed, producing a stale venv inside a
freshly-built app.

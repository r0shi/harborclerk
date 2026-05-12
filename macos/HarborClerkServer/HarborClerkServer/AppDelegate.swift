import AppKit
import SwiftUI

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    private var statusItem: NSStatusItem!
    private var serviceManager: ServiceManager!
    private var statusWindowController: NSWindowController?
    private var preferencesWindowController: NSWindowController?
    private var healthChecker: HealthChecker!
    private var menuBarIcon: NSImage?
    private var restartTask: Task<Void, Never>?
    private var isQuitting = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        // First-launch master key bootstrap. Idempotent — generates the key in
        // Keychain only on the very first launch; subsequent launches just read.
        // Must run before ServiceManager.startAll() because the Python subprocesses
        // need this key in their environment (Task 10 injects it).
        _ = MasterKeyManager.production.loadOrGenerate()

        serviceManager = ServiceManager()
        healthChecker = HealthChecker(serviceManager: serviceManager)
        serviceManager.healthChecker = healthChecker

        setupStatusItem()

        // Auto-start services on launch
        Task {
            await serviceManager.startAll()
            healthChecker.startPolling()
        }

        // Observe state changes to update icon
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(servicesStateChanged),
            name: .servicesStateChanged,
            object: nil,
        )

        // Observe preferences restart request
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(handlePreferencesRestart),
            name: .preferencesRequestRestart,
            object: nil,
        )
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard !isQuitting else { return .terminateNow }
        isQuitting = true
        healthChecker.stopPolling()

        // 30-second hard deadline. The audit memo
        // (project_menubar_process_management_audit.md) traced multiple
        // ways the shutdown path can hang indefinitely — workers stuck on
        // SIGTERM, Pipe+waitUntilExit deadlock, etc. Tier A fixes
        // (this PR) make the hangs much less likely, but the deadline is
        // the absolute guarantee: if stopAll() hasn't completed in 30s,
        // SIGKILL everything we know about and exit. Better an unclean
        // shutdown than a menubar app the user can't quit without
        // Terminal — which is exactly the failure mode the user
        // reported.
        let deadlineSeconds: UInt64 = 30
        let deadlineTask: Task<Void, Never> = Task {
            try? await Task.sleep(for: .seconds(deadlineSeconds))
            // If we reach here, Task.sleep ran to completion — the
            // shutdown task hasn't called deadlineTask.cancel() yet, so
            // stopAll() is still running. Force-kill and exit hard.
            Log.logger("lifecycle").error(
                "Shutdown exceeded \(deadlineSeconds, privacy: .public)s deadline — force-killing tracked children and exiting"
            )
            await self.serviceManager.forceKillEverything()
            // launchd-managed agents — bootout so KeepAlive doesn't
            // resurrect the postmaster / Tika JVM after our SIGKILL.
            await self.serviceManager.postgresService.stop()
            await self.serviceManager.tikaService.stop()
            // exit(0) bypasses any further AppKit teardown. We've signalled
            // every child we know about; the kernel will reparent any
            // survivors to launchd. Reaching this line means stopAll() was
            // wedged anyway — there's no graceful path forward.
            exit(0)
        }

        Task {
            await self.serviceManager.stopAll()
            // stopAll() completed cleanly — cancel the deadline before it
            // fires, then signal AppKit that termination can proceed.
            deadlineTask.cancel()
            NSApp.reply(toApplicationShouldTerminate: true)
        }
        return .terminateLater
    }

    // MARK: - Status Item

    private func setupStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        updateStatusIcon(.starting)
        setupMenu()
    }

    private func setupMenu() {
        let menu = NSMenu()

        menu.addItem(NSMenuItem(title: "Harbor Clerk Server", action: nil, keyEquivalent: ""))
        menu.addItem(NSMenuItem.separator())

        // Service status rows
        let servicesItem = NSMenuItem(title: "Services: Starting...", action: nil, keyEquivalent: "")
        servicesItem.tag = 100
        menu.addItem(servicesItem)

        menu.addItem(NSMenuItem.separator())

        let openItem = NSMenuItem(title: "Open Harbor Clerk", action: #selector(openFrontendApp), keyEquivalent: "o")
        openItem.target = self
        menu.addItem(openItem)

        menu.addItem(NSMenuItem.separator())

        let startItem = NSMenuItem(title: "Start All", action: #selector(startAllServices), keyEquivalent: "")
        startItem.target = self
        menu.addItem(startItem)

        let stopItem = NSMenuItem(title: "Stop All", action: #selector(stopAllServices), keyEquivalent: "")
        stopItem.target = self
        menu.addItem(stopItem)

        let forceStopItem = NSMenuItem(title: "Force Stop All", action: #selector(forceStopAllServices), keyEquivalent: "")
        forceStopItem.target = self
        menu.addItem(forceStopItem)

        menu.addItem(NSMenuItem.separator())

        let statusWindowItem = NSMenuItem(title: "Show Status Window...", action: #selector(showStatusWindow), keyEquivalent: "s")
        statusWindowItem.target = self
        menu.addItem(statusWindowItem)

        let consoleItem = NSMenuItem(title: "View Logs in Console...", action: #selector(openConsole), keyEquivalent: "l")
        consoleItem.target = self
        menu.addItem(consoleItem)

        let preferencesItem = NSMenuItem(title: "Preferences...", action: #selector(showPreferences), keyEquivalent: ",")
        preferencesItem.target = self
        menu.addItem(preferencesItem)

        menu.addItem(NSMenuItem.separator())

        let quitItem = NSMenuItem(title: "Quit", action: #selector(quitApp), keyEquivalent: "q")
        quitItem.target = self
        menu.addItem(quitItem)

        statusItem.menu = menu
    }

    @objc private func servicesStateChanged() {
        let state = serviceManager.overallState
        updateStatusIcon(state)
        updateServiceStatusMenuItem()
    }

    private func updateStatusIcon(_ state: ServiceState) {
        guard let button = statusItem.button else { return }
        if menuBarIcon == nil {
            if let img = NSImage(named: "MenuBarIcon") {
                img.isTemplate = true
                img.size = NSSize(width: 18, height: 18)
                menuBarIcon = img
            } else if let img = NSImage(systemSymbolName: "doc.text.magnifyingglass", accessibilityDescription: "Harbor Clerk") {
                img.isTemplate = true
                menuBarIcon = img
            }
        }
        if let icon = menuBarIcon {
            button.image = icon
        }
        switch state {
        case .stopped:
            button.appearsDisabled = true
        case .startupPending, .starting, .shutdownPending, .stopping:
            button.appearsDisabled = false
        case .running:
            button.appearsDisabled = false
        case .errored:
            button.appearsDisabled = false
        }
    }

    private func updateServiceStatusMenuItem() {
        guard let menu = statusItem.menu,
              let item = menu.item(withTag: 100) else { return }

        let counts = serviceManager.services.reduce(into: [ServiceState: Int]()) { result, svc in
            result[svc.state, default: 0] += 1
        }
        let total = serviceManager.services.count
        let running = counts[.running, default: 0]
        item.title = "Services: \(running)/\(total) running"
    }

    // MARK: - Actions

    @objc private func openFrontendApp() {
        let bundleID = "com.harborclerk.HarborClerk"
        if let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: bundleID) {
            NSWorkspace.shared.openApplication(at: url, configuration: .init())
        } else {
            let port = AppSettings.shared.apiPort
            if let url = URL(string: "http://localhost:\(port)") {
                NSWorkspace.shared.open(url)
            }
        }
    }

    @objc private func startAllServices() {
        Task { await serviceManager.startAll() }
    }

    @objc private func stopAllServices() {
        Task { await serviceManager.stopAll() }
    }

    /// Manual escape hatch for users who'd rather not wait the 30s deadline
    /// in applicationShouldTerminate. SIGKILLs every tracked subprocess and
    /// leaves the menubar open so the user can re-Start All when ready.
    /// Destructive operation, gated by an NSAlert confirmation.
    @objc private func forceStopAllServices() {
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

        Task { @MainActor in
            // Prevent auto-restart of menubar-spawned children — without
            // this, the SIGKILLs below trigger each PythonService's
            // terminationHandler restart loop, plus the
            // onUnexpectedExit -> attemptAutoRestart path on max-restart
            // exhaustion. Smoke testing PR-4 step 7 showed Embedder,
            // LLM, API, and Watcher coming back to Running after Force
            // Stop because of this.
            //
            // Two-pronged: (1) set state=.stopping on every service
            // *before* SIGKILL — PythonService/LlamaService
            // terminationHandlers guard on `state == .running` and
            // become no-ops; (2) flip isShuttingDown so any leak-through
            // auto-restart Task early-returns in attemptAutoRestart.
            // Same approach as stopAll() but without the orderly
            // shutdown sequence.
            self.serviceManager.isShuttingDown = true
            await self.healthChecker.cancelInFlightRestarts()
            for service in self.serviceManager.services where service.state != .stopped {
                service.state = .stopping
            }
            self.serviceManager.notifyStateChanged()

            // forceKillEverything sends SIGKILL to every tracked subprocess.
            // After PR-4 it's async because Tika's PID is queried via
            // `launchctl print`. The menubar stays open afterward, and the
            // user's next Start All click lands on a freshly-empty state
            // because removePidFile happens during the kill pass.
            await self.serviceManager.forceKillEverything()

            // launchd-managed agents — let launchctl bootout do the right
            // thing for them rather than relying on just the SIGKILL above.
            // Without bootout, launchd's KeepAlive would observe the
            // unexpected exit and auto-restart the postmaster / Tika JVM,
            // defeating the user's "stop everything" intent.
            await self.serviceManager.postgresService.stop()
            await self.serviceManager.tikaService.stop()

            // Mark every service as stopped — the UI was in an
            // intermediate .stopping state, and the SIGKILL'd processes
            // can't transition themselves. Workers, embedder, llm, api,
            // watcher all get nilled here so a subsequent Start All
            // lands cleanly. (forceKillEverything itself doesn't touch
            // .state — keeping it that way means it's safe to call
            // from anywhere, e.g. the 30s deadline path.)
            for service in self.serviceManager.services {
                service.state = .stopped
                if let pySvc = service as? PythonService { pySvc.process = nil }
            }
            self.serviceManager.isShuttingDown = false
            self.serviceManager.notifyStateChanged()
        }
    }

    @objc private func showStatusWindow() {
        if statusWindowController == nil {
            let view = StatusWindow(serviceManager: serviceManager)
            let hostingController = NSHostingController(rootView: view)
            let window = NSWindow(contentViewController: hostingController)
            window.title = "Harbor Clerk Server"
            window.styleMask = [.titled, .closable, .resizable, .miniaturizable, .fullSizeContentView]
            configureGlassWindow(window, size: NSSize(width: 600, height: 500))
            statusWindowController = NSWindowController(window: window)
        }
        statusWindowController?.showWindow(nil)
        statusWindowController?.window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc private func openConsole() {
        if let consoleURL = NSWorkspace.shared.urlForApplication(
            withBundleIdentifier: "com.apple.Console"
        ) {
            NSWorkspace.shared.openApplication(at: consoleURL, configuration: .init())
        }
    }

    @objc private func showPreferences() {
        // If the window is already on screen, just bring it forward — don't
        // disturb anything the user might be editing.
        if let controller = preferencesWindowController, controller.window?.isVisible == true {
            controller.window?.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            return
        }

        // Otherwise, build a fresh window. PreferencesWindow's @State props
        // are initialized from AppSettings.shared at view-struct creation
        // time — if we reused an existing controller after the web app (or
        // anything else) wrote to config.json, the picker would still show
        // the previous selection. AppSettings.reload() pulls in the latest
        // file contents so the freshly-built View captures them.
        AppSettings.shared.reload()
        let view = PreferencesWindow()
        let hostingController = NSHostingController(rootView: view)
        let window = NSWindow(contentViewController: hostingController)
        window.title = "Preferences"
        window.styleMask = [.titled, .closable, .fullSizeContentView]
        configureGlassWindow(window, size: NSSize(width: 480, height: 590))
        preferencesWindowController = NSWindowController(window: window)
        preferencesWindowController?.showWindow(nil)
        preferencesWindowController?.window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    // MARK: - Glass Window Configuration

    private func configureGlassWindow(_ window: NSWindow, size: NSSize) {
        window.titlebarAppearsTransparent = true
        window.titleVisibility = .hidden
        window.toolbarStyle = .unified
        window.setContentSize(size)
        window.center()
    }

    @objc private func handlePreferencesRestart(_ notification: Notification) {
        // Cancel any in-flight restart to prevent duplicate concurrent restarts
        restartTask?.cancel()
        let changedKeys = notification.userInfo?["changedKeys"] as? Set<String> ?? []
        restartTask = Task {
            // Pause health checker to prevent it from marking restarting services as errored
            healthChecker.paused = true
            if changedKeys.isEmpty {
                await serviceManager.stopAll()
                await serviceManager.startAll()
            } else {
                await serviceManager.restartForChangedSettings(changedKeys)
            }
            // Only unpause if this task wasn't superseded by a newer restart
            guard !Task.isCancelled else { return }
            healthChecker.paused = false
        }
    }

    @objc private func quitApp() {
        // Don't stop services here — applicationWillTerminate handles shutdown.
        // Calling stopAll() in an async Task would block the MainActor and prevent
        // NSApp.terminate from processing properly.
        NSApp.terminate(nil)
    }
}

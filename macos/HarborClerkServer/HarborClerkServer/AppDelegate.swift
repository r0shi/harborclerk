import AppKit
import SwiftUI

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    private var statusItem: NSStatusItem!
    private var serviceManager: ServiceManager!
    private var statusWindowController: NSWindowController?
    private var preferencesWindowController: NSWindowController?
    private var healthChecker: HealthChecker!

    func applicationDidFinishLaunching(_ notification: Notification) {
        serviceManager = ServiceManager()
        healthChecker = HealthChecker(serviceManager: serviceManager)

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

    func applicationWillTerminate(_ notification: Notification) {
        healthChecker.stopPolling()
        serviceManager.stopAll()
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

        menu.addItem(NSMenuItem.separator())

        let statusWindowItem = NSMenuItem(title: "Show Status Window...", action: #selector(showStatusWindow), keyEquivalent: "s")
        statusWindowItem.target = self
        menu.addItem(statusWindowItem)

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
        let symbolName: String
        let tint: NSColor
        switch state {
        case .stopped:
            symbolName = "circle.fill"
            tint = .systemGray
        case .starting, .stopping:
            symbolName = "circle.fill"
            tint = .systemYellow
        case .running:
            symbolName = "circle.fill"
            tint = .systemGreen
        case .errored:
            symbolName = "exclamationmark.circle.fill"
            tint = .systemRed
        }
        if let image = NSImage(systemSymbolName: symbolName, accessibilityDescription: "Server status") {
            let config = NSImage.SymbolConfiguration(pointSize: 14, weight: .regular)
            let configured = image.withSymbolConfiguration(config) ?? image
            button.image = configured
            button.contentTintColor = tint
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
        serviceManager.stopAll()
    }

    @objc private func showStatusWindow() {
        if statusWindowController == nil {
            let view = StatusWindow(serviceManager: serviceManager)
            let hostingController = NSHostingController(rootView: view)
            let window = NSWindow(contentViewController: hostingController)
            window.title = "Harbor Clerk Server"
            window.setContentSize(NSSize(width: 600, height: 450))
            window.styleMask = [.titled, .closable, .resizable, .miniaturizable]
            statusWindowController = NSWindowController(window: window)
        }
        statusWindowController?.showWindow(nil)
        statusWindowController?.window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc private func showPreferences() {
        if preferencesWindowController == nil {
            let view = PreferencesWindow()
            let hostingController = NSHostingController(rootView: view)
            let window = NSWindow(contentViewController: hostingController)
            window.title = "Preferences"
            window.styleMask = [.titled, .closable]
            window.center()
            preferencesWindowController = NSWindowController(window: window)
        }
        preferencesWindowController?.showWindow(nil)
        preferencesWindowController?.window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc private func handlePreferencesRestart() {
        serviceManager.stopAll()
        Task { await serviceManager.startAll() }
    }

    @objc private func quitApp() {
        serviceManager.stopAll()
        NSApp.terminate(nil)
    }
}

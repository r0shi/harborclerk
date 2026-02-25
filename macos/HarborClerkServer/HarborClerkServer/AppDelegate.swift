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
        if menuBarIcon == nil, let url = Bundle.main.url(forResource: "menubar_icon", withExtension: "png") {
            if let img = NSImage(contentsOf: url) {
                img.isTemplate = true
                img.size = NSSize(width: 18, height: 18)
                menuBarIcon = img
            }
        }
        if let icon = menuBarIcon {
            button.image = icon
        }
        switch state {
        case .stopped:
            button.appearsDisabled = true
        case .starting, .stopping:
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
        serviceManager.stopAll()
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

    @objc private func showPreferences() {
        if preferencesWindowController == nil {
            let view = PreferencesWindow()
            let hostingController = NSHostingController(rootView: view)
            let window = NSWindow(contentViewController: hostingController)
            window.title = "Preferences"
            window.styleMask = [.titled, .closable, .fullSizeContentView]
            configureGlassWindow(window, size: NSSize(width: 480, height: 590))
            preferencesWindowController = NSWindowController(window: window)
        }
        preferencesWindowController?.showWindow(nil)
        preferencesWindowController?.window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    // MARK: - Glass Window Configuration

    private func configureGlassWindow(_ window: NSWindow, size: NSSize) {
        window.isOpaque = false
        window.backgroundColor = .clear
        window.titlebarAppearsTransparent = true
        window.titleVisibility = .hidden
        window.toolbarStyle = .unified
        window.setContentSize(size)
        window.center()

        // Add NSVisualEffectView as background for window-level vibrancy
        let visualEffect = NSVisualEffectView()
        visualEffect.material = .underWindowBackground
        visualEffect.blendingMode = .behindWindow
        visualEffect.state = .active
        visualEffect.translatesAutoresizingMaskIntoConstraints = false

        if let contentView = window.contentView {
            contentView.addSubview(visualEffect, positioned: .below, relativeTo: nil)
            NSLayoutConstraint.activate([
                visualEffect.leadingAnchor.constraint(equalTo: contentView.leadingAnchor),
                visualEffect.trailingAnchor.constraint(equalTo: contentView.trailingAnchor),
                visualEffect.topAnchor.constraint(equalTo: contentView.topAnchor),
                visualEffect.bottomAnchor.constraint(equalTo: contentView.bottomAnchor),
            ])
        }
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

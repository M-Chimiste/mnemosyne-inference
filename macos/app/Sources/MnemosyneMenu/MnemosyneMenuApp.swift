import AppKit
import MnemosyneAppCore
import SwiftUI

@main
struct MnemosyneMenuApp: App {
    @NSApplicationDelegateAdaptor(MenuAppDelegate.self) private var appDelegate

    var body: some Scene {
        Settings {
            EmptyView()
        }
    }
}

@MainActor
final class MenuAppDelegate: NSObject, NSApplicationDelegate {
    private let workstationName = WorkstationIdentity.current
    private let viewModel = MenuViewModel()
    private let registration = LaunchAgentRegistration()
    private let popover = NSPopover()
    private var statusItem: NSStatusItem?
    private lazy var configurationWindowController = ConfigurationWindowController(
        registration: registration
    )

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApplication.shared.setActivationPolicy(.accessory)
        registration.migrateRenamedBundleIfNeeded()

        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        guard let button = item.button else {
            NSLog("Unified Inference could not create its menu bar button")
            NSApplication.shared.terminate(nil)
            return
        }

        if let image = NSImage(
            systemSymbolName: "brain.head.profile",
            accessibilityDescription: "\(workstationName) Inference"
        ) {
            image.isTemplate = true
            button.image = image
        } else {
            button.title = "M"
        }
        let controllerName = "\(workstationName) — Unified Inference"
        button.toolTip = controllerName
        button.setAccessibilityLabel(controllerName)
        button.target = self
        button.action = #selector(togglePopover(_:))
        item.isVisible = true
        statusItem = item

        let controller = NSHostingController(
            rootView: MenuContentView(
                workstationName: workstationName,
                viewModel: viewModel,
                registration: registration,
                openConfiguration: { [weak self] in
                    self?.configurationWindowController.show()
                }
            )
        )
        controller.sizingOptions = [.preferredContentSize]
        popover.behavior = .transient
        popover.animates = true
        popover.contentViewController = controller

        NSLog("Unified Inference menu bar status item installed for %@", workstationName)
    }

    @objc
    private func togglePopover(_ sender: NSStatusBarButton) {
        if popover.isShown {
            popover.performClose(sender)
            return
        }
        NSApplication.shared.activate(ignoringOtherApps: true)
        popover.show(
            relativeTo: sender.bounds,
            of: sender,
            preferredEdge: .minY
        )
    }
}

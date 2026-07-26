import AppKit
import MnemosyneAppCore
import Sparkle
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
    private var updaterController: SPUStandardUpdaterController?
    private lazy var configurationWindowController = ConfigurationWindowController(
        registration: registration,
        markSetupCompleted: { [weak self] in
            guard let self else { return }
            GuidedSetupEvidenceStore.recordCompletion(
                version: self.productVersion,
                build: self.productBuild
            )
        }
    )
    private var productVersion: String {
        Bundle.main.object(
            forInfoDictionaryKey: "CFBundleShortVersionString"
        ) as? String ?? "unknown"
    }
    private var productBuild: String {
        Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion")
            as? String ?? "unknown"
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApplication.shared.setActivationPolicy(.accessory)
        if Bundle.main.object(forInfoDictionaryKey: "SUPublicEDKey") != nil {
            updaterController = SPUStandardUpdaterController(
                startingUpdater: true,
                updaterDelegate: nil,
                userDriverDelegate: nil
            )
        }
        Task {
            await registration.refreshChangedBundleRegistrationsIfNeeded()
        }

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
                },
                checkForUpdates: { [weak self] in
                    self?.checkForUpdates()
                }
            )
        )
        controller.sizingOptions = [.preferredContentSize]
        popover.behavior = .transient
        popover.animates = true
        popover.contentViewController = controller

        NSLog("Unified Inference menu bar status item installed for %@", workstationName)
        if !UserDefaults.standard.bool(
            forKey: GuidedSetupEvidenceStore.completionKey
        ) {
            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                self.configurationWindowController.show()
                GuidedSetupEvidenceStore.recordFirstPresentation(
                    version: self.productVersion,
                    build: self.productBuild
                )
            }
        }
    }

    private func checkForUpdates() {
        guard let updaterController else {
            let alert = NSAlert()
            alert.alertStyle = .informational
            alert.messageText = "Updates are disabled in this local build"
            alert.informativeText =
                "Signed update checking is enabled in Developer ID release builds."
            alert.addButton(withTitle: "OK")
            alert.runModal()
            return
        }
        updaterController.checkForUpdates(nil)
    }

    func applicationShouldHandleReopen(
        _ sender: NSApplication,
        hasVisibleWindows flag: Bool
    ) -> Bool {
        configurationWindowController.show()
        return true
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

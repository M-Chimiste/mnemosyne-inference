import AppKit
import MnemosyneAppCore
import Sparkle
import SwiftUI

@main
struct MnemosyneMenuApp: App {
    @NSApplicationDelegateAdaptor(MenuAppDelegate.self) private var appDelegate

    var body: some Scene {
        Settings { EmptyView() }
            .commands {
                CommandGroup(replacing: .appSettings) {
                    Button("Settings…") { appDelegate.showWorkspace(.general) }.keyboardShortcut(",", modifiers: .command)
                    Button("Open Unified Inference") { appDelegate.showWorkspace() }.keyboardShortcut("o", modifiers: .command)
                    Divider()
                    Button("Overview") { appDelegate.showWorkspace(.overview) }.keyboardShortcut("1", modifiers: .command)
                    Button("Models") { appDelegate.showWorkspace(.models) }.keyboardShortcut("2", modifiers: .command)
                    Button("Downloads") { appDelegate.showWorkspace(.downloads) }.keyboardShortcut("3", modifiers: .command)
                    Button("Fleet") { appDelegate.showWorkspace(.fleet) }.keyboardShortcut("4", modifiers: .command)
                }
            }
    }
}

@MainActor
final class MenuAppDelegate: NSObject, NSApplicationDelegate {
    private let workstationName = WorkstationIdentity.current
    private let viewModel = MenuViewModel()
    private let registration = LaunchAgentRegistration()
    private let startup = ServiceStartupCoordinator()
    private let popover = NSPopover()
    private var statusItem: NSStatusItem?
    private var updaterController: SPUStandardUpdaterController?
    private lazy var configurationWindowController = ConfigurationWindowController(
        registration: registration,
        startup: startup,
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
        if WorkspacePreview.isRequested { WorkspacePreview.show(); return }
        NSApplication.shared.setActivationPolicy(.accessory)
        let guidedSetupCompleted = UserDefaults.standard.bool(
            forKey: GuidedSetupEvidenceStore.completionKey
        )
        if Bundle.main.object(forInfoDictionaryKey: "SUPublicEDKey") != nil {
            updaterController = SPUStandardUpdaterController(
                startingUpdater: true,
                updaterDelegate: nil,
                userDriverDelegate: nil
            )
        }
        Task {
            let hubStore = HubConfigurationStore(
                nativeEnvironmentURL: ControlConnectionConfiguration
                    .load().environmentURL
            )
            let hubConfigurationChanged: Bool
            let hubConfigured: Bool
            do {
                hubConfigured = try hubStore.loadConfiguration() != nil
            } catch {
                // Unreadable existing state is not evidence that Hub was
                // never configured; preserve discovery/recovery diagnostics.
                hubConfigured = true
            }
            do {
                hubConfigurationChanged = try hubStore
                    .refreshManagedConfiguration()
            } catch {
                hubConfigurationChanged = false
                NSLog(
                    "Unified Inference could not refresh preserved Hub configuration: %@",
                    error.localizedDescription
                )
            }
            await registration.refreshChangedBundleRegistrationsIfNeeded(
                hubConfigurationChanged: hubConfigurationChanged,
                hubConfigured: hubConfigured
            )
            await registration.applyStartupAtLoginDefaultsIfNeeded(
                guidedSetupCompleted: guidedSetupCompleted
            )
            await startup.connect(registration: registration.startupRegistrationState)
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

        let checkForUpdates: (() -> Void)? =
            updaterController == nil
                ? nil
                : { [weak self] in
                    self?.checkForUpdates()
                }
        let controller = NSHostingController(
            rootView: MenuContentView(
                workstationName: workstationName,
                viewModel: viewModel,
                registration: registration,
                startup: startup,
                openConfiguration: { [weak self] in
                    self?.showWorkspace()
                },
                checkForUpdates: checkForUpdates
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
                self.configurationWindowController.show(section: .setup)
                GuidedSetupEvidenceStore.recordFirstPresentation(
                    version: self.productVersion,
                    build: self.productBuild
                )
            }
        }
    }

    func showWorkspace(_ section: SettingsViewModel.Section? = nil) {
        if WorkspacePreview.isRequested { WorkspacePreview.navigate(section); return }
        popover.performClose(nil)
        configurationWindowController.show(section: section)
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
        showWorkspace()
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

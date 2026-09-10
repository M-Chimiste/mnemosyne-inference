import AppKit
import MnemosyneAppCore
import SwiftUI

@MainActor
final class ConfigurationWindowController: NSObject, NSWindowDelegate {
    private let registration: LaunchAgentRegistration
    private let startup: ServiceStartupCoordinator
    private let markSetupCompleted: () -> Void
    private let viewModel = SettingsViewModel()
    private var window: NSWindow?

    init(
        registration: LaunchAgentRegistration,
        startup: ServiceStartupCoordinator,
        markSetupCompleted: @escaping () -> Void = {}
    ) {
        self.registration = registration
        self.startup = startup
        self.markSetupCompleted = markSetupCompleted
        super.init()
    }

    func show(section: SettingsViewModel.Section? = nil) {
        if let section { viewModel.selectedSection = section }
        let window = window ?? makeWindow()
        if !window.isVisible, !viewModel.hasUnsavedChanges, startup.state == .ready {
            Task { await viewModel.load() }
        }
        NSApplication.shared.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        guard viewModel.hasUnsavedChanges else {
            viewModel.cancelRestartWait()
            viewModel.fleetPairingViewDidDisappear()
            return true
        }

        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = "Discard unsaved configuration changes?"
        alert.informativeText =
            "Changes in the settings window have not been saved."
        alert.addButton(withTitle: "Cancel")
        alert.addButton(withTitle: "Discard Changes")
        let response = alert.runModal()
        guard response == .alertSecondButtonReturn else { return false }
        viewModel.discardChanges()
        viewModel.fleetPairingViewDidDisappear()
        return true
    }

    private var checkingRestart = false

    private func requestRestart() {
        guard !checkingRestart, !viewModel.isWaitingForRestart else { return }
        guard !viewModel.hasUnsavedChanges else {
            let alert = NSAlert()
            alert.messageText = "Save or discard your changes first"
            alert.informativeText = "The service restarts with the last saved settings."
            alert.addButton(withTitle: "OK")
            alert.runModal()
            return
        }
        checkingRestart = true
        Task {
            defer { checkingRestart = false }
            do {
                let work = try await viewModel.restartActivity()
                if work.inFlightRequests > 0 || work.queuedRequests > 0 || !["idle", "ready"].contains(work.state) {
                    let alert = NSAlert()
                    alert.messageText = "This Mac is still working"
                    alert.informativeText = "\(work.inFlightRequests) active requests and \(work.queuedRequests) queued. Wait for idle before restarting, or restart now and interrupt work. Keep clients quiet while waiting."
                    alert.addButton(withTitle: "Wait for Idle")
                    alert.addButton(withTitle: "Cancel")
                    alert.addButton(withTitle: "Restart Now")
                    let response = alert.runModal()
                    if response == .alertFirstButtonReturn {
                        guard await viewModel.waitForIdleBeforeRestart() else { return }
                    } else if response != .alertThirdButtonReturn { return }
                }
                await performRestart()
            } catch {
                let alert = NSAlert()
                alert.messageText = "Current activity is unavailable"
                alert.informativeText = "The service did not respond. Restarting may interrupt active requests."
                alert.addButton(withTitle: "Cancel")
                alert.addButton(withTitle: "Restart Now")
                if alert.runModal() == .alertSecondButtonReturn { await performRestart() }
            }
        }
    }

    private func performRestart() async {
        guard !viewModel.hasUnsavedChanges, viewModel.serviceRestartStarted() else { return }
        startup.prepare()
        let succeeded = await registration.restartAgent()
        let error = registration.lastError
        await startup.connect(registration: registration.startupRegistrationState)
        await viewModel.serviceRestartRequested(succeeded: succeeded, error: error)
    }

    private func makeWindow() -> NSWindow {
        let content = SettingsView(
            viewModel: viewModel,
            registration: registration,
            startup: startup,
            markSetupCompleted: markSetupCompleted,
            restartService: { [weak self] in self?.requestRestart() }
        )
        let controller = NSHostingController(rootView: content)
        let window = NSWindow(contentViewController: controller)
        window.title = "Unified Inference"
        window.styleMask = [.titled, .closable, .miniaturizable, .resizable]
        window.setContentSize(NSSize(width: 1120, height: 800))
        window.minSize = NSSize(width: 960, height: 680)
        window.setFrameAutosaveName("UnifiedInferenceWorkspace")
        window.isReleasedWhenClosed = false
        window.tabbingMode = .disallowed
        window.delegate = self
        window.center()
        self.window = window
        return window
    }
}

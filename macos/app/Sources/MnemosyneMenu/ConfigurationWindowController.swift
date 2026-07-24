import AppKit
import SwiftUI

@MainActor
final class ConfigurationWindowController: NSObject, NSWindowDelegate {
    private let registration: LaunchAgentRegistration
    private let viewModel = SettingsViewModel()
    private var window: NSWindow?

    init(registration: LaunchAgentRegistration) {
        self.registration = registration
        super.init()
    }

    func show() {
        let window = window ?? makeWindow()
        if !window.isVisible, !viewModel.hasUnsavedChanges {
            Task { await viewModel.load() }
        }
        NSApplication.shared.activate(ignoringOtherApps: true)
        window.makeKeyAndOrderFront(nil)
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        guard viewModel.hasUnsavedChanges else { return true }

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
        return true
    }

    private func makeWindow() -> NSWindow {
        let content = SettingsView(
            viewModel: viewModel,
            restartService: { [weak self] in
                guard let self else { return }
                guard self.viewModel.serviceRestartStarted() else { return }
                Task {
                    let succeeded = await self.registration.restartAgent()
                    let error = self.registration.lastError
                    await self.viewModel.serviceRestartRequested(
                        succeeded: succeeded,
                        error: error
                    )
                }
            }
        )
        let controller = NSHostingController(rootView: content)
        let window = NSWindow(contentViewController: controller)
        window.title = "Unified Inference Settings"
        window.styleMask = [.titled, .closable, .miniaturizable, .resizable]
        window.setContentSize(NSSize(width: 980, height: 720))
        window.minSize = NSSize(width: 900, height: 650)
        window.isReleasedWhenClosed = false
        window.tabbingMode = .disallowed
        window.delegate = self
        window.center()
        self.window = window
        return window
    }
}

import AppKit
import SwiftUI

@MainActor
final class ConfigurationWindowController: NSObject, NSWindowDelegate {
    private let registration: LaunchAgentRegistration
    private let viewModel = ConfigurationEditorViewModel()
    private var window: NSWindow?

    init(registration: LaunchAgentRegistration) {
        self.registration = registration
        super.init()
    }

    func show() {
        let window = window ?? makeWindow()
        if !window.isVisible, !viewModel.hasUnsavedChanges {
            viewModel.loadFromDisk()
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
            "Changes to config.yaml or .env have not been saved."
        alert.addButton(withTitle: "Cancel")
        alert.addButton(withTitle: "Discard Changes")
        let response = alert.runModal()
        guard response == .alertSecondButtonReturn else { return false }
        viewModel.loadFromDisk()
        return true
    }

    private func makeWindow() -> NSWindow {
        let content = ConfigurationEditorView(
            viewModel: viewModel,
            restartService: { [weak self] in
                guard let self else { return }
                let succeeded = self.registration.restartAgent()
                self.viewModel.serviceRestartRequested(
                    succeeded: succeeded,
                    error: self.registration.lastError
                )
            }
        )
        let controller = NSHostingController(rootView: content)
        let window = NSWindow(contentViewController: controller)
        window.title = "Unified Inference Configuration"
        window.styleMask = [.titled, .closable, .miniaturizable, .resizable]
        window.setContentSize(NSSize(width: 780, height: 620))
        window.minSize = NSSize(width: 680, height: 480)
        window.isReleasedWhenClosed = false
        window.tabbingMode = .disallowed
        window.delegate = self
        window.center()
        self.window = window
        return window
    }
}

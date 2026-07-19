import ServiceManagement
import SwiftUI

@MainActor
final class LaunchAgentRegistration: ObservableObject {
    static let agentPlistName = "com.mnemosyne.inference.agent.plist"

    @Published private(set) var agentStatus: SMAppService.Status
    @Published private(set) var menuLoginStatus: SMAppService.Status
    @Published private(set) var lastError: String?

    private let agent = SMAppService.agent(plistName: agentPlistName)
    private let menuLoginItem = SMAppService.mainApp

    init() {
        agentStatus = agent.status
        menuLoginStatus = menuLoginItem.status
    }

    func refresh() {
        agentStatus = agent.status
        menuLoginStatus = menuLoginItem.status
    }

    func enableAgent() {
        perform {
            try agent.register()
        }
    }

    func disableAgent() {
        perform {
            try agent.unregister()
        }
    }

    func enableMenuAtLogin() {
        perform {
            try menuLoginItem.register()
        }
    }

    func disableMenuAtLogin() {
        perform {
            try menuLoginItem.unregister()
        }
    }

    func openLoginItemsSettings() {
        SMAppService.openSystemSettingsLoginItems()
    }

    func label(for status: SMAppService.Status) -> String {
        switch status {
        case .notRegistered:
            "Disabled"
        case .enabled:
            "Enabled"
        case .requiresApproval:
            "Needs approval"
        case .notFound:
            "Not found"
        @unknown default:
            "Unknown"
        }
    }

    private func perform(_ operation: () throws -> Void) {
        do {
            try operation()
            lastError = nil
        } catch {
            lastError = error.localizedDescription
        }
        refresh()
    }
}

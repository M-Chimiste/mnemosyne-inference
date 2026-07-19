import ServiceManagement
import SwiftUI

@MainActor
final class LaunchAgentRegistration: ObservableObject {
    static let agentPlistName = "com.mnemosyne.inference.agent.plist"
    private static let unifiedInferenceMigrationKey =
        "didMigrateToUnifiedInferenceBundleV1"

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

    /// ServiceManagement caches the containing app's code requirement. Refresh
    /// enabled registrations once when upgrading from Mnemosyne.app to the
    /// renamed Unified Inference.app so launchd resolves the new bundle path.
    func migrateRenamedBundleIfNeeded() {
        guard Bundle.main.bundleURL.lastPathComponent == "Unified Inference.app" else {
            return
        }

        let defaults = UserDefaults.standard
        guard !defaults.bool(forKey: Self.unifiedInferenceMigrationKey) else {
            return
        }

        do {
            if agent.status == .enabled {
                try agent.unregister()
                try agent.register()
            }
            if menuLoginItem.status == .enabled {
                try menuLoginItem.unregister()
                try menuLoginItem.register()
            }
            defaults.set(true, forKey: Self.unifiedInferenceMigrationKey)
            lastError = nil
        } catch {
            // Leave the migration marker unset so the next launch can retry.
            lastError = error.localizedDescription
        }
        refresh()
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

    @discardableResult
    func restartAgent() -> Bool {
        guard agent.status == .enabled else {
            lastError = "Enable the background service before restarting it."
            refresh()
            return false
        }
        do {
            try agent.unregister()
            try agent.register()
            lastError = nil
            refresh()
            return true
        } catch {
            lastError = error.localizedDescription
            refresh()
            return false
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

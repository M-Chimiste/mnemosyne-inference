import CryptoKit
import MnemosyneAppCore
import ServiceManagement
import SwiftUI

@MainActor
final class LaunchAgentRegistration: ObservableObject {
    static let agentPlistName = "com.mnemosyne.inference.agent.plist"
    static let hubAgentPlistName = "com.mnemosyne.inference.hub.plist"
    private static let registrationFingerprintKey =
        "registeredServiceBundleFingerprintV2"
    private static let pendingAgentRefreshKey =
        "pendingServiceBundleAgentRefreshV1"
    private static let pendingMenuRefreshKey =
        "pendingServiceBundleMenuRefreshV1"
    private static let pendingHubRefreshKey =
        "pendingServiceBundleHubRefreshV1"

    @Published private(set) var agentStatus: SMAppService.Status
    @Published private(set) var hubAgentStatus: SMAppService.Status
    @Published private(set) var menuLoginStatus: SMAppService.Status
    @Published private(set) var lastError: String?
    @Published private(set) var isChangingRegistration = false

    private let agent = SMAppService.agent(plistName: agentPlistName)
    private let hubAgent = SMAppService.agent(plistName: hubAgentPlistName)
    private let menuLoginItem = SMAppService.mainApp

    init() {
        agentStatus = agent.status
        hubAgentStatus = hubAgent.status
        menuLoginStatus = menuLoginItem.status
    }

    func refresh() {
        agentStatus = agent.status
        hubAgentStatus = hubAgent.status
        menuLoginStatus = menuLoginItem.status
    }

    /// Apply the ordinary first-install startup behavior exactly once. Existing
    /// configured installations retain their current choices, and an explicit
    /// later disable is never undone on a subsequent app launch.
    func applyStartupAtLoginDefaultsIfNeeded(
        guidedSetupCompleted: Bool
    ) async {
        let action = StartupAtLoginDefaults.pendingAction(
            guidedSetupCompleted: guidedSetupCompleted
        )
        guard action != .none else { return }
        guard action == .enableBoth else {
            StartupAtLoginDefaults.markApplied()
            return
        }
        guard beginRegistrationChange() else { return }
        defer { finishRegistrationChange() }

        var failures: [String] = []
        var notices: [String] = []
        for (service, name) in [
            (agent, "Background service"),
            (menuLoginItem, "Menu login item"),
        ] {
            do {
                let status = try await registerAndWait(
                    service,
                    serviceName: name
                )
                if status == .requiresApproval {
                    notices.append(approvalMessage(for: name))
                }
            } catch {
                failures.append(
                    "Could not enable \(name.lowercased()) at login: \(error.localizedDescription)"
                )
            }
        }

        if failures.isEmpty {
            StartupAtLoginDefaults.markApplied()
        }
        let messages = failures + notices
        lastError = messages.isEmpty ? nil : messages.joined(separator: "\n")
    }

    /// ServiceManagement caches the containing app's path and code requirement.
    /// Refresh enabled registrations when the installed signed bundle changes. This
    /// covers the Mnemosyne.app rename and subsequent locally ad-hoc-signed
    /// updates without interrupting the service on ordinary menu launches.
    func refreshChangedBundleRegistrationsIfNeeded(
        hubConfigurationChanged: Bool = false
    ) async {
        guard !isChangingRegistration else { return }
        let defaults = UserDefaults.standard
        let fingerprint = currentBundleFingerprint()
        let bundleChanged = defaults.string(
            forKey: Self.registrationFingerprintKey
        ) != fingerprint
        let agentRefreshPending = defaults.bool(
            forKey: Self.pendingAgentRefreshKey
        )
        let menuRefreshPending = defaults.bool(
            forKey: Self.pendingMenuRefreshKey
        )
        let hubRefreshPending = defaults.bool(
            forKey: Self.pendingHubRefreshKey
        )
        guard bundleChanged || agentRefreshPending || menuRefreshPending
                || hubRefreshPending || hubConfigurationChanged
        else {
            return
        }

        isChangingRegistration = true
        defer {
            isChangingRegistration = false
            refresh()
        }

        var failures: [String] = []
        var notices: [String] = []
        let agentAction = BundleRegistrationRefreshPolicy.action(
            bundleChanged: bundleChanged,
            refreshPending: agentRefreshPending,
            state: managedState(agent.status)
        )
        let menuAction = BundleRegistrationRefreshPolicy.action(
            bundleChanged: bundleChanged,
            refreshPending: menuRefreshPending,
            state: managedState(menuLoginItem.status)
        )
        let hubAction = BundleRegistrationRefreshPolicy.action(
            bundleChanged: bundleChanged || hubConfigurationChanged,
            refreshPending: hubRefreshPending,
            state: managedState(hubAgent.status)
        )

        if agentAction == .refresh {
            defaults.set(true, forKey: Self.pendingAgentRefreshKey)
        } else if agentAction == .retryDiscovery {
            failures.append(
                "The replaced app is still waiting for macOS to discover its bundled background service. Reopen Unified Inference to retry."
            )
        }
        if menuAction == .refresh {
            defaults.set(true, forKey: Self.pendingMenuRefreshKey)
        } else if menuAction == .retryDiscovery {
            failures.append(
                "The replaced app is still waiting for macOS to discover its menu login item. Reopen Unified Inference to retry."
            )
        }
        if hubAction == .refresh {
            defaults.set(true, forKey: Self.pendingHubRefreshKey)
        } else if hubAction == .retryDiscovery {
            failures.append(
                "The replaced app is still waiting for macOS to discover its bundled Hub service. Reopen Unified Inference to retry."
            )
        }

        if defaults.bool(forKey: Self.pendingAgentRefreshKey) {
            do {
                let status = try await refreshRegistration(
                    agent,
                    serviceName: "Background service"
                )
                defaults.removeObject(forKey: Self.pendingAgentRefreshKey)
                if status == .requiresApproval {
                    notices.append(approvalMessage(for: "Background service"))
                }
            } catch {
                failures.append(
                    "Could not update the background-service registration: \(error.localizedDescription)"
                )
            }
        }
        if !Task.isCancelled,
           defaults.bool(forKey: Self.pendingMenuRefreshKey) {
            do {
                let status = try await refreshRegistration(
                    menuLoginItem,
                    serviceName: "Menu login item"
                )
                defaults.removeObject(forKey: Self.pendingMenuRefreshKey)
                if status == .requiresApproval {
                    notices.append(approvalMessage(for: "Menu login item"))
                }
            } catch {
                failures.append(
                    "Could not update the menu login-item registration: \(error.localizedDescription)"
                )
            }
        }
        if !Task.isCancelled,
           defaults.bool(forKey: Self.pendingHubRefreshKey) {
            do {
                let status = try await refreshRegistration(
                    hubAgent,
                    serviceName: "Fleet Hub"
                )
                defaults.removeObject(forKey: Self.pendingHubRefreshKey)
                if status == .requiresApproval {
                    notices.append(approvalMessage(for: "Fleet Hub"))
                }
            } catch {
                failures.append(
                    "Could not update the Fleet Hub registration: \(error.localizedDescription)"
                )
            }
        }

        if Task.isCancelled {
            failures.append(
                "The registration update was interrupted. It will be retried the next time Unified Inference opens."
            )
        }
        let refreshPending = defaults.bool(forKey: Self.pendingAgentRefreshKey)
            || defaults.bool(forKey: Self.pendingMenuRefreshKey)
            || defaults.bool(forKey: Self.pendingHubRefreshKey)
        if failures.isEmpty, !refreshPending {
            defaults.set(fingerprint, forKey: Self.registrationFingerprintKey)
        }
        let messages = failures + notices
        lastError = messages.isEmpty ? nil : messages.joined(separator: "\n")
    }

    private func currentBundleFingerprint() -> String {
        let menuExecutable = Bundle.main.bundleURL
            .appending(path: "Contents/MacOS/UnifiedInference")
        let helper = Bundle.main.bundleURL
            .appending(path: "Contents/MacOS/mnemosyne-service-bootstrap")
        let hubHelper = Bundle.main.bundleURL
            .appending(path: "Contents/MacOS/mnemosyne-hub-bootstrap")
        let signature = Bundle.main.bundleURL
            .appending(path: "Contents/_CodeSignature/CodeResources")
        let version = Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String
        return [
            Bundle.main.bundleURL.standardizedFileURL.path,
            version ?? "unknown",
            fileFingerprint(menuExecutable),
            fileFingerprint(helper),
            fileFingerprint(hubHelper),
            fileFingerprint(signature),
        ].joined(separator: "|")
    }

    private func fileFingerprint(_ url: URL) -> String {
        if let data = try? Data(contentsOf: url, options: [.mappedIfSafe]) {
            return SHA256.hash(data: data)
                .map { String(format: "%02x", $0) }
                .joined()
        }

        let attributes = try? FileManager.default.attributesOfItem(atPath: url.path)
        let size = attributes?[.size] as? NSNumber
        let modified = attributes?[.modificationDate] as? Date
        return [
            size?.stringValue ?? "unknown",
            modified.map { String($0.timeIntervalSince1970) } ?? "unknown",
        ].joined(separator: "@")
    }

    func enableAgent() async {
        guard beginRegistrationChange() else { return }
        defer { finishRegistrationChange() }
        do {
            let status = try await registerAndWait(
                agent,
                serviceName: "Background service"
            )
            UserDefaults.standard.removeObject(forKey: Self.pendingAgentRefreshKey)
            lastError = status == .requiresApproval
                ? approvalMessage(for: "Background service") : nil
        } catch {
            lastError = "Could not enable the background service: \(error.localizedDescription)"
        }
    }

    func disableAgent() async {
        guard beginRegistrationChange() else { return }
        UserDefaults.standard.removeObject(forKey: Self.pendingAgentRefreshKey)
        defer { finishRegistrationChange() }
        do {
            try await unregisterAndWait(
                agent,
                serviceName: "Background service"
            )
            lastError = nil
        } catch {
            lastError = "Could not disable the background service: \(error.localizedDescription)"
        }
    }

    func enableHubAgent() async {
        guard beginRegistrationChange() else { return }
        defer { finishRegistrationChange() }
        do {
            let status = try await registerAndWait(
                hubAgent,
                serviceName: "Fleet Hub"
            )
            UserDefaults.standard.removeObject(forKey: Self.pendingHubRefreshKey)
            lastError = status == .requiresApproval
                ? approvalMessage(for: "Fleet Hub") : nil
        } catch {
            lastError = "Could not enable Fleet Hub: \(error.localizedDescription)"
        }
    }

    func disableHubAgent() async {
        guard beginRegistrationChange() else { return }
        UserDefaults.standard.removeObject(forKey: Self.pendingHubRefreshKey)
        defer { finishRegistrationChange() }
        do {
            try await unregisterAndWait(
                hubAgent,
                serviceName: "Fleet Hub"
            )
            lastError = nil
        } catch {
            lastError = "Could not disable Fleet Hub: \(error.localizedDescription)"
        }
    }

    @discardableResult
    func restartHubAgent() async -> Bool {
        guard beginRegistrationChange() else { return false }
        defer { finishRegistrationChange() }
        guard hubAgent.status == .enabled else {
            lastError = hubAgent.status == .requiresApproval
                ? approvalMessage(for: "Fleet Hub")
                : "Enable Fleet Hub before restarting it."
            return false
        }
        do {
            let status = try await refreshRegistration(
                hubAgent,
                serviceName: "Fleet Hub"
            )
            guard status == .enabled else {
                lastError = approvalMessage(for: "Fleet Hub")
                return false
            }
            lastError = nil
            return true
        } catch {
            lastError = "Could not restart Fleet Hub: \(error.localizedDescription)"
            return false
        }
    }

    @discardableResult
    func restartAgent() async -> Bool {
        guard beginRegistrationChange() else { return false }
        defer { finishRegistrationChange() }
        guard agent.status == .enabled else {
            lastError = agent.status == .requiresApproval
                ? approvalMessage(for: "Background service")
                : "Enable the background service before restarting it."
            return false
        }
        do {
            let status = try await refreshRegistration(
                agent,
                serviceName: "Background service"
            )
            guard status == .enabled else {
                lastError = approvalMessage(for: "Background service")
                return false
            }
            lastError = nil
            return true
        } catch {
            lastError = """
            Could not restart the background service safely: \(error.localizedDescription) \
            The saved settings still require a restart.
            """
            return false
        }
    }

    func enableMenuAtLogin() async {
        guard beginRegistrationChange() else { return }
        defer { finishRegistrationChange() }
        do {
            let status = try await registerAndWait(
                menuLoginItem,
                serviceName: "Menu login item"
            )
            UserDefaults.standard.removeObject(forKey: Self.pendingMenuRefreshKey)
            lastError = status == .requiresApproval
                ? approvalMessage(for: "Menu login item") : nil
        } catch {
            lastError = "Could not enable the menu login item: \(error.localizedDescription)"
        }
    }

    func disableMenuAtLogin() async {
        guard beginRegistrationChange() else { return }
        UserDefaults.standard.removeObject(forKey: Self.pendingMenuRefreshKey)
        defer { finishRegistrationChange() }
        do {
            try await unregisterAndWait(
                menuLoginItem,
                serviceName: "Menu login item"
            )
            lastError = nil
        } catch {
            lastError = "Could not disable the menu login item: \(error.localizedDescription)"
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

    private func beginRegistrationChange() -> Bool {
        guard !isChangingRegistration else {
            lastError = "Another Login Items change is still in progress. Wait for it to finish and try again."
            return false
        }
        isChangingRegistration = true
        return true
    }

    private func finishRegistrationChange() {
        isChangingRegistration = false
        refresh()
    }

    private func refreshRegistration(
        _ service: SMAppService,
        serviceName: String
    ) async throws -> ManagedServiceRegistrationState {
        if managedState(service.status) != .notRegistered {
            try await unregisterAndWait(service, serviceName: serviceName)
        }
        return try await registerAndWait(service, serviceName: serviceName)
    }

    private func unregisterAndWait(
        _ service: SMAppService,
        serviceName: String
    ) async throws {
        guard managedState(service.status) != .notRegistered else { return }
        do {
            // The SDK's async completion is invoked only after the running
            // helper has been killed, at which point re-registration is safe.
            try await unregisterService(service)
        } catch {
            guard managedState(service.status) == .notRegistered else {
                throw error
            }
        }
        _ = try await ServiceRegistrationPolling.waitUntilUnregistered(
            service: serviceName
        ) {
            self.managedState(service.status)
        }
        try await ServiceRegistrationPolling.waitUntilSafeToReregister(
            service: serviceName
        ) {
            self.managedState(service.status)
        }
    }

    private func unregisterService(_ service: SMAppService) async throws {
        try await withCheckedThrowingContinuation {
            (continuation: CheckedContinuation<Void, any Error>) in
            // Use the completion-handler API explicitly. Some macOS SDK
            // concurrency overlays synthesize a nonisolated async overload
            // that attempts to send this main-actor SMAppService across an
            // isolation boundary under Swift 6 strict checking.
            service.unregister { error in
                if let error {
                    continuation.resume(throwing: error)
                } else {
                    continuation.resume()
                }
            }
        }
    }

    private func registerAndWait(
        _ service: SMAppService,
        serviceName: String
    ) async throws -> ManagedServiceRegistrationState {
        let current = managedState(service.status)
        if current == .enabled || current == .requiresApproval {
            return current
        }
        do {
            try service.register()
        } catch {
            let stateAfterError = managedState(service.status)
            guard stateAfterError == .enabled
                    || stateAfterError == .requiresApproval
            else {
                throw error
            }
        }
        return try await ServiceRegistrationPolling.waitUntilRegistered(
            service: serviceName
        ) {
            self.managedState(service.status)
        }
    }

    private func managedState(
        _ status: SMAppService.Status
    ) -> ManagedServiceRegistrationState {
        switch status {
        case .notRegistered: .notRegistered
        case .enabled: .enabled
        case .requiresApproval: .requiresApproval
        case .notFound: .notFound
        @unknown default: .unknown
        }
    }

    private func approvalMessage(for serviceName: String) -> String {
        "\(serviceName) is registered but needs approval in System Settings → General → Login Items."
    }
}

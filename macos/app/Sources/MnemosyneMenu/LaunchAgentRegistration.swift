import CryptoKit
import MnemosyneAppCore
import ServiceManagement
import SwiftUI

@MainActor
final class LaunchAgentRegistration: ObservableObject {
    static let agentPlistName = "com.mnemosyne.inference.agent.plist"
    private static let registrationFingerprintKey =
        "registeredServiceBundleFingerprintV2"
    private static let pendingAgentRefreshKey =
        "pendingServiceBundleAgentRefreshV1"
    private static let pendingMenuRefreshKey =
        "pendingServiceBundleMenuRefreshV1"

    @Published private(set) var agentStatus: SMAppService.Status
    @Published private(set) var menuLoginStatus: SMAppService.Status
    @Published private(set) var lastError: String?
    @Published private(set) var isChangingRegistration = false

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

    /// ServiceManagement caches the containing app's path and code requirement.
    /// Refresh enabled registrations when the installed signed bundle changes. This
    /// covers the Mnemosyne.app rename and subsequent locally ad-hoc-signed
    /// updates without interrupting the service on ordinary menu launches.
    func refreshChangedBundleRegistrationsIfNeeded() async {
        guard !isChangingRegistration else { return }
        let defaults = UserDefaults.standard
        let fingerprint = currentBundleFingerprint()
        guard defaults.string(forKey: Self.registrationFingerprintKey) != fingerprint else {
            return
        }

        isChangingRegistration = true
        defer {
            isChangingRegistration = false
            refresh()
        }

        if isRegistered(agent.status) {
            defaults.set(true, forKey: Self.pendingAgentRefreshKey)
        }
        if isRegistered(menuLoginItem.status) {
            defaults.set(true, forKey: Self.pendingMenuRefreshKey)
        }

        var failures: [String] = []
        var notices: [String] = []
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

        if Task.isCancelled {
            failures.append(
                "The registration update was interrupted. It will be retried the next time Unified Inference opens."
            )
        }
        let refreshPending = defaults.bool(forKey: Self.pendingAgentRefreshKey)
            || defaults.bool(forKey: Self.pendingMenuRefreshKey)
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
        let signature = Bundle.main.bundleURL
            .appending(path: "Contents/_CodeSignature/CodeResources")
        let version = Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String
        return [
            Bundle.main.bundleURL.standardizedFileURL.path,
            version ?? "unknown",
            fileFingerprint(menuExecutable),
            fileFingerprint(helper),
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

    private func isRegistered(_ status: SMAppService.Status) -> Bool {
        status == .enabled || status == .requiresApproval
    }

    private func approvalMessage(for serviceName: String) -> String {
        "\(serviceName) is registered but needs approval in System Settings → General → Login Items."
    }
}

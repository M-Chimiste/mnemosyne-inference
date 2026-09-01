import AppKit
import Foundation
import MnemosyneAppCore
import ServiceManagement

@MainActor
final class HubModeViewModel: ObservableObject {
    enum ExposureMode: String, CaseIterable, Identifiable {
        case tailscale = "Tailscale HTTPS"
        case existingHTTPS = "Existing HTTPS proxy"

        var id: String { rawValue }
    }

    @Published var exposureMode: ExposureMode = .tailscale
    @Published var customPublicOrigin = ""
    @Published var includeLocalWorker = false
    @Published private(set) var configuration: HubModeConfiguration?
    @Published private(set) var isWorking = false
    @Published private(set) var hubHealthy = false
    @Published private(set) var statusMessage = "Hub Mode has not been configured."
    @Published private(set) var isError = false
    @Published private(set) var pendingPairingClaims: [HubPairingClaim] = []
    @Published private(set) var pairingEnrollments: [HubPairingEnrollment] = []
    @Published private(set) var presencePINs: [String: String] = [:]
    @Published private(set) var pairingInFlightClaimIDs: Set<String> = []
    @Published private(set) var enrollmentInFlightIDs: Set<String> = []
    @Published private(set) var isRefreshingPairing = false
    @Published private(set) var pairingStatusMessage = ""
    @Published private(set) var pairingIsError = false

    private let controlConfiguration: ControlConnectionConfiguration
    private let store: HubConfigurationStore
    private let snapshotClient = HubLocalSnapshotClient()
    private let tailscale = HubTailscaleManager()

    init(configuration: ControlConnectionConfiguration = .load()) {
        controlConfiguration = configuration
        store = HubConfigurationStore(
            nativeEnvironmentURL: configuration.environmentURL
        )
        if let existing = try? store.loadConfiguration() {
            self.configuration = existing
            customPublicOrigin = existing.publicOrigin
            exposureMode = existing.managedTailscaleServe
                ? .tailscale : .existingHTTPS
            includeLocalWorker = existing.includesLocalWorker
        }
    }

    func load(registration: LaunchAgentRegistration) async {
        registration.refresh()
        configuration = try? store.loadConfiguration()
        if let configuration {
            customPublicOrigin = configuration.publicOrigin
            exposureMode = configuration.managedTailscaleServe
                ? .tailscale : .existingHTTPS
            includeLocalWorker = configuration.includesLocalWorker
        }
        hubHealthy = await healthCheck()
        updateStatus(registration: registration)
        if hubHealthy {
            await refreshPairingAdministration(reportFailure: false)
        } else {
            clearPairingAdministration()
        }
    }

    func promote(
        registration: LaunchAgentRegistration,
        fallbackWorkerNodeID: String
    ) async {
        guard !isWorking else { return }
        isWorking = true
        isError = false
        statusMessage = "Preparing private Hub configuration…"
        defer { isWorking = false }

        do {
            let discovery: HubTailscaleManager.Discovery?
            let publicOrigin: String
            switch exposureMode {
            case .tailscale:
                statusMessage = "Detecting Nyx on Tailscale…"
                let found = try await tailscale.discover()
                discovery = found
                publicOrigin = found.publicOrigin
            case .existingHTTPS:
                discovery = nil
                publicOrigin = try HubConfigurationStore.normalizedHTTPSOrigin(
                    customPublicOrigin
                )
            }

            let credentialStore = CredentialStore(
                environmentURL: controlConfiguration.environmentURL
            )
            let secrets = try store.prepareSecrets(
                nativeCredentialStore: credentialStore,
                provisionLocalWorker: includeLocalWorker
            )
            var nodeID = fallbackWorkerNodeID
            var deployments: [HubPublishedDeployment] = []
            if includeLocalWorker {
                if registration.agentStatus != .enabled {
                    statusMessage = "Enabling the local inference worker…"
                    await registration.enableAgent()
                    guard registration.agentStatus == .enabled else {
                        throw HubModePresentationError.registration(
                            registration.lastError
                                ?? "Approve the background service in Login Items, then try again."
                        )
                    }
                }
                statusMessage = "Restarting the local worker with its Hub credentials…"
                guard await registration.restartAgent() else {
                    throw HubModePresentationError.registration(
                        registration.lastError
                            ?? "The local worker could not restart."
                    )
                }
                statusMessage = "Verifying Fleet-eligible local models…"
                let local = try await snapshotClient.waitForEligibleSnapshot(
                    snapshotKey: secrets.localWorkerSnapshotKey
                )
                nodeID = local.nodeID.isEmpty
                    ? fallbackWorkerNodeID : local.nodeID
                deployments = local.deployments
            }
            let saved = try store.saveConfiguration(
                publicOrigin: publicOrigin,
                localWorkerNodeID: nodeID,
                managedTailscaleServe: discovery != nil,
                includesLocalWorker: includeLocalWorker,
                deployments: deployments
            )
            configuration = saved
            customPublicOrigin = saved.publicOrigin

            statusMessage = "Starting Fleet Hub at login…"
            if registration.hubAgentStatus == .enabled {
                guard await registration.restartHubAgent() else {
                    throw HubModePresentationError.registration(
                        registration.lastError ?? "Fleet Hub could not restart."
                    )
                }
            } else {
                await registration.enableHubAgent()
                guard registration.hubAgentStatus == .enabled else {
                    throw HubModePresentationError.registration(
                        registration.lastError
                            ?? "Approve Fleet Hub in Login Items, then try again."
                    )
                }
            }
            guard await waitForHealth() else {
                throw HubModePresentationError.health
            }

            if let discovery {
                statusMessage = "Publishing Fleet Hub privately through Tailscale HTTPS…"
                try await tailscale.enableServe(using: discovery)
            }
            hubHealthy = true
            isError = false
            statusMessage = includeLocalWorker
                ? "This Mac is running Fleet Hub with its local worker enrolled as LIMITED / overflow."
                : "This Mac is running Fleet Hub without local inference overhead. Enrolled nodes publish their eligible models automatically."
            await refreshPairingAdministration(reportFailure: false)
        } catch {
            hubHealthy = await healthCheck()
            isError = true
            statusMessage = error.localizedDescription
        }
    }

    func disable(registration: LaunchAgentRegistration) async {
        guard !isWorking else { return }
        isWorking = true
        isError = false
        statusMessage = "Stopping Fleet Hub…"
        defer { isWorking = false }
        let managedServe = configuration?.managedTailscaleServe == true
        await registration.disableHubAgent()
        if registration.hubAgentStatus == .notRegistered {
            if managedServe { await tailscale.disableServeIfAvailable() }
            hubHealthy = false
            clearPairingAdministration()
            statusMessage = "Fleet Hub is disabled. Its configuration, invitations, enrollments, and route history are preserved."
        } else {
            isError = true
            statusMessage = registration.lastError ?? "Fleet Hub could not be disabled."
        }
    }

    func reenable(registration: LaunchAgentRegistration) async {
        guard !isWorking, let configuration else { return }
        isWorking = true
        isError = false
        statusMessage = "Starting the preserved Fleet Hub…"
        defer { isWorking = false }
        await registration.enableHubAgent()
        guard registration.hubAgentStatus == .enabled else {
            isError = true
            statusMessage = registration.lastError ?? "Fleet Hub could not start."
            return
        }
        if configuration.managedTailscaleServe {
            do {
                let discovery = try await tailscale.discover()
                guard discovery.publicOrigin == configuration.publicOrigin else {
                    throw HubModeError.tailscaleNotConnected
                }
                try await tailscale.enableServe(using: discovery)
            } catch {
                isError = true
                statusMessage = error.localizedDescription
                hubHealthy = await healthCheck()
                return
            }
        }
        hubHealthy = await waitForHealth()
        isError = !hubHealthy
        statusMessage = hubHealthy
            ? "Fleet Hub is running with its preserved identity and enrollments."
            : "Fleet Hub was registered but did not become healthy."
        if hubHealthy {
            await refreshPairingAdministration(reportFailure: false)
        } else {
            clearPairingAdministration()
        }
    }

    func presencePIN(for claimID: String) -> String {
        presencePINs[claimID] ?? ""
    }

    func setPresencePIN(_ rawValue: String, for claimID: String) {
        let digits = rawValue.unicodeScalars.compactMap { scalar -> Character? in
            guard (48 ... 57).contains(scalar.value) else { return nil }
            return Character(scalar)
        }
        presencePINs[claimID] = String(digits.prefix(6))
        if pairingIsError {
            pairingIsError = false
            pairingStatusMessage = ""
        }
    }

    func canPair(_ claim: HubPairingClaim) -> Bool {
        let pin = presencePIN(for: claim.claimID)
        return hubHealthy
            && pin.utf8.count == 6
            && pin.utf8.allSatisfy { (48 ... 57).contains($0) }
            && !pairingInFlightClaimIDs.contains(claim.claimID)
    }

    func monitorPairingAdministration() async {
        while !Task.isCancelled {
            await refreshPairingAdministration(reportFailure: false)
            do {
                try await Task.sleep(for: .seconds(2))
            } catch {
                return
            }
        }
    }

    func refreshPairingAdministration(reportFailure: Bool = true) async {
        guard hubHealthy, configuration != nil, !isRefreshingPairing else {
            return
        }
        isRefreshingPairing = true
        defer { isRefreshingPairing = false }
        do {
            let client = try pairingAdminClient()
            async let claimsRequest = client.pendingClaims()
            async let enrollmentsRequest = client.enrollments()
            let claims = try await claimsRequest
            let enrollments = try await enrollmentsRequest
            pendingPairingClaims = claims
            pairingEnrollments = enrollments
            let liveClaimIDs = Set(claims.map(\.claimID))
                .union(pairingInFlightClaimIDs)
            presencePINs = presencePINs.filter {
                liveClaimIDs.contains($0.key)
            }
        } catch {
            if reportFailure {
                pairingIsError = true
                pairingStatusMessage = error.localizedDescription
            }
        }
    }

    func pairAndEnable(_ claim: HubPairingClaim) async {
        guard canPair(claim) else {
            pairingIsError = true
            pairingStatusMessage = HubPairingAdminError.invalidPIN
                .localizedDescription
            return
        }
        let pin = presencePIN(for: claim.claimID)
        pairingInFlightClaimIDs.insert(claim.claimID)
        pairingIsError = false
        pairingStatusMessage = "Checking the code shown on \(claim.displayName)…"
        defer {
            pairingInFlightClaimIDs.remove(claim.claimID)
            presencePINs.removeValue(forKey: claim.claimID)
        }

        do {
            let client = try pairingAdminClient()
            let approved = try await client.approvePresence(
                claimID: claim.claimID,
                pin: pin
            )
            pendingPairingClaims.removeAll { $0.claimID == claim.claimID }
            pairingStatusMessage = "Code accepted. Waiting for \(claim.displayName) to finish its private credential exchange…"

            let deadline = Date().addingTimeInterval(90)
            var lastEnrollment = approved
            while Date() < deadline, !Task.isCancelled {
                do {
                    if let current = try await client.enrollments().first(
                        where: { $0.pairingID == approved.pairingID }
                    ) {
                        lastEnrollment = current
                    }
                    if let failureCode = lastEnrollment.failureCode {
                        throw HubModePresentationError.pairingState(
                            failureCode
                        )
                    }
                    switch lastEnrollment.state {
                    case "active":
                        pairingIsError = false
                        pairingStatusMessage = "\(claim.displayName) is paired and enabled. Its eligible models will publish automatically."
                        await refreshPairingAdministration(reportFailure: false)
                        return
                    case "disabled":
                        _ = try await client.setEnrollmentEnabled(
                            pairingID: approved.pairingID,
                            enabled: true
                        )
                        pairingIsError = false
                        pairingStatusMessage = "\(claim.displayName) is paired and enabled. Its eligible models will publish automatically."
                        await refreshPairingAdministration(reportFailure: false)
                        return
                    case "failed", "revoked", "recovery_required":
                        throw HubModePresentationError.pairingState(
                            lastEnrollment.failureCode ?? lastEnrollment.state
                        )
                    default:
                        break
                    }
                } catch let error as HubModePresentationError {
                    throw error
                } catch let error as HubPairingAdminError {
                    if case let .rejected(statusCode, _) = error,
                       statusCode < 500
                    {
                        throw error
                    }
                    // A transient loopback read must not abandon a code that
                    // the Hub already accepted. Keep polling to a fixed bound.
                }
                try await Task.sleep(for: .seconds(1))
            }
            throw HubModePresentationError.activationTimedOut
        } catch {
            pairingIsError = true
            pairingStatusMessage = error.localizedDescription
            await refreshPairingAdministration(reportFailure: false)
        }
    }

    func enableEnrollment(_ enrollment: HubPairingEnrollment) async {
        guard
            hubHealthy,
            enrollment.state == "disabled",
            !enrollment.hubEnabled,
            enrollment.failureCode == nil,
            !enrollmentInFlightIDs.contains(enrollment.pairingID)
        else { return }
        enrollmentInFlightIDs.insert(enrollment.pairingID)
        pairingIsError = false
        pairingStatusMessage = "Enabling \(enrollment.displayName)…"
        defer { enrollmentInFlightIDs.remove(enrollment.pairingID) }
        do {
            let client = try pairingAdminClient()
            _ = try await client.setEnrollmentEnabled(
                pairingID: enrollment.pairingID,
                enabled: true
            )
            pairingStatusMessage = "\(enrollment.displayName) is enabled. Its eligible models will publish automatically."
            await refreshPairingAdministration(reportFailure: false)
        } catch {
            pairingIsError = true
            pairingStatusMessage = error.localizedDescription
        }
    }

    func copyAdminKey() {
        copySecret { try store.adminKey() }
    }

    func copyClientKey() {
        copySecret { try store.clientKey() }
    }

    func openDashboard() {
        let base = configuration?.publicOrigin ?? "http://127.0.0.1:17400"
        guard let url = URL(string: base + "/fleet/") else { return }
        NSWorkspace.shared.open(url)
    }

    private func copySecret(_ value: () throws -> String) {
        do {
            let pasteboard = NSPasteboard.general
            pasteboard.clearContents()
            pasteboard.setString(try value(), forType: .string)
            isError = false
            statusMessage = "Credential copied. Paste it only into the intended trusted client."
        } catch {
            isError = true
            statusMessage = error.localizedDescription
        }
    }

    private func pairingAdminClient() throws -> HubPairingAdminClient {
        try HubPairingAdminClient(adminKey: store.adminKey())
    }

    private func clearPairingAdministration() {
        pendingPairingClaims = []
        pairingEnrollments = []
        presencePINs = [:]
        pairingInFlightClaimIDs = []
        enrollmentInFlightIDs = []
        pairingStatusMessage = ""
        pairingIsError = false
    }

    private func updateStatus(registration: LaunchAgentRegistration) {
        if configuration == nil {
            statusMessage = "Hub Mode has not been configured."
        } else if registration.hubAgentStatus == .requiresApproval {
            statusMessage = "Fleet Hub needs approval in System Settings → General → Login Items."
        } else if registration.hubAgentStatus != .enabled {
            statusMessage = "Fleet Hub is configured but disabled. Private state is preserved."
        } else if hubHealthy {
            statusMessage = "Fleet Hub is running."
        } else {
            statusMessage = "Fleet Hub is registered but not healthy yet."
        }
        isError = registration.hubAgentStatus == .enabled && !hubHealthy
    }

    private func waitForHealth() async -> Bool {
        let deadline = Date().addingTimeInterval(30)
        while Date() < deadline {
            if await healthCheck() { return true }
            try? await Task.sleep(for: .milliseconds(500))
        }
        return false
    }

    private func healthCheck() async -> Bool {
        var request = URLRequest(url: URL(string: "http://127.0.0.1:17400/health")!)
        request.timeoutInterval = 2
        request.cachePolicy = .reloadIgnoringLocalAndRemoteCacheData
        let sessionConfiguration = URLSessionConfiguration.ephemeral
        sessionConfiguration.connectionProxyDictionary = [:]
        do {
            let (_, response) = try await URLSession(
                configuration: sessionConfiguration
            ).data(for: request)
            return (response as? HTTPURLResponse)?.statusCode == 200
        } catch {
            return false
        }
    }
}

private enum HubModePresentationError: Error, LocalizedError {
    case registration(String)
    case health
    case activationTimedOut
    case pairingState(String)

    var errorDescription: String? {
        switch self {
        case let .registration(message): message
        case .health:
            "Fleet Hub did not become healthy. Its preserved configuration remains available for retry."
        case .activationTimedOut:
            "The code was accepted, but the Mac has not completed activation yet. Keep its Inference Pool settings open; when it appears below as disabled, choose Enable."
        case let .pairingState(code):
            "The Mac could not complete pairing (\(code))."
        }
    }
}

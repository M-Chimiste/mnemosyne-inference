import Foundation
import MnemosyneAppCore
import SwiftUI

@MainActor
final class MenuViewModel: ObservableObject {
    enum ConnectionState: Equatable {
        case checking
        case online
        case offline(String)
    }

    @Published private(set) var connection: ConnectionState = .checking
    @Published private(set) var snapshot: ServiceSnapshot?
    @Published private(set) var models: [ModelSummary] = []
    @Published private(set) var fleetPairing: FleetPairingSnapshot?
    @Published private(set) var fleetParticipation: FleetParticipationSnapshot?
    @Published var selectedAlias = ""
    @Published private(set) var actionError = ""
    @Published private(set) var lastUpdated: Date?
    @Published private(set) var inferenceEndpoint: URL?
    var isLive: Bool { connection == .online && (lastUpdated.map { Date().timeIntervalSince($0) < 15 } ?? false) }
    var activity: WorkspaceActivity { WorkspaceActivity(snapshot: snapshot, isLive: isLive) }
    @Published private(set) var mutationInProgress = false
    @Published private(set) var participationMutationInProgress = false

    private let client: any ControlAPI
    private var participationMutationGeneration = 0
    let controlBaseURL: URL

    init(
        client: (any ControlAPI)? = nil,
        connectionConfiguration: ControlConnectionConfiguration? = nil
    ) {
        let configuration = connectionConfiguration ?? .load()
        controlBaseURL = configuration.baseURL
        if let client {
            self.client = client
            return
        }
        self.client = ControlAPIClient(
            baseURL: configuration.baseURL,
            adminPassword: configuration.adminPassword
        )
    }

    #if DEBUG
    func loadWorkspacePreview(_ fixture: WorkspacePreviewFixture) {
        snapshot = fixture.status
        models = fixture.catalog.models
        fleetPairing = fixture.pairing
        fleetParticipation = fixture.participation
        selectedAlias = models.first?.id ?? ""
        lastUpdated = Date().addingTimeInterval(3600)
        connection = .online
        inferenceEndpoint = URL(string: "http://127.0.0.1:1240/v1")
    }
    #endif

    func refresh() async {
        let requestedAt = Date()
        if snapshot == nil {
            connection = .checking
        }
        let participationGeneration = participationMutationGeneration
        do {
            async let status = client.status()
            async let catalog = client.models()
            async let pairing = try? client.fleetPairing()
            async let participation = try? client.fleetParticipation()
            let (newSnapshot, newCatalog, newPairing, newParticipation) = try await (
                status,
                catalog,
                pairing,
                participation
            )
            guard !Task.isCancelled else { return }
            snapshot = newSnapshot
            models = newCatalog.models.sorted { $0.id < $1.id }
            fleetPairing = newPairing
            // A GET started before a pause/join mutation must not overwrite
            // the newer result when it eventually returns.
            if participationGeneration == participationMutationGeneration {
                fleetParticipation = newParticipation
            }
            let availableAliases = Set(models.map(\.id))
            if !availableAliases.contains(selectedAlias) {
                selectedAlias = newCatalog.residentAlias.flatMap {
                    availableAliases.contains($0) ? $0 : nil
                } ?? models.first?.id ?? ""
            }
            lastUpdated = requestedAt
            connection = .online
            if let port = newSnapshot.ports?.inference, (1...65535).contains(port) {
                var url = URLComponents()
                url.scheme = "http"
                url.host = "127.0.0.1"
                url.port = port
                url.path = "/v1"
                inferenceEndpoint = url.url
            } else { inferenceEndpoint = nil }
        } catch {
            guard !Task.isCancelled else { return }
            connection = .offline(error.localizedDescription)
        }
    }

    func setFleetParticipation(enabled: Bool) async {
        guard !participationMutationInProgress, connection == .online else {
            return
        }
        actionError = ""
        participationMutationGeneration += 1
        participationMutationInProgress = true
        defer { participationMutationInProgress = false }
        do {
            fleetParticipation = try await client.setFleetParticipation(
                enabled: enabled
            )
            connection = .online
        } catch {
            actionError = error.localizedDescription
        }
    }

    func loadSelectedModel() async {
        guard !selectedAlias.isEmpty, !mutationInProgress else { return }
        actionError = ""
        mutationInProgress = true
        defer { mutationInProgress = false }
        do {
            snapshot = try await client.load(model: selectedAlias)
            lastUpdated = Date()
            connection = .online
        } catch {
            actionError = error.localizedDescription
        }
    }

    func unloadResidentModel() async {
        guard !mutationInProgress else { return }
        actionError = ""
        mutationInProgress = true
        defer { mutationInProgress = false }
        do {
            try await client.unload()
            await refresh()
        } catch {
            actionError = error.localizedDescription
        }
    }
}

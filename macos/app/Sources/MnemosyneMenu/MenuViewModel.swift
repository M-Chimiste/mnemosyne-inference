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
    @Published var selectedAlias = ""
    @Published private(set) var mutationInProgress = false

    private let client: any ControlAPI
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

    func refresh() async {
        if snapshot == nil {
            connection = .checking
        }
        do {
            async let status = client.status()
            async let catalog = client.models()
            let (newSnapshot, newCatalog) = try await (status, catalog)
            snapshot = newSnapshot
            models = newCatalog.models.sorted { $0.id < $1.id }
            let availableAliases = Set(models.map(\.id))
            if !availableAliases.contains(selectedAlias) {
                selectedAlias = newCatalog.residentAlias.flatMap {
                    availableAliases.contains($0) ? $0 : nil
                } ?? models.first?.id ?? ""
            }
            connection = .online
        } catch {
            connection = .offline(error.localizedDescription)
        }
    }

    func loadSelectedModel() async {
        guard !selectedAlias.isEmpty, !mutationInProgress else { return }
        mutationInProgress = true
        defer { mutationInProgress = false }
        do {
            snapshot = try await client.load(model: selectedAlias)
            connection = .online
        } catch {
            connection = .offline(error.localizedDescription)
        }
    }

    func unloadResidentModel() async {
        guard !mutationInProgress else { return }
        mutationInProgress = true
        defer { mutationInProgress = false }
        do {
            try await client.unload()
            await refresh()
        } catch {
            connection = .offline(error.localizedDescription)
        }
    }
}

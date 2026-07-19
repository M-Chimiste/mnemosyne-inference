import Foundation
import MnemosyneAppCore
import SwiftUI

@MainActor
final class ConfigurationEditorViewModel: ObservableObject {
    enum Tab: String, CaseIterable, Identifiable {
        case configuration = "config.yaml"
        case environment = ".env"

        var id: String { rawValue }
    }

    enum StatusTone {
        case normal
        case success
        case warning
        case error
    }

    @Published var selectedTab: Tab = .configuration
    @Published var configYAML = ""
    @Published var environment = ""
    @Published var confirmUnvalidatedSave = false
    @Published var confirmReloadFromDisk = false
    @Published private(set) var isWorking = false
    @Published private(set) var requiresRestart = false
    @Published private(set) var canSaveWithoutValidation = false
    @Published private(set) var statusMessage = ""
    @Published private(set) var statusTone: StatusTone = .normal

    let configURL: URL
    let environmentURL: URL

    private let client: any ControlAPI
    private let store: ConfigurationDocumentStore
    private var savedConfigYAML = ""
    private var savedEnvironment = ""

    init(
        configuration: ControlConnectionConfiguration = .load(),
        client: (any ControlAPI)? = nil
    ) {
        configURL = configuration.configURL
        environmentURL = configuration.environmentURL
        store = ConfigurationDocumentStore(
            configURL: configuration.configURL,
            environmentURL: configuration.environmentURL
        )
        self.client = client ?? ControlAPIClient(
            baseURL: configuration.baseURL,
            adminPassword: configuration.adminPassword
        )
    }

    var hasUnsavedChanges: Bool {
        configYAML != savedConfigYAML || environment != savedEnvironment
    }

    var statusColor: Color {
        switch statusTone {
        case .normal:
            .secondary
        case .success:
            .green
        case .warning:
            .orange
        case .error:
            .red
        }
    }

    func loadFromDisk() {
        guard !isWorking else { return }
        do {
            let documents = try store.load()
            configYAML = documents.configYAML
            environment = documents.environment
            savedConfigYAML = documents.configYAML
            savedEnvironment = documents.environment
            requiresRestart = false
            canSaveWithoutValidation = false
            setStatus("Loaded configuration from disk.", tone: .normal)
        } catch {
            setStatus(error.localizedDescription, tone: .error)
        }
    }

    func validateSaveAndApply() async {
        guard !isWorking else { return }
        guard !configYAML.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            selectedTab = .configuration
            setStatus("config.yaml cannot be empty.", tone: .error)
            return
        }

        isWorking = true
        canSaveWithoutValidation = false
        setStatus("Validating config.yaml…", tone: .normal)
        defer { isWorking = false }

        let environmentChanged = environment != savedEnvironment
        let validation: ConfigurationValidation
        do {
            validation = try await client.validateConfiguration(configYAML)
        } catch let error as ControlAPIError {
            if case .rejected = error {
                selectedTab = .configuration
                setStatus(error.localizedDescription, tone: .error)
            } else {
                canSaveWithoutValidation = true
                setStatus(
                    "The service could not validate this draft: \(error.localizedDescription)",
                    tone: .warning
                )
            }
            return
        } catch {
            canSaveWithoutValidation = true
            setStatus(
                "The service could not validate this draft: \(error.localizedDescription)",
                tone: .warning
            )
            return
        }

        do {
            try persistDraft()
        } catch {
            setStatus("Could not save configuration: \(error.localizedDescription)", tone: .error)
            return
        }

        if environmentChanged {
            requiresRestart = true
            setStatus(
                "Saved and validated \(validation.modelCount) model profiles. Restart the service to apply .env changes.",
                tone: .warning
            )
            return
        }

        do {
            _ = try await client.reloadConfiguration()
            requiresRestart = false
            setStatus(
                "Saved and applied \(validation.modelCount) model profiles.",
                tone: .success
            )
        } catch let error as ControlAPIError {
            requiresRestart = true
            setStatus(
                "Saved, but a service restart is required: \(error.localizedDescription)",
                tone: .warning
            )
        } catch {
            requiresRestart = true
            setStatus(
                "Saved, but reload failed: \(error.localizedDescription)",
                tone: .warning
            )
        }
    }

    func saveWithoutValidation() {
        guard !isWorking else { return }
        do {
            try persistDraft()
            requiresRestart = true
            canSaveWithoutValidation = false
            setStatus(
                "Saved without validation. Restart the service to try this configuration.",
                tone: .warning
            )
        } catch {
            setStatus("Could not save configuration: \(error.localizedDescription)", tone: .error)
        }
    }

    func serviceRestartRequested(succeeded: Bool, error: String?) {
        if succeeded {
            requiresRestart = false
            setStatus("Background service restart requested.", tone: .success)
        } else {
            setStatus(
                error ?? "The background service could not be restarted.",
                tone: .error
            )
        }
    }

    private func persistDraft() throws {
        let documents = ConfigurationDocuments(
            configYAML: configYAML,
            environment: environment
        )
        try store.save(documents)
        savedConfigYAML = configYAML
        savedEnvironment = environment
    }

    private func setStatus(_ message: String, tone: StatusTone) {
        statusMessage = message
        statusTone = tone
    }
}
